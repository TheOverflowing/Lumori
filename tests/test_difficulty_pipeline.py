"""Difficulty contracts through isolated provider HTTP fixtures; no live calls."""
import asyncio
import copy
import json

import httpx
import pytest

from app.config import Endpoint, Settings
from app.difficulty import RUBRIC_VERSION, build_difficulty_plan
from app.models import GenerateRequest, LearningAsset
from app.pipeline import AssetRuleError, Pipeline, validate_asset
from app.providers import ApiProviders
from app.store import Store, dumps, now


SOURCE = {'id': 'chunk-1', 'document_id': 'doc-1', 'document_name': 'rag.md', 'page': 1,
          'text': 'RAG 检索课程资料提供生成依据。资料仍需人工核对。向量模型变更必须重建索引。'
                  '未索引资料不得参与检索。课程之间的资料必须隔离。'}
STEMS = {
    'easy': '识别 RAG 检索提供的内容。',
    'medium': '更换向量模型后检索报维度错误，应用兼容性规则说明如何恢复。',
    'hard': '课程 A 更换向量模型且补充资料未索引，课程 B 索引仍可用。'
            '在课程隔离和资料可核对的约束下设计恢复方案，并论证为什么不能直接复用 B 的索引。',
}


def question(slot, index, refs):
    level = slot['difficulty']
    return {'slot_id': slot['slot_id'], 'difficulty': level, 'kind': 'short_answer',
            'stem': f'{index}. {STEMS[level]}', 'options': [], 'answer': '先核对资料与索引状态。',
            'explanation': '资料范围与向量兼容性约束必须同时满足。',
            'difficulty_reason': 'PRIVATE_AUTHOR_REASON', 'citation_ids': refs,
            'difficulty_design': {
                'cognitive_process': {'easy': 'understand', 'medium': 'apply', 'hard': 'evaluate'}[level],
                'concepts': ['资料检索'],
                'expected_steps': ['核对课程资料', '判断索引兼容性', '论证恢复方案'][:
                    {'easy': 1, 'medium': 2, 'hard': 3}[level]],
            }}


class DifficultyWire:
    def __init__(self, mode='valid'):
        self.mode = mode
        self.generations = []
        self.assessments = []

    @staticmethod
    def response(body):
        return httpx.Response(200, json={'choices': [
            {'finish_reason': 'stop', 'message': {'content': dumps(body)}}]})

    def __call__(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data': [{'index': i, 'embedding': [1., .5]}
                for i, _ in enumerate(body['input'])]})
        instruction = json.loads(body['messages'][1]['content'])
        if instruction.get('task') == 'difficulty_assessment':
            self.assessments.append(copy.deepcopy(body))
            if self.mode == 'judge_http_error':
                return httpx.Response(401, text='private upstream response must not escape')
            if self.mode == 'judge_invalid_json':
                return httpx.Response(200, json={'choices': [
                    {'finish_reason': 'stop', 'message': {'content': '{"items":'}}]})
            items = []
            for q in instruction['questions']:
                # Simulated independent judgement from the supplied task, never its label.
                level = next(level for level, stem in STEMS.items() if stem in q['stem'])
                items.append({'slot_id': q['slot_id'], 'assessed_difficulty': level,
                    'confidence': 'high', 'rationale': 'PRIVATE_ASSESSMENT_REASON',
                    'answerable_from_sources': True, 'ambiguity_free': True})
            if self.mode == 'low_confidence': items[0]['confidence'] = 'low'
            if self.mode == 'unanswerable': items[0]['answerable_from_sources'] = False
            if self.mode == 'ambiguous': items[0]['ambiguity_free'] = False
            if self.mode == 'judge_missing_slot': items.pop()
            if self.mode == 'judge_duplicate_slot': items.append(copy.deepcopy(items[0]))
            if self.mode == 'judge_extra_slot': items.append(dict(items[0], slot_id='not-requested'))
            if self.mode == 'judge_coerced_bool': items[0]['ambiguity_free'] = 'true'
            return self.response({'items': items})
        self.generations.append(copy.deepcopy(body))
        refs = [instruction['reference_chunks'][0]['id']]
        asset = {'title': '课程检索', 'evidence_sufficient': self.mode != 'insufficient',
                 'sections': [], 'questions': []}
        if asset['evidence_sufficient']:
            if instruction['request'].get('include_explanations') or instruction['request']['material'] == 'lesson':
                asset['sections'] = [{'heading': '索引兼容性', 'text': SOURCE['text'], 'citation_ids': refs}]
            asset['questions'] = [question(slot, i, refs)
                for i, slot in enumerate(instruction['difficulty_plan'], 1)]
        if asset['questions']:
            first = asset['questions'][0]
            if self.mode == 'false_hard' or self.mode == 'repair_once' and len(self.generations) == 1 or self.mode == 'repair_twice' and len(self.generations) < 3:
                first['stem'] = '1. ' + STEMS['easy']
            if self.mode == 'wrong_label': first['difficulty'] = 'easy'
            if self.mode == 'wrong_slot': first['slot_id'] = 'q2'
            if self.mode == 'wrong_cognitive_process': first['difficulty_design']['cognitive_process'] = 'remember'
            if self.mode == 'wrong_steps': first['difficulty_design']['expected_steps'] = ['一个要点']
            if self.mode == 'missing_design': first.pop('difficulty_design')
        return self.response(asset)


