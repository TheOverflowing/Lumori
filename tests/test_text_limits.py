"""Long authored text survives the API and storage; no live model calls."""
import csv
import io
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Endpoint, Settings
from app.main import create_app
from app.models import CourseCreate, GenerateRequest, Question, Section
from app.providers import ApiProviders
from test_workflow import apply_difficulty_fixture, difficulty_judge_fixture


def long_text(label, former_limit):
    return (label + ' ' + 'Detailed English content. ' * (former_limit // 20 + 1)).strip()


def long_asset(reference_id):
    return {
        'title': long_text('Title', 200),
        'evidence_sufficient': True,
        'evidence_note': long_text('Evidence note', 1000),
        'learning_objectives': [long_text('Objective', 1000)],
        'sections': [{
            'heading': long_text('Heading', 200),
            'text': long_text('Body', 6000),
            'citation_ids': [reference_id],
        }],
        'questions': [{
            'kind': 'short_answer',
            'stem': long_text('Question', 3000),
            'options': [],
            'answer': long_text('Answer', 3000),
            'explanation': long_text('Explanation', 4000),
            'difficulty_reason': long_text('Difficulty rationale', 1000),
            'citation_ids': [reference_id],
        }],
        'visual_prompt': long_text('Visual prompt', 3000),
    }


class LongTextWire:
    def __init__(self):
        self.embedding_inputs = []
        self.generation_request = None
        self.asset = None

    def __call__(self, request):
        data = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            self.embedding_inputs.extend(data['input'])
            return httpx.Response(200, json={'data': [
                {'index': index, 'embedding': [1., .5, .25]}
                for index, _ in enumerate(data['input'])
            ]})
        if request.url.path.endswith('/chat/completions'):
            instruction = json.loads(data['messages'][1]['content'])
            if instruction.get('task') == 'difficulty_assessment':
                result=difficulty_judge_fixture(instruction,self.difficulty_levels)
                return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]})
            self.generation_request = instruction['request']
            self.asset = long_asset(instruction['reference_chunks'][0]['id'])
            self.difficulty_levels=apply_difficulty_fixture(self.asset,instruction)
            return httpx.Response(200, json={'choices': [{
                'finish_reason': 'stop', 'message': {'content': json.dumps(self.asset)},
            }]})
        raise AssertionError(f'Unexpected provider request: {request.url.path}')


def finish(client, response):
    assert response.status_code == 202, response.text
    job_id = response.json()['job_id']
    for _ in range(200):
        job = client.get('/api/jobs/' + job_id).json()
        if job['status'] not in ('queued', 'running'):
            assert job['status'] == 'succeeded', job
            return job
        time.sleep(.005)
    pytest.fail('Isolated job did not complete')


def test_long_text_round_trips_through_generation_review_and_evaluation(tmp_path):
    settings = Settings(data_dir=tmp_path)
    for capability, path in [('text', '/chat/completions'), ('embedding', '/embeddings')]:
        setattr(settings, capability, Endpoint('https://fixture.invalid/v1', 'fixture-key', 'fixture-model', path))
    wire = LongTextWire()
    app = create_app(settings, lambda config, store: ApiProviders(
        config, store, httpx.AsyncClient(transport=httpx.MockTransport(wire))))

    with TestClient(app) as client:
        from test_workflow import authenticate
        authenticate(client)
        name = long_text('Course', 120)
        response = client.post('/api/courses', json={'name': name})
        assert response.status_code == 201, response.text
        course_id = response.json()['id']
        assert response.json()['name'] == name
        assert client.get('/api/courses').json()[0]['name'] == name

        response = client.post('/api/documents', data={'course_id': course_id}, files={
            'file': ('reference.md', b'Retrieval supplies reference passages for teaching materials.'),
        })
        assert response.status_code == 201, response.text
        finish(client, client.post('/api/documents/' + response.json()['id'] + '/index'))

        topic = long_text('Explain retrieval with examples and limitations.', 3000)
        job = finish(client, client.post('/api/generations', json={
            'course_id': course_id, 'topic': topic, 'material': 'quiz',
            'question_type': 'short_answer', 'count': 1, 'language': 'en',
            'include_explanations': True,
            'request_key': 'long-text-generation',
        }))
        content_id = job['result']['content_id']
        assert wire.embedding_inputs[-1] == topic
        assert wire.generation_request['topic'] == topic
        content = client.get('/api/contents/' + content_id).json()
        assert content['config']['topic'] == topic
        # Legacy assets receive the explicit shared-material default without
        # changing any authored text or adding question-local material bundles.
        assert content['asset'] == wire.asset | {'section_scope': 'shared'}
        assert client.get('/api/contents', params={'course_id': course_id}).json()[0]['title'] == wire.asset['title']

        edited = content['asset']
        edited['title'] += ' Revised'
        edited['sections'][0]['text'] += '\nA teacher-authored revision.'
        edited['questions'][0]['answer'] += '\nA longer answer added during review.'
        response = client.post('/api/contents/' + content_id + '/review', json={
            'version': 1, 'action': 'save', 'asset': edited,
        })
        assert response.status_code == 200, response.text
        assert response.json()['version'] == 2
        assert client.get('/api/contents/' + content_id).json()['asset'] == edited

        notes = long_text('Reviewer notes', 5000)
        response = client.post('/api/contents/' + content_id + '/evaluations', json={
            'version': 2, 'correctness': 5, 'groundedness': 4, 'difficulty_match': 3, 'notes': notes,
        })
        assert response.status_code == 201, response.text
        export = client.get('/api/evaluations/export')
        row = next(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
        assert row['notes'] == notes
        assert row['content_id'] == content_id


@pytest.mark.parametrize('changes', [
    {'topic': ''}, {'topic': 'x'}, {'count': 0}, {'count': 51},
    {'document_ids': ['document'] * 21}, {'request_key': 'x' * 101},
])
def test_generation_keeps_required_and_operational_constraints(changes):
    valid = {'course_id': 'course', 'topic': 'A learning goal', 'request_key': 'valid-request-key'}
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(valid | changes)


def test_empty_names_and_body_and_invalid_question_options_remain_rejected():
    with pytest.raises(ValidationError):
        CourseCreate(name='   ')
    with pytest.raises(ValidationError):
        Section(heading='Heading', text='', citation_ids=['source'])
    with pytest.raises(ValidationError):
        Question(kind='mcq', stem='Question', options=['Same'] * 4, answer='A',
                 explanation='Explanation', difficulty_reason='Reason', citation_ids=['source'])
