"""Cross-course library and job navigation contracts using isolated HTTP fixtures."""
import json

import pytest

from app.job_progress import EVENT_FIELDS, MAX_EVENTS
from test_workflow import done, request, rig


def seed_named(client, name):
    course = client.post('/api/courses', json={'name': name}).json()['id']
    document = client.post('/api/documents', data={'course_id': course},
        files={'file': ('notes.md', '检索为生成提供课程参考资料。'.encode())}).json()['id']
    index = done(client, client.post('/api/documents/' + document + '/index'))
    assert index['status'] == 'succeeded'
    return course, document, index


def generate(client, course, key, material='quiz'):
    job = done(client, client.post('/api/generations', json=request(course, key) | {'material': material}))
    assert job['status'] == 'succeeded', job
    return job


def test_library_lists_every_course_and_preserves_explicit_course_scope(rig):
    client, _, _ = rig
    first, first_document, _ = seed_named(client, '第一门课程')
    second, second_document, _ = seed_named(client, 'Second course')
    empty = client.post('/api/courses', json={'name': 'Empty course'}).json()['id']
    lesson = generate(client, first, 'library-lesson', 'lesson')
    quiz = generate(client, second, 'library-quiz')
    assignment = generate(client, second, 'library-assignment', 'assignment')
    expected = {
        lesson['content_id']: (first, '第一门课程', 'lesson', first_document),
        quiz['content_id']: (second, 'Second course', 'quiz', second_document),
        assignment['content_id']: (second, 'Second course', 'assignment', second_document),
    }

    rows = client.get('/api/contents').json()
    assert {row['id'] for row in rows} == set(expected)
    assert [row['created_at'] for row in rows] == sorted((row['created_at'] for row in rows), reverse=True)
    for row in rows:
        course, name, material, document = expected[row['id']]
        assert set(row) == {'id', 'version', 'status', 'created_at', 'course_id', 'course_name', 'title', 'material'}
        assert (row['course_id'], row['course_name'], row['material']) == (course, name, material)
        assert row['status'] == 'draft' and row['version'] == 1 and row['title']
        content = client.get('/api/contents/' + row['id']).json()
        assert {source['document_id'] for source in content['sources']} == {document}
    assert {row['id'] for row in client.get('/api/contents', params={'course_id': first}).json()} == {lesson['content_id']}
    assert {row['id'] for row in client.get('/api/contents', params={'course_id': second}).json()} == {quiz['content_id'], assignment['content_id']}
    assert client.get('/api/contents', params={'course_id': empty}).json() == []
    assert client.get('/api/contents', params={'course_id': 'unknown-course'}).status_code == 404
    assert client.post('/api/generations', json=request(first, 'cross-course-doc') | {'document_ids': [second_document]}).status_code == 400


@pytest.mark.parametrize('mode,status', [('bad_citation', 'failed'), ('insufficient', 'insufficient_evidence')])
def test_unsuccessful_generation_has_course_context_without_a_phantom_material(rig, mode, status):
    client, _, wire = rig
    course, _, _ = seed_named(client, 'Generation result course')
    wire.mode = mode
    private_key = 'private-generation-request-key'
    private_topic = 'A private learning objective which must not appear in job navigation metadata'
    job = done(client, client.post('/api/generations', json=request(course, private_key) | {'topic': private_topic}))
    assert job['status'] == status and job['course_id'] == course and job['content_id'] is None
    listed = next(row for row in client.get('/api/jobs', params={'course_id': course}).json() if row['id'] == job['id'])
    assert listed['status'] == status and listed['course_id'] == course and listed['content_id'] is None
    assert client.get('/api/contents').json() == []
    public = json.dumps([job, listed])
    for private in ('payload', 'request_key', private_key, private_topic, 'test-key-not-real'):
        assert private not in public


