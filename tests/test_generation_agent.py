"""Agent orchestration through isolated HTTP fixtures, never live model calls.

The fake roles return predetermined judgments. These checks prove bounded calls,
contracts, gates and recovery, not a real model's correctness or difficulty skill.
"""
import asyncio
import copy
import json
import re
from collections import Counter
from dataclasses import dataclass

import httpx
import pytest

from app.config import Endpoint, Settings
from app.generation_agent import authorize_resume
from app.models import GenerateRequest
from app.pipeline import Pipeline
from app.providers import ApiProviders
from app.store import Store, dumps, now


SOURCE = {
    'id': 'agent-source-1', 'document_id': 'agent-doc-1', 'document_name': 'fixture-course.md',
    'page': 1, 'text': 'Retrieved evidence supports course generation. Rebuild embeddings when their model changes. '
                     'Keep different courses isolated and verify generated answers against the supplied material.',
    'metadata': {'source_kind': 'text', 'extraction_method': 'fixture',
                 'extraction_status': 'ready', 'extraction_warnings': ['Fixture warning must stay visible.']},
}
STEMS = {
    'easy': 'Identify the purpose of retrieved course evidence.',
    'medium': 'Apply the indexing rule when the embedding model changes.',
    'hard': 'Design an index recovery plan under course isolation and evidence verification constraints.',
}
AUTHOR_SECRET = 'PRIVATE_AUTHOR_ANSWER'
DESIGN_SECRET = 'PRIVATE_AUTHOR_DESIGN'
REASON_SECRET = 'PRIVATE_AUTHOR_REASON'