@pytest.fixture
def run_case(tmp_path):
    counter = 0

    def run(mode='valid', **overrides):
        nonlocal counter
        counter += 1
        settings = Settings(data_dir=tmp_path / str(counter))
        settings.text = Endpoint('https://fixture.invalid/v1', 'fixture-key', 'fixture-text', '/chat/completions')
        settings.embedding = Endpoint('https://fixture.invalid/v1', 'fixture-key', 'fixture-vector', '/embeddings')
        store = Store(settings.data_dir)
        store.execute('INSERT INTO courses VALUES(?,?,?)', ('course-1', '课程', now()))
        store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                      ('doc-1', 'course-1', 'rag.md', 'fixture-hash', 'ready', 1, now()))
        store.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)',
                      ('chunk-1', 'doc-1', 1, SOURCE['text'], '[1.0,0.5]', settings.embedding.signature))
        wire = DifficultyWire(mode)
        providers = ApiProviders(settings, store, httpx.AsyncClient(transport=httpx.MockTransport(wire)))
        pipeline = Pipeline(settings, store, providers)
        payload = dict(course_id='course-1', topic='检索与索引恢复', difficulty='hard', count=3,
                       include_explanations=True,
                       question_type='short_answer', request_key=f'difficulty-{counter}') | overrides
        req = GenerateRequest(**payload)
        job, _ = store.job(req.request_key, 'generate', req.model_dump())

        async def perform():
            try:
                await pipeline.run(job['id'])
            finally:
                await providers.close()

        asyncio.run(perform())
        row = store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
        evidence = json.loads(store.one('SELECT * FROM job_evidence WHERE job_id=?', (job['id'],))['evidence'])
        contents = store.all('SELECT * FROM contents')
        content = ({key: json.loads(value) if key in ('asset', 'config', 'sources') else value
                    for key, value in contents[0].items()} if contents else None)
        return row, evidence, content, wire

    return run


@pytest.mark.parametrize('distribution, levels', [
    (None, ['hard', 'hard', 'hard']),
    ({'easy': 0, 'medium': 0, 'hard': 3}, ['hard', 'hard', 'hard']),
    ({'easy': 1, 'medium': 0, 'hard': 2}, ['easy', 'hard', 'hard']),
    ({'easy': 1, 'medium': 1, 'hard': 1}, ['easy', 'medium', 'hard']),
])
def test_exact_plan_is_enforced_and_retained(run_case, distribution, levels):
    job, evidence, content, wire = run_case(difficulty_distribution=distribution)
    assert job['status'] == 'succeeded', job
    expected = [{'slot_id': f'q{i}', 'difficulty': level} for i, level in enumerate(levels, 1)]
    assert content['config']['difficulty_plan'] == expected
    assert content['config']['rubric_version'] == RUBRIC_VERSION
    assert content['config']['learner_profile'] == '已学习所选课程资料的本科生'
    assert [q['difficulty'] for q in content['asset']['questions']] == levels
    assert evidence['generation_contract']['difficulty_plan'] == expected
    assert evidence['generation_contract']['rubric']
    assert evidence['prompt_version'] == 'education-v3'
    assert len(wire.generations) == len(wire.assessments) == 1
    assessment = evidence['attempts'][0]['assessment']
    assert assessment['status'] == 'passed' and assessment['response']['items']
    assert '非教师验证' in assessment['note']


def test_assessor_does_not_receive_author_target_or_design(run_case):
    job, evidence, content, wire = run_case(learner_profile='已掌握本课程定义的学习者')
    assert job['status'] == 'succeeded'
    sent = wire.assessments[0]
    assert len(sent['messages']) == 2
    instruction = json.loads(sent['messages'][1]['content'])
    assert set(instruction) == {'task', 'learner_profile', 'rubric_version', 'rubric',
                                'rubric_note', 'questions', 'reference_chunks', 'schema',
                                'student_visible_context'}
    assert instruction['learner_profile'] == '已掌握本课程定义的学习者'
    assert set(instruction['rubric']) == {'easy', 'medium', 'hard'}
    assert all(set(q) == {'slot_id', 'kind', 'stem', 'options', 'answer', 'explanation'}
               for q in instruction['questions'])
    assert 'PRIVATE_AUTHOR_REASON' not in dumps(sent)
    assert 'difficulty_plan' not in dumps(sent) and 'difficulty_design' not in dumps(sent)


