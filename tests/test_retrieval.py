"""RAG failure, evidence, and persistence checks using isolated HTTP fixtures."""
import json
import math

import httpx
import pytest

from test_workflow import done, generated, request, rig, seed


def test_selected_unindexed_document_blocks_generation_before_api_call(rig):
    client, app, wire = rig
    course, ready = seed(client)
    parsed = client.post('/api/documents', data={'course_id': course},
        files={'file': ('unindexed.md', '尚未索引的补充资料。'.encode())}).json()['id']
    before = wire.calls
    response = client.post('/api/generations', json=request(course) | {'document_ids': [ready, parsed]})
    job = done(client, response)
    assert job['status'] == 'failed' and '全部选定资料' in job['error']
    assert wire.calls == before


@pytest.mark.parametrize('corrupt_vector', [
    '[1.0, 0.5]', 'null', '[NaN, 0, 0]', '[0, 0, 0]', '[[1, 2]]', '["invalid", 0, 0]',
])
def test_invalid_cached_vectors_fail_before_query_call(rig, corrupt_vector):
    client, app, wire = rig
    course, document = seed(client)
    second = client.post('/api/documents', data={'course_id': course},
        files={'file': ('second.md', '另一份独立的课程参考资料。'.encode())}).json()['id']
    assert done(client, client.post('/api/documents/' + second + '/index'))['status'] == 'succeeded'
    app.state.store.execute('UPDATE chunks SET vector=? WHERE document_id=?', (corrupt_vector, document))
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'failed' and '无效向量或维度不一致' in job['error']
    assert wire.calls == before
    assert 'inhomogeneous' not in job['error'] and 'invalid' not in job['error']


def test_large_finite_cached_vector_has_finite_cosine_score(rig):
    client, app, _ = rig
    course, document = seed(client)
    app.state.store.execute('UPDATE chunks SET vector=? WHERE document_id=?', ('[1e308, 0, 0]', document))
    cid = generated(client, course)
    source = client.get('/api/contents/' + cid).json()['sources'][0]
    assert math.isfinite(source['score']) and -1 <= source['score'] <= 1


def test_query_dimension_drift_is_explicit_and_never_calls_text(rig, monkeypatch):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__

    def changed_dimension(self, http_request):
        if http_request.url.path.endswith('/embeddings'):
            self.calls += 1
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0, .5]}]})
        return original(self, http_request)

    monkeypatch.setattr(type(wire), '__call__', changed_dimension)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'failed' and '向量维度变化' in job['error']
    assert app.state.store.all("SELECT * FROM calls WHERE capability='text'") == []


@pytest.mark.parametrize('mode, expected_status', [('valid', 'succeeded'), ('insufficient', 'insufficient_evidence'), ('bad_citation', 'failed')])
def test_job_evidence_survives_success_insufficient_and_invalid_output(rig, mode, expected_status):
    client, app, wire = rig
    course, _ = seed(client)
    wire.mode = mode
    response = client.post('/api/generations', json=request(course))
    job = done(client, response)
    assert job['status'] == expected_status
    evidence = client.get('/api/jobs/' + response.json()['job_id'] + '/evidence').json()
    # The endpoint may add job metadata; the saved evidence remains the same public contract.
    evidence = evidence.get('evidence', evidence)
    assert evidence['sources'] and evidence['configuration']['embedding_model']
    assert evidence['request']['topic'] == '检索的作用'
    assert 'request_key' not in evidence['request'] and 'request_key' not in evidence['configuration']
    assert len(evidence['attempts']) == (3 if mode == 'bad_citation' else 1)
    assert evidence['attempts'][0]['response']['evidence_sufficient'] is (mode != 'insufficient')
    assert evidence['attempts'][-1]['status'] == ('invalid_output' if mode == 'bad_citation' else 'valid')
    assert 'test-key-not-real' not in json.dumps(evidence)


@pytest.mark.parametrize('first_reply', ['invalid_json', 'truncated_json'])
def test_model_output_error_is_repaired_once(rig, monkeypatch, first_reply):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__
    text_attempts = 0

    def invalid_then_valid(self, http_request):
        nonlocal text_attempts
        if http_request.url.path.endswith('/chat/completions') and json.loads(json.loads(http_request.content)['messages'][1]['content']).get('task')!='difficulty_assessment':
            text_attempts += 1
            if text_attempts == 1:
                self.calls += 1
                return httpx.Response(200, json={'choices': [{'finish_reason': 'length' if first_reply == 'truncated_json' else 'stop',
                    'message': {'content': '{"title":' if first_reply == 'invalid_json' else '{}'}}]})
        return original(self, http_request)

    monkeypatch.setattr(type(wire), '__call__', invalid_then_valid)
    response = client.post('/api/generations', json=request(course))
    assert done(client, response)['status'] == 'succeeded'
    assert text_attempts == 2
    evidence = client.get('/api/jobs/' + response.json()['job_id'] + '/evidence').json()
    evidence = evidence.get('evidence', evidence)
    assert [attempt['status'] for attempt in evidence['attempts']] == ['invalid_output', 'valid']


def test_failed_initial_revision_rolls_back_generated_content(rig):
    client, app, _ = rig
    course, _ = seed(client)
    app.state.store.execute("CREATE TRIGGER reject_initial_revision BEFORE INSERT ON revisions BEGIN SELECT RAISE(ABORT, 'fixture revision failure'); END")
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'failed'
    assert client.get('/api/contents', params={'course_id': course}).json() == []
    assert app.state.store.all('SELECT * FROM revisions') == []