class AgentWire:
    """Keep the network boundary and real API ledger, replacing only model output."""
    def __init__(self, *, repair_once_at=None, review_fail_at=None, fail_phase=None, cancel_phase=None,
                 requires_calculation=False, numerical_cpu=False, tool_input=None,
                 review_override=None, malformed_phase=None):
        self.contracts = []
        self.messages = []
        self.stage_counts = Counter()
        self.author_counts = Counter()
        self.repair_once_at = repair_once_at
        self.review_fail_at = review_fail_at
        self.fail_phase = fail_phase
        self.cancel_phase = cancel_phase
        self.requires_calculation = requires_calculation
        self.numerical_cpu = numerical_cpu
        self.tool_input = tool_input
        self.review_override = review_override or {}
        self.malformed_phase = malformed_phase

    @staticmethod
    def response(body):
        return httpx.Response(200, json={'choices': [
            {'finish_reason': 'stop', 'message': {'content': dumps(body)}}]})

    @staticmethod
    def identity(contract):
        if contract['task'] == 'agent_author':
            return contract['set_position'], contract['difficulty_plan'][0]['difficulty']
        stem = contract['question']['stem']
        position = int(re.search(r'Fixture (\d+)\.', stem)[1])
        level = next(level for level, text in STEMS.items() if text in stem)
        return position, level

    def __call__(self, request):
        body = json.loads(request.content)
        contract = json.loads(body['messages'][1]['content'])
        task = contract['task']
        assert task in ('agent_author', 'agent_solve', 'agent_review'), task
        position, level = self.identity(contract)
        self.contracts.append(copy.deepcopy(contract))
        self.messages.append(copy.deepcopy(body['messages']))
        self.stage_counts[(task, position)] += 1
        if self.cancel_phase == (task, position) and self.stage_counts[(task, position)] == 1:
            raise asyncio.CancelledError()
        if self.fail_phase == (task, position) and self.stage_counts[(task, position)] == 1:
            return httpx.Response(429, text='fixture transport error')
        if self.malformed_phase == task:
            return httpx.Response(200, json={'choices': [
                {'finish_reason': 'stop', 'message': {'content': '{"unfinished":'}}]})
        if task == 'agent_author':
            self.author_counts[position] += 1
            suffix = ' RR with quantum 2 and jobs A and B: compute completion times.' if self.numerical_cpu else ''
            question = {
                'slot_id': 'q1', 'difficulty': level, 'kind': 'short_answer',
                'stem': f'Fixture {position:02d}. {STEMS[level]}{suffix}', 'options': [],
                'answer': f'{AUTHOR_SECRET}_{position:02d}',
                'explanation': f'Fixture author explanation for position {position:02d}.',
                'difficulty_reason': REASON_SECRET, 'citation_ids': [SOURCE['id']],
                'difficulty_design': {
                    'cognitive_process': {'easy': 'understand', 'medium': 'apply', 'hard': 'evaluate'}[level],
                    'concepts': [DESIGN_SECRET],
                    'expected_steps': ['Verify evidence.', 'Apply the rule.', 'Justify the constrained choice.'][:
                        {'easy': 1, 'medium': 2, 'hard': 3}[level]],
                },
            }
            return self.response({
                'title': 'Agent fixture', 'evidence_sufficient': True,
                'learning_objectives': ['Use course evidence.'] if contract['request'].get('include_explanations') else [],
                'sections': ([{'heading': 'Reference', 'text': SOURCE['text'], 'citation_ids': [SOURCE['id']]}]
                             if contract['request'].get('include_explanations') else []),
                'questions': [question],
            })
        if task == 'agent_solve':
            return self.response({
                'answerable': True, 'ambiguity_free': True, 'assessed_difficulty': level,
                'confidence': 'high', 'answer': ('A' if contract['question']['kind'] == 'mcq'
                    and not self.tool_input and 'question_checks' in contract['schema'].get('properties', {})
                    else f'Independent fixture solution {position:02d}.'),
                'explanation': 'Apply the supplied course rule.',
                'requires_calculation': self.requires_calculation,
                'tool_requests': [] if self.tool_input is None else [
                    {'name': 'cpu_schedule_v1', 'input': copy.deepcopy(self.tool_input)}],
                **self.quality_fields(contract),
            })
        review = {
            'answer_correct': True, 'explanation_correct': True, 'source_supported': True,
            'ambiguity_free': True, 'tool_inputs_match_question': True, 'calculations_verified': True,
            'distinct_from_previous': True, 'confidence': 'high', 'issues': [],
            'feedback': 'Fixture gates passed.', **self.review_override,
            **self.quality_fields(contract),
        }
        if position == self.review_fail_at or (
            position == self.repair_once_at and self.stage_counts[(task, position)] == 1
        ):
            review.update(answer_correct=False, issues=['incorrect_reference_answer'],
                          feedback='Correct only this question before another independent solve.')
        return self.response(review)

    @staticmethod
    def quality_fields(contract):
        if 'question_checks' not in contract['schema'].get('properties', {}):
            return {}
        question = contract['question']
        checks = {'condition_issues': [], 'option_checks': [
            {'label': chr(65+i), 'verdict': 'correct' if i == 0 else 'incorrect', 'reason': 'Fixture option check.'}
            for i, _ in enumerate(question.get('options', []))] if question['kind'] == 'mcq' else []}
        return {'question_checks': checks, **({'explanation_issues': []}
            if 'explanation_issues' in contract['schema']['properties'] else {})}


@dataclass
class AgentRig:
    settings: Settings
    store: Store
    pipeline: Pipeline
    providers: ApiProviders
    wire: AgentWire
    job_id: str
    retrieval_calls: list

    def run(self):
        asyncio.run(self.pipeline.run(self.job_id))
        return self.job()

    def job(self):
        return self.store.one('SELECT * FROM jobs WHERE id=?', (self.job_id,))

    def evidence(self):
        return json.loads(self.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (self.job_id,))['evidence'])

    def contents(self):
        return self.store.all('SELECT * FROM contents WHERE job_id=?', (self.job_id,))

    def asset(self):
        return json.loads(self.contents()[0]['asset'])

    def text_call_count(self):
        return self.store.one("SELECT count(*) AS n FROM calls WHERE job_id=? AND capability='text'", (self.job_id,))['n']


def attach_retrieval(pipeline, calls):
    async def retrieve(request, job_id, *, with_trace=False):
        assert with_trace
        calls.append(job_id)
        call_id = pipeline.store.reserve_call(job_id, 'embedding', 'fixture-embedding', pipeline.settings.max_daily_calls)
        pipeline.store.execute("UPDATE calls SET status='succeeded' WHERE id=?", (call_id,))
        return [copy.deepcopy(SOURCE)], {'configuration': {'strategy': 'fixture'}, 'selected_count': 1}
    pipeline.retrieve = retrieve