def test_missed_target_after_three_rounds_keeps_reviewed_content_with_disclosure(run_case):
    job, evidence, content, wire = run_case(mode='false_hard')
    assert job['status'] == 'succeeded' and content
    assert len(wire.generations) == len(wire.assessments) == 3
    assert len(evidence['attempts']) == 3
    decision = content['config']['difficulty_acceptance']
    assert decision['status'] == 'adjusted' and decision['strict_passed'] is False
    assert decision['acceptance_reason'] == 'any_level'
    assert decision['rounds'] == decision['accepted_attempt'] == 3
    assert decision['items'][0]['target_difficulty'] == 'hard'
    assert decision['items'][0]['assessed_difficulty'] == 'easy'
    assert content['asset']['questions'][0]['difficulty'] == 'hard'
    for attempt in evidence['attempts']:
        assert attempt['response']['questions'][0]['difficulty'] == 'hard'
        assert attempt['assessment']['response']['items'][0]['assessed_difficulty'] == 'easy'
        assert attempt['assessment']['status'] in ('failed', 'adjusted')
        assert attempt['validation']['rule'] == 'difficulty_assessment_failed'
    repair = wire.generations[1]['messages'][-1]['content']
    assert 'PRIVATE_ASSESSMENT_REASON' not in repair and 'PRIVATE_AUTHOR_REASON' not in repair
    assert 'difficulty_mismatch' in repair


def test_repair_regenerates_then_independently_rechecks(run_case):
    job, evidence, content, wire = run_case(mode='repair_once')
    assert job['status'] == 'succeeded' and content
    assert len(wire.generations) == len(wire.assessments) == 2
    assert [attempt['assessment']['status'] for attempt in evidence['attempts']] == ['failed', 'passed']
    assert STEMS['hard'] in content['asset']['questions'][0]['stem']


@pytest.mark.parametrize('mode, reason', [
    ('low_confidence', 'low_confidence'), ('unanswerable', 'not_answerable_from_sources'),
    ('ambiguous', 'ambiguous_question'),
])
def test_uncertain_unsupported_or_ambiguous_judgement_fails_closed(run_case, mode, reason):
    job, evidence, content, wire = run_case(mode=mode)
    assert job['status'] == 'failed' and content is None
    assert len(wire.generations) == len(wire.assessments) == 3
    assert len(evidence['attempts']) == 3
    assert reason in evidence['attempts'][-1]['validation']['items'][0]['reasons']


@pytest.mark.parametrize('mode', ['judge_missing_slot', 'judge_duplicate_slot', 'judge_extra_slot',
                                  'judge_coerced_bool', 'judge_invalid_json', 'judge_http_error'])
def test_malformed_or_failed_assessment_never_retries_generation(run_case, mode):
    job, evidence, content, wire = run_case(mode=mode)
    assert job['status'] == 'failed' and '难度复核' in job['error']
    assert content is None and len(wire.generations) == len(wire.assessments) == 1
    assert len(evidence['attempts']) == 1
    assessment = evidence['attempts'][0]['assessment']
    assert assessment['status'] == ('provider_error' if mode == 'judge_http_error' else 'invalid_response')
    assert 'private upstream' not in dumps(evidence) and 'fixture-key' not in dumps(evidence)


@pytest.mark.parametrize('mode, rule', [
    ('wrong_label', 'difficulty_label_mismatch'), ('wrong_slot', 'difficulty_slot_mismatch'),
    ('wrong_cognitive_process', 'difficulty_cognitive_process_mismatch'),
    ('wrong_steps', 'difficulty_expected_steps_mismatch'), ('missing_design', 'difficulty_design_required'),
])
def test_structural_difficulty_failure_is_bounded_before_judgement(run_case, mode, rule):
    job, evidence, content, wire = run_case(mode=mode)
    assert job['status'] == 'failed' and content is None
    assert len(wire.generations) == 3 and wire.assessments == []
    assert len(evidence['attempts']) == 3
    assert all(attempt['validation']['rule'] == rule for attempt in evidence['attempts'])


@pytest.mark.parametrize('overrides, expected_status', [
    ({'mode': 'insufficient'}, 'insufficient_evidence'),
    ({'material': 'lesson'}, 'succeeded'),
])
def test_no_question_material_has_no_assessment_call(run_case, overrides, expected_status):
    job, evidence, content, wire = run_case(**overrides)
    assert job['status'] == expected_status
    assert len(wire.generations) == 1 and wire.assessments == []
    assert 'assessment' not in evidence['attempts'][0]
    if content:
        assert content['config']['difficulty_plan'] == [] and content['asset']['questions'] == []


def test_legacy_assets_remain_readable_but_new_generation_requires_metadata():
    q = question({'slot_id': 'q1', 'difficulty': 'easy'}, 1, ['chunk-1'])
    for field in ('slot_id', 'difficulty', 'difficulty_design'):
        q.pop(field)
    asset = LearningAsset.model_validate({'title': '旧草稿', 'evidence_sufficient': True,
        'sections': [{'heading': '资料', 'text': SOURCE['text'], 'citation_ids': ['chunk-1']}],
        'questions': [q]})
    request = GenerateRequest(course_id='course-1', topic='课程资料', count=1,
                              difficulty='easy', request_key='legacy-fixture', include_explanations=True)
    validate_asset(asset, request, [SOURCE])
    with pytest.raises(AssetRuleError):
        validate_asset(asset, request, [SOURCE], enforce_difficulty=True)