def test_text_call_budget_failure_is_not_retried_or_misreported(rig):
    client, app, wire = rig
    course, _ = seed(client)
    # One existing index call and one allowed query call leave no budget for generation.
    app.state.settings.max_daily_calls = 2
    response = client.post('/api/generations', json=request(course))
    job = done(client, response)
    assert job['status'] == 'failed' and '上限' in job['error']
    evidence = client.get('/api/jobs/' + response.json()['job_id'] + '/evidence').json()
    evidence = evidence.get('evidence', evidence)
    assert len(evidence['attempts']) == 1 and evidence['attempts'][0]['status'] == 'provider_error'


@pytest.mark.parametrize('material,count', [('lesson', 1), ('quiz', 2), ('assignment', 3)])
def test_generation_prompt_contract_matches_material(rig, monkeypatch, material, count):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__
    sent = []

    def capture(self, http_request):
        if http_request.url.path.endswith('/chat/completions'):
            sent.append(json.loads(http_request.content))
        return original(self, http_request)

    monkeypatch.setattr(type(wire), '__call__', capture)
    response = client.post('/api/generations', json=request(course) | {
        'material': material, 'count': count, 'question_type': 'short_answer' if material == 'lesson' else 'mcq'})
    job = done(client, response)
    assert job['status'] == 'succeeded'
    instruction = json.loads(sent[0]['messages'][1]['content'])
    expected = 0 if material == 'lesson' else count
    assert instruction['question_count'] == expected
    assert instruction['schema']['properties']['questions']['maxItems'] == expected
    assert instruction['schema']['properties']['questions'].get('minItems', 0) == 0
    if material == 'lesson':
        assert 'count' not in instruction['request'] and 'question_type' not in instruction['request']
        assert 'questions 必须为 []' in sent[0]['messages'][0]['content']
    else:
        assert instruction['request']['count'] == count and instruction['request']['question_type'] == 'mcq'
    evidence = client.get('/api/jobs/' + response.json()['job_id'] + '/evidence').json()
    evidence = evidence.get('evidence', evidence)
    assert evidence['prompt_version'] == 'education-v3'
    assert evidence['configuration']['text_json_mode'] is app.state.settings.text_json_mode
    assert evidence['generation_contract']['request'] == instruction['request']
    assert evidence['generation_contract']['schema'] == instruction['schema']


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_lesson_repair_states_exact_zero_question_rule_and_stays_bounded(rig, monkeypatch, repair_succeeds):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__
    sent = []

    def adds_unwanted_question(self, http_request):
        response = original(self, http_request)
        if http_request.url.path.endswith('/chat/completions'):
            sent.append(json.loads(http_request.content))
            if len(sent) == 1 or not repair_succeeds:
                envelope = response.json()
                asset = json.loads(envelope['choices'][0]['message']['content'])
                asset['questions'] = [{'kind': 'short_answer', 'stem': '多余题目', 'options': [],
                    'answer': '参考答案', 'explanation': '简短解释', 'difficulty_reason': '单一知识点',
                    'citation_ids': asset['sections'][0]['citation_ids']}]
                envelope['choices'][0]['message']['content'] = json.dumps(asset)
                return httpx.Response(200, json=envelope)
        return response

    monkeypatch.setattr(type(wire), '__call__', adds_unwanted_question)
    response = client.post('/api/generations', json=request(course) | {
        'material': 'lesson', 'count': 1, 'question_type': 'short_answer'})
    job = done(client, response)
    assert job['status'] == ('succeeded' if repair_succeeds else 'failed')
    assert len(sent) == (2 if repair_succeeds else 3)
    repair = json.loads(sent[1]['messages'][-1]['content'].split('\n', 1)[1])
    assert repair['validation'] == {'rule': 'question_count_mismatch', 'expected_questions': 0, 'actual_questions': 1}
    assert repair['required_constraints']['questions'] == '必须为 []，不得包含任何题目。'
    assert 'question_kind' not in repair['required_constraints']


def test_schema_repair_never_echoes_validation_input_or_arbitrary_field_names(rig, monkeypatch):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__
    sent = []
    marker = 'DO_NOT_ECHO_UNTRUSTED_FIELD_OR_VALUE'

    def invalid_schema_then_valid(self, http_request):
        response = original(self, http_request)
        if http_request.url.path.endswith('/chat/completions') and json.loads(json.loads(http_request.content)['messages'][1]['content']).get('task')!='difficulty_assessment':
            sent.append(json.loads(http_request.content))
            if len(sent) == 1:
                envelope = response.json()
                asset = json.loads(envelope['choices'][0]['message']['content'])
                asset['title'] = {'untrusted_value': marker}
                asset[marker] = marker
                envelope['choices'][0]['message']['content'] = json.dumps(asset)
                return httpx.Response(200, json=envelope)
        return response

    monkeypatch.setattr(type(wire), '__call__', invalid_schema_then_valid)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'succeeded' and len(sent) == 2
    repair_text = sent[1]['messages'][-1]['content']
    assert marker not in repair_text and 'input' not in repair_text
    diagnostic = json.loads(repair_text.split('\n', 1)[1])['validation']
    assert diagnostic['rule'] == 'schema_validation'
    assert {'loc': ['title'], 'type': 'string_type'} in diagnostic['fields']
    assert {'loc': ['unknown_field'], 'type': 'extra_forbidden'} in diagnostic['fields']