@pytest.fixture
def agent_case(tmp_path):
    rigs = []

    def create(*, count=3, distribution=None, wire_options=None, settings_options=None,
               include_explanations=True):
        settings = Settings(data_dir=tmp_path / str(len(rigs)), generation_workflow='agent_v1',
                            max_daily_calls=1000)
        for key, value in (settings_options or {}).items():
            setattr(settings, key, value)
        settings.text = Endpoint('https://fixture.invalid/v1', 'fixture-key', 'fixture-author', '/chat/completions')
        settings.embedding = Endpoint('https://fixture.invalid/v1', 'fixture-key', 'fixture-embedding', '/embeddings')
        store = Store(settings.data_dir)
        store.execute('INSERT INTO courses VALUES(?,?,?)', ('course-1', 'Fixture course', now()))
        wire = AgentWire(**(wire_options or {}))
        providers = ApiProviders(settings, store, httpx.AsyncClient(transport=httpx.MockTransport(wire)))
        pipeline = Pipeline(settings, store, providers, enforce_account_ownership=False)
        retrieval_calls = []
        attach_retrieval(pipeline, retrieval_calls)
        request = GenerateRequest(course_id='course-1', topic='Course evidence and index recovery',
                                  difficulty='hard', difficulty_distribution=distribution,
                                  count=count, language='en', question_type='short_answer',
                                  include_explanations=include_explanations,
                                  request_key='agent-fixture-request')
        job, _ = store.job(request.request_key, 'generate', request.model_dump())
        rig = AgentRig(settings, store, pipeline, providers, wire, job['id'], retrieval_calls)
        rigs.append(rig)
        return rig

    yield create
    for rig in rigs:
        asyncio.run(rig.providers.close())