def test_job_metadata_resolves_index_generation_and_failed_media_without_payloads(rig):
    client, _, wire = rig
    course, _, index = seed_named(client, 'Media course')
    other, _, other_index = seed_named(client, 'Other course')
    generation = generate(client, course, 'metadata-generation')
    cid = generation['content_id']
    assert client.post('/api/contents/' + cid + '/review', json={'version': 1, 'action': 'approve'}).status_code == 200
    wire.mode = 'http_error'
    media = done(client, client.post('/api/contents/' + cid + '/media',
        json={'version': 1, 'kind': 'audio', 'request_key': 'private-media-request-key'}))
    assert media['status'] == 'failed' and 'HTTP 401' in media['error']

    expected = {index['id']: None, generation['id']: cid, media['id']: cid}
    scoped = client.get('/api/jobs', params={'course_id': course}).json()
    assert {row['id'] for row in scoped} == set(expected)
    for row in scoped:
        assert set(row) == {'id', 'kind', 'status', 'error', 'created_at', 'course_id', 'content_id'}
        assert row['course_id'] == course and row['content_id'] == expected[row['id']]
        detail = client.get('/api/jobs/' + row['id']).json()
        assert detail['course_id'] == course and detail['content_id'] == expected[row['id']]
        assert set(detail) == set(row) | {'result', 'updated_at', 'timeline'}
        timeline = detail['timeline']
        assert set(timeline) == {'version', 'recorded', 'stages', 'active_stage', 'activity',
            'completed', 'total', 'unit', 'updated_at', 'elapsed_ms', 'timing_complete',
            'events', 'events_truncated'}
        assert timeline['version'] == 'job-progress-v1'
        expected_stages = {'index': [], 'generate': ['retrieval', 'writing', 'saving'],
                           'media': ['audio', 'saving']}[row['kind']]
        assert timeline['recorded'] is bool(expected_stages)
        assert [stage['id'] for stage in timeline['stages']] == expected_stages
        for stage in timeline['stages']:
            assert set(stage) == {'id', 'kind', 'status', 'started_at', 'finished_at', 'elapsed_ms', 'timing_complete'}
            assert stage['kind'] == stage['id']
            assert stage['status'] in {'pending', 'active', 'completed', 'failed', 'blocked'}
            assert stage['elapsed_ms'] is None or type(stage['elapsed_ms']) is int and stage['elapsed_ms'] >= 0
        assert len(timeline['events']) <= MAX_EVENTS == 120
        for event in timeline['events']:
            assert set(event) == {'id', 'stage', 'code', 'at', 'data'}
            assert event['stage'] in expected_stages and event['code'] in EVENT_FIELDS
            assert set(event['data']) == set(EVENT_FIELDS[event['code']])
            for key, allowed in EVENT_FIELDS[event['code']].items():
                value = event['data'][key]
                assert (type(value) is int and 0 <= value <= 10**7) if allowed == 'integer' else value in allowed
        public = json.dumps([row, detail])
        for private in ('payload', 'request_key', 'private-media-request-key', 'metadata-generation', 'test-key-not-real'):
            assert private not in public
    assert {row['id'] for row in client.get('/api/jobs').json()} == set(expected) | {other_index['id']}
    assert [row['id'] for row in client.get('/api/jobs', params={'course_id': other}).json()] == [other_index['id']]
    assert client.get('/api/jobs', params={'course_id': 'unknown-course'}).status_code == 404
    assert client.get('/api/jobs/unknown-job').status_code == 404


def test_job_course_filter_is_applied_before_the_recent_job_limit(rig):
    client, app, _ = rig
    first, _, index = seed_named(client, 'Older course')
    second = client.post('/api/courses', json={'name': 'Busy course'}).json()['id']
    empty = client.post('/api/courses', json={'name': 'Empty course'}).json()['id']
    # Queue metadata directly in this isolated fixture; these jobs are not executed.
    for i in range(31):
        app.state.store.job('limit-fixture-' + str(i), 'generate', {'course_id': second, 'topic': 'private pending objective'},
            owner_id=client.headers['X-Account-ID'])
    assert len(client.get('/api/jobs').json()) == 30
    scoped = client.get('/api/jobs', params={'course_id': first}).json()
    assert [row['id'] for row in scoped] == [index['id']]
    pending = client.get('/api/jobs', params={'course_id': second}).json()
    assert len(pending) == 30 and all(row['course_id'] == second and row['content_id'] is None for row in pending)
    assert client.get('/api/jobs', params={'course_id': empty}).json() == []
    assert 'private pending objective' not in json.dumps(pending)