def test_each_question_gets_three_fresh_calls_and_keeps_fixed_slots_and_distribution(agent_case):
    rig = agent_case(distribution={'easy': 1, 'medium': 1, 'hard': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert [c['task'] for c in rig.wire.contracts] == ['agent_author', 'agent_solve', 'agent_review'] * 3
    assert rig.text_call_count() == 9
    assert len(rig.retrieval_calls) == 1
    questions = rig.asset()['questions']
    assert [(q['slot_id'], q['difficulty']) for q in questions] == [('q1', 'easy'), ('q2', 'medium'), ('q3', 'hard')]
    assert all(len(messages) == 2 for messages in rig.wire.messages)
    evidence = rig.evidence()
    assert evidence['agent']['call_count'] == 9
    assert evidence['agent']['status'] == 'completed'
    assert [slot['status'] for slot in evidence['agent']['slots']] == ['passed'] * 3
    for contract in rig.wire.contracts:
        assert contract['reference_chunks'][0]['extraction']['extraction_warnings'] == SOURCE['metadata']['extraction_warnings']
        assert contract['reference_chunks'][0]['original_image_provided'] is False
        if contract['task'] == 'agent_review':
            # Qualitative questions retain their separately produced solution.
            assert contract['independent_solution']['answer'].startswith('Independent fixture solution')
            assert contract['independent_solution']['explanation'] == 'Apply the supplied course rule.'


def test_failed_question_alone_is_repaired_once_and_previously_passed_work_is_kept(agent_case):
    rig = agent_case(wire_options={'repair_once_at': 2})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.text_call_count() == 12
    for task in ('agent_author', 'agent_solve', 'agent_review'):
        assert [rig.wire.stage_counts[(task, i)] for i in (1, 2, 3)] == [1, 2, 1]
    slots = rig.evidence()['agent']['slots']
    assert [[a['status'] for a in slot['attempts']] for slot in slots] == [['passed'], ['rejected', 'passed'], ['passed']]
    repairs = [c for c in rig.wire.contracts if 'repair_feedback' in c]
    assert len(repairs) == 1 and repairs[0]['set_position'] == 2
    assert 'answer_correct' in repairs[0]['repair_feedback']['issues']
    assert len(rig.asset()['questions']) == 3


def test_repeated_wrong_reference_answer_never_publishes_partial_set(agent_case):
    rig = agent_case(wire_options={'review_fail_at': 2})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert rig.text_call_count() == 12
    assert rig.wire.author_counts == {1: 1, 2: 3}
    state = rig.evidence()['agent']
    assert state['status'] == 'quality_failed'
    assert [slot['status'] for slot in state['slots']] == ['passed', 'failed', 'pending']
    assert state['slots'][0]['accepted_asset']['questions'][0]['slot_id'] == 'q1'
    assert all('answer_correct' in attempt['feedback']['issues'] for attempt in state['slots'][1]['attempts'])
    authorize_resume(rig.store, rig.job_id)
    resumed = rig.evidence()['agent']
    assert resumed['slots'][0] == state['slots'][0]
    assert resumed['slots'][1]['attempts'] == state['slots'][1]['attempts']
    assert resumed['slots'][1]['repair_extensions'][0]['rounds'] == 3
    assert not rig.contents()
    assert rig.text_call_count() == 12


def test_solver_never_receives_author_answer_target_or_design(agent_case):
    rig = agent_case(count=2)
    assert rig.run()['status'] == 'succeeded'
    for contract, messages in zip(rig.wire.contracts, rig.wire.messages):
        if contract['task'] != 'agent_solve':
            continue
        assert set(contract['question']) == {'kind', 'stem', 'options'}
        assert not {'request', 'difficulty', 'difficulty_plan', 'author_design', 'difficulty_design'} & set(contract)
        assert set(contract['rubric']) == {'easy', 'medium', 'hard'}
        serialized = dumps(messages)
        for secret in (AUTHOR_SECRET, DESIGN_SECRET, REASON_SECRET):
            assert secret not in serialized
        assert 'difficulty_plan' not in serialized and 'difficulty_design' not in serialized


@pytest.mark.parametrize('wire_options', [
    {'requires_calculation': True},
    {'requires_calculation': False, 'numerical_cpu': True},
])
def test_calculation_without_supported_tool_fails_before_review(agent_case, wire_options):
    rig = agent_case(count=1, wire_options=wire_options)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert rig.text_call_count() == 6  # author + solve, three rounds; no paid review
    assert not any(c['task'] == 'agent_review' for c in rig.wire.contracts)
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert len(attempts) == 3
    assert all('calculation_without_supported_tool' in a['feedback']['issues'] for a in attempts)


def test_invalid_calculation_inputs_fail_before_review(agent_case):
    rig = agent_case(count=1, wire_options={'requires_calculation': True, 'tool_input': {'policy': 'arbitrary-code'}})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert rig.text_call_count() == 6
    assert all('invalid_tool_input' in a['feedback']['issues'] for a in rig.evidence()['agent']['slots'][0]['attempts'])


@pytest.mark.parametrize('binding_matches', [True, False])
def test_tool_output_is_persisted_and_review_must_accept_its_question_binding(agent_case, binding_matches):
    # Tool arithmetic is real here; the model's binding judgment remains a preset fixture.
    tool_input = {
        'policy': 'rr', 'processes': [{'id': 'A', 'arrival': 0, 'burst': 2}, {'id': 'B', 'arrival': 0, 'burst': 1}],
        'quantum': 2, 'switch_cost': 0, 'boundary': 'before', 'during_switch': 'tail', 'early_finish_free': False,
    }
    rig = agent_case(count=1, wire_options={
        'requires_calculation': True, 'tool_input': tool_input,
        'review_override': {'tool_inputs_match_question': binding_matches},
    }, settings_options={'agent_max_repairs': 0})
    assert rig.run()['status'] == ('succeeded' if binding_matches else 'failed')
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    tool = attempt['tool_results'][0]
    assert tool['request']['input'] == tool_input
    assert tool['result']['timeline'] == [
        {'kind': 'run', 'process_id': 'A', 'start': 0, 'end': 2},
        {'kind': 'run', 'process_id': 'B', 'start': 2, 'end': 3},
    ]
    assert tool['result']['averages']['turnaround'] == {'numerator': 5, 'denominator': 2}
    review = next(c for c in rig.wire.contracts if c['task'] == 'agent_review')
    assert review['tool_results'] == attempt['tool_results']
    assert not {'answer', 'explanation'} & review['independent_solution'].keys()
    assert 'Independent fixture solution' not in dumps(review)
    assert review['independent_solution']['requires_calculation'] is True
    assert bool(rig.contents()) is binding_matches
    if not binding_matches:
        assert 'tool_inputs_match_question' in attempt['feedback']['issues']
        assert 'tool_results' not in attempt['feedback']
        assert 'review' not in attempt['feedback']
        assert 'do not reuse' in attempt['feedback']['repair_action']


def test_nonempty_review_issues_reject_even_when_every_boolean_gate_is_true(agent_case):
    rig = agent_case(count=1, wire_options={'review_override': {'issues': ['The stated answer key is incorrect.']}},
                     settings_options={'agent_max_repairs': 0})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    response = attempt['review']['response']
    assert all(value for value in response.values() if type(value) is bool)
    assert response['issues'] == ['The stated answer key is incorrect.']
    assert 'review_reported_issues' in attempt['feedback']['issues']
    assert rig.text_call_count() == 9  # New jobs retain three rounds even when max_repairs is zero.


def test_large_tool_timelines_stay_in_evidence_but_not_review_or_repair_prompts(agent_case):
    tool_input = {
        'policy': 'rr', 'processes': [{'id': chr(65 + i), 'arrival': 0, 'burst': 100} for i in range(8)],
        'quantum': 1, 'switch_cost': 0, 'boundary': 'before', 'during_switch': 'tail', 'early_finish_free': False,
    }
    rig = agent_case(count=1, wire_options={'requires_calculation': True, 'tool_input': tool_input, 'repair_once_at': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert [attempt['status'] for attempt in attempts] == ['rejected', 'passed']
    for attempt in attempts:
        full = attempt['tool_results'][0]['result']
        assert len(full['timeline']) == 800
        assert 'timeline_omitted' not in full
    reviews = [c for c in rig.wire.contracts if c['task'] == 'agent_review']
    repair = next(c for c in rig.wire.contracts if 'repair_feedback' in c)
    compact_views = [c['tool_results'][0]['result'] for c in reviews]
    compact_views.append(repair['repair_feedback']['tool_results'][0]['result'])
    compact_views.append(attempts[0]['feedback']['tool_results'][0]['result'])
    for compact in compact_views:
        assert 'timeline' not in compact
        assert compact['timeline_omitted']['interval_count'] == 800
        assert 'cannot be verified' in compact['timeline_omitted']['reason']
        assert 'must be rejected' in compact['timeline_omitted']['reason']
        assert compact['per_process'] == attempts[0]['tool_results'][0]['result']['per_process']
    assert rig.text_call_count() == 6
    assert max(len(dumps(messages)) for messages in rig.wire.messages) < 48000


@pytest.mark.parametrize('review_override', [
    {'answer_correct': False}, {'explanation_correct': False}, {'source_supported': False},
    {'calculations_verified': False}, {'confidence': 'low'},
])
def test_review_gates_cannot_be_bypassed_by_other_positive_flags(agent_case, review_override):
    rig = agent_case(count=1, wire_options={'review_override': review_override}, settings_options={'agent_max_repairs': 0})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'quality_failed'
    assert rig.text_call_count() == 9 and not rig.contents()


@pytest.mark.parametrize('settings_options', [
    {'agent_max_calls': 6}, {'max_daily_calls': 6},
])
def test_insufficient_minimum_budget_stops_before_retrieval_and_text_calls(agent_case, settings_options):
    rig = agent_case(count=2, settings_options=settings_options)
    assert rig.run()['status'] == 'failed'
    assert rig.retrieval_calls == [] and rig.wire.contracts == []
    assert rig.store.one('SELECT count(*) AS n FROM calls')['n'] == 0
    assert rig.evidence()['agent']['call_count'] == 0 and not rig.contents()


@pytest.mark.parametrize('settings_options,expected_status', [
    ({'agent_max_calls': 7}, 'call_limit'),
    ({'max_daily_calls': 7}, 'call_limit'),
])
def test_runtime_budget_does_not_make_an_extra_repair_call(agent_case, settings_options, expected_status):
    rig = agent_case(count=2, wire_options={'review_fail_at': 2}, settings_options=settings_options)
    assert rig.run()['status'] == 'failed'
    assert rig.text_call_count() == len(rig.wire.contracts) == 6
    assert len(rig.retrieval_calls) == 1
    assert rig.store.one('SELECT count(*) AS n FROM calls')['n'] == 7
    state = rig.evidence()['agent']
    assert state['status'] == expected_status and state['call_count'] == 6
    assert state['slots'][0]['status'] == 'passed' and not rig.contents()


@pytest.mark.parametrize('phase', ['agent_author', 'agent_solve', 'agent_review'])
def test_transport_failure_resumes_only_the_uncached_phase_with_explicit_authorization(agent_case, phase):
    rig = agent_case(count=2, wire_options={'fail_phase': (phase, 2)})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'provider_error'
    assert rig.evidence()['agent']['slots'][0]['status'] == 'passed'
    calls_before = len(rig.wire.contracts)
    rig.run()  # Failed jobs cannot spend more calls by being queued accidentally in memory.
    assert len(rig.wire.contracts) == calls_before
    authorize_resume(rig.store, rig.job_id)
    assert rig.job()['status'] == 'queued'
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.text_call_count() == 7  # six useful calls, one failed provider request
    assert len(rig.retrieval_calls) == 1
    assert rig.evidence()['agent']['resume_count'] == 1
    for task in ('agent_author', 'agent_solve', 'agent_review'):
        assert rig.wire.stage_counts[(task, 1)] == 1
        assert rig.wire.stage_counts[(task, 2)] == (2 if task == phase else 1)
    stage = {'agent_author': 'author', 'agent_solve': 'solve', 'agent_review': 'review'}[phase]
    history = rig.evidence()['agent']['slots'][1]['attempts'][0][stage]['history']
    assert [item['status'] for item in history] == ['provider_error', 'completed']
    assert [item['resume_count'] for item in history] == [0, 1]


def test_resume_does_not_reset_job_budget_when_daily_ledger_date_changes(agent_case):
    rig = agent_case(count=2, wire_options={'fail_phase': ('agent_solve', 2)},
                     settings_options={'agent_max_calls': 7})
    assert rig.run()['status'] == 'failed'
    assert rig.text_call_count() == 5
    # An older day no longer consumes today's quota, but still consumed this job's budget.
    rig.store.execute("UPDATE calls SET created_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (rig.job_id,))
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert rig.text_call_count() == 6
    assert rig.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (rig.job_id,))['n'] == 7
    assert rig.wire.stage_counts[('agent_review', 2)] == 0
    assert not rig.contents()


def test_cancelled_in_flight_call_requires_resume_and_keeps_completed_work(agent_case):
    rig = agent_case(count=2, wire_options={'cancel_phase': ('agent_solve', 2)})
    with pytest.raises(asyncio.CancelledError):
        rig.run()
    assert rig.job()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['status'] == 'running'
    assert state['slots'][0]['status'] == 'passed'
    history = state['slots'][1]['attempts'][0]['solve']['history']
    assert [item['status'] for item in history] == ['in_flight']
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    history = rig.evidence()['agent']['slots'][1]['attempts'][0]['solve']['history']
    assert [item['status'] for item in history] == ['in_flight', 'completed']
    assert rig.text_call_count() == 7 and len(rig.retrieval_calls) == 1
    assert rig.wire.author_counts == {1: 1, 2: 1}


def test_fresh_pipeline_uses_durable_author_and_passed_slot_checkpoints(agent_case):
    rig = agent_case(count=2, wire_options={'fail_phase': ('agent_solve', 2)})
    assert rig.run()['status'] == 'failed'
    fresh_store = Store(rig.settings.data_dir)
    fresh_wire = AgentWire()
    fresh_providers = ApiProviders(rig.settings, fresh_store, httpx.AsyncClient(transport=httpx.MockTransport(fresh_wire)))
    fresh_pipeline = Pipeline(rig.settings, fresh_store, fresh_providers)
    new_retrieval_calls = []
    attach_retrieval(fresh_pipeline, new_retrieval_calls)
    authorize_resume(fresh_store, rig.job_id)
    try:
        asyncio.run(fresh_pipeline.run(rig.job_id))
    finally:
        asyncio.run(fresh_providers.close())
    assert rig.job()['status'] == 'succeeded'
    assert [c['task'] for c in fresh_wire.contracts] == ['agent_solve', 'agent_review']
    assert new_retrieval_calls == []
    assert len(rig.contents()) == 1


def test_switching_checkpointed_agent_job_to_legacy_cannot_overwrite_evidence(agent_case):
    rig = agent_case(count=1, wire_options={'fail_phase': ('agent_solve', 1)})
    assert rig.run()['status'] == 'failed'
    authorize_resume(rig.store, rig.job_id)
    before = rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (rig.job_id,))['evidence']
    calls_before = rig.text_call_count()
    rig.settings.generation_workflow = 'legacy_v3'
    assert rig.run()['status'] == 'failed'
    assert '不能降级重做或覆盖检查点' in rig.job()['error']
    assert rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (rig.job_id,))['evidence'] == before
    assert rig.text_call_count() == calls_before
    assert len(rig.retrieval_calls) == 1
    assert not rig.contents()


def test_rerunning_same_job_after_publish_is_content_idempotent(agent_case):
    rig = agent_case(count=1)
    assert rig.run()['status'] == 'succeeded'
    content_id = rig.contents()[0]['id']
    rig.store.execute("UPDATE jobs SET status='queued',result=NULL WHERE id=?", (rig.job_id,))
    fresh_pipeline = Pipeline(rig.settings, Store(rig.settings.data_dir), rig.providers)
    retrieval_calls = []
    attach_retrieval(fresh_pipeline, retrieval_calls)
    asyncio.run(fresh_pipeline.run(rig.job_id))
    assert rig.job()['status'] == 'succeeded'
    assert json.loads(rig.job()['result'])['content_id'] == content_id
    assert len(rig.contents()) == 1
    assert rig.store.one('SELECT count(*) AS n FROM revisions WHERE content_id=?', (content_id,))['n'] == 1
    assert rig.text_call_count() == 3 and retrieval_calls == []


@pytest.mark.parametrize('phase,expected_calls', [('agent_solve', 2), ('agent_review', 3)])
def test_malformed_verifier_response_stops_without_reauthoring(agent_case, phase, expected_calls):
    rig = agent_case(count=1, wire_options={'malformed_phase': phase})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert rig.text_call_count() == expected_calls and not rig.contents()
    assert rig.wire.author_counts[1] == 1
    with pytest.raises(ValueError):
        authorize_resume(rig.store, rig.job_id)


def test_fifty_questions_use_single_question_schemas_and_bounded_history(agent_case):
    rig = agent_case(count=50, distribution={'easy': 20, 'medium': 15, 'hard': 15})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.text_call_count() == 150
    questions = rig.asset()['questions']
    assert [q['slot_id'] for q in questions] == [f'q{i}' for i in range(1, 51)]
    assert Counter(q['difficulty'] for q in questions) == {'easy': 20, 'medium': 15, 'hard': 15}
    authors = [c for c in rig.wire.contracts if c['task'] == 'agent_author']
    assert len(authors) == 50
    for position, contract in enumerate(authors, 1):
        assert contract['request']['count'] == 1
        assert contract['request']['difficulty_distribution'] is None
        assert contract['difficulty_plan'] == [{'slot_id': 'q1', 'difficulty': questions[position - 1]['difficulty']}]
        assert contract['set_position'] == position
        assert contract['schema']['properties']['questions']['maxItems'] == 1
        assert contract['schema']['properties']['sections']['maxItems'] == 2
        assert len(contract['recent_question_stems']) <= 8
        assert AUTHOR_SECRET not in dumps(contract) and DESIGN_SECRET not in dumps(contract)
        assert contract['reference_chunks'] == authors[0]['reference_chunks']
    author_sizes = [len(dumps(c)) for c in authors]
    assert max(author_sizes) - min(author_sizes) < 1500
    assert max(len(dumps(messages)) for messages in rig.wire.messages) <= rig.settings.agent_context_chars
    assert rig.evidence()['agent']['call_count'] == 150


def test_context_limit_fails_before_first_author_network_call(agent_case):
    rig = agent_case(count=1, settings_options={'agent_context_chars': 100})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'context_limit'
    assert rig.text_call_count() == 0 and rig.wire.contracts == []
    assert not rig.contents()
