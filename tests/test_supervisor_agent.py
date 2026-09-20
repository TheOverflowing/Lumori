"""Supervisor/worker integration over mocked HTTP; no model or Harness process.

These fixtures verify persistence, isolation, budgets and gates, not real model
quality, semantic diversity or parallel speedup.
"""
import asyncio
import copy
import json

import httpx
import pytest

from app.generation_agent import authorize_resume, fingerprint
from app.pipeline import Pipeline
from app.providers import ApiProviders
from app.store import Store, dumps, now
from test_generation_agent import AgentWire, SOURCE, agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401


@pytest.fixture
def planner_wire(monkeypatch):
    options = {}
    original = AgentWire.__call__

    def respond(self, request):
        messages = json.loads(request.content)['messages']
        contract = json.loads(messages[1]['content'])
        if contract['task'] != 'agent_plan':
            response = original(self, request)
            if response.status_code == 200 and contract['task'] == 'agent_author':
                value = json.loads(response.json()['choices'][0]['message']['content'])
                # Existing author fixtures use one source; bind the fake answer
                # to the assigned view so the controller must enforce that view.
                assigned = contract['reference_chunks'][0]
                value['questions'][0]['citation_ids'] = [assigned['id']]
                value['sections'][0].update(text=assigned['text'], citation_ids=[assigned['id']])
                if options.get('obey_kind') and contract['request']['question_type'] == 'mcq':
                    value['questions'][0].update(kind='mcq', options=['First', 'Second', 'Third', 'Fourth'], answer='A')
                if options.get('author_mutation'):
                    options['author_mutation'](value, contract)
                return self.response(value)
            if response.status_code == 200 and contract['task'] == 'agent_review' and options.get('review_mutation'):
                value = json.loads(response.json()['choices'][0]['message']['content'])
                options['review_mutation'](value, contract)
                return self.response(value)
            return response
        first = int(contract['question_slots'][0]['slot_id'][1:])
        self.contracts.append(copy.deepcopy(contract))
        self.messages.append(copy.deepcopy(messages))
        self.stage_counts[('agent_plan', first)] += 1
        if options.get('fail_plan_at') == first and self.stage_counts[('agent_plan', first)] == 1:
            return httpx.Response(429, text='fixture planner interrupted')
        value = {'questions': [
            {'slot_id': slot['slot_id'], 'difficulty': slot['difficulty'],
             'kind': options.get('kinds', {}).get(slot['slot_id'], slot.get('kind', 'short_answer')),
             'focus': 'BRIEF_ONLY_FOCUS_' + slot['slot_id'],
             'learning_goal': 'BRIEF_ONLY_LEARNING_GOAL_' + slot['slot_id'],
             'requirements': ['BRIEF_ONLY_REQUIREMENT_' + slot['slot_id']],
             'source_ids': options.get('sources', {}).get(slot['slot_id'], [SOURCE['id']])}
            for slot in contract['question_slots']]}
        if options.get('plan_mutation'):
            options['plan_mutation'](value, contract)
        return self.response(value)

    monkeypatch.setattr(AgentWire, '__call__', respond)
    return options


def supervisor(agent_case, **kwargs):
    settings = {'agent_runtime': 'deepseek_harness', 'agent_orchestration': 'supervisor_v1'}
    settings.update(kwargs.pop('settings_options', {}))
    return agent_case(settings_options=settings, **kwargs)


def update_request(rig, **updates):
    payload = json.loads(rig.job()['payload']) | updates
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))


def test_plan_three_isolated_workers_then_deterministic_assembly(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, distribution={'easy': 1, 'medium': 1, 'hard': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert offline_harness == ['plan'] + ['author', 'solve', 'review'] * 3
    evidence = rig.evidence()
    assert evidence['schema_version'] == 'education-agent-v1'
    assert evidence['configuration']['orchestration']['dispatch'] == 'serial_isolated_workers'
    assert evidence['agent']['call_count'] == rig.text_call_count() == 10
    assert rig.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (rig.job_id,))['n'] == 11
    slots = evidence['agent']['slots']
    assert [s['worker']['status'] for s in slots] == ['completed'] * 3
    assert [s['worker']['dispatch_count'] for s in slots] == [1] * 3
    assert len({s['worker']['id'] for s in slots}) == 3
    assert [(q['slot_id'], q['difficulty']) for q in rig.asset()['questions']] == [
        ('q1', 'easy'), ('q2', 'medium'), ('q3', 'hard')]
    assert rig.asset()['questions'] == [s['accepted_asset']['questions'][0] for s in slots]
    state = evidence['agent']['supervisor']
    assert state['status'] == 'completed'
    assert state['assembled_asset_sha256'] == fingerprint(rig.asset())
    assert [item['asset_sha256'] for item in state['assembly_inputs']] == [
        fingerprint(s['accepted_asset']) for s in slots]
    for slot in slots:
        for phase in ('author', 'solve', 'review'):
            assert slot['attempts'][0][phase]['history'][0]['worker_id'] == slot['worker']['id']


def test_worker_source_views_do_not_replace_root_evidence(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=2)
    second = copy.deepcopy(SOURCE) | {'id': 'agent-source-2', 'document_id': 'agent-doc-2', 'text': 'Second course excerpt.'}
    original = rig.pipeline.retrieve

    async def retrieve(*args, **kwargs):
        sources, trace = await original(*args, **kwargs)
        return sources + [second], trace

    rig.pipeline.retrieve = retrieve
    planner_wire['sources'] = {'q2': [second['id']]}
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert [s['id'] for s in rig.evidence()['sources']] == [SOURCE['id'], second['id']]
    assert [s['id'] for s in json.loads(rig.contents()[0]['sources'])] == [SOURCE['id'], second['id']]
    for contract in rig.wire.contracts:
        if contract['task'] == 'agent_plan':
            assert len(contract['reference_chunks']) == 2
            continue
        position, _ = AgentWire.identity(contract)
        expected = SOURCE['id'] if position == 1 else second['id']
        assert [s['id'] for s in contract['reference_chunks']] == [expected]
        assert contract['reference_chunks'][0]['extraction']['extraction_warnings'] == SOURCE['metadata']['extraction_warnings']
    assert len(rig.evidence()['agent']['supervisor']['batches']) == 1
    assert [s['status'] for s in rig.evidence()['agent']['slots']] == ['passed', 'passed']


def test_full_brief_is_author_only_solve_blind_and_review_receives_only_requirements(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=1)
    assert rig.run()['status'] == 'succeeded'
    author = next(c for c in rig.wire.contracts if c['task'] == 'agent_author')
    assert author['question_brief']['slot_id'] == 'q1'
    for contract in rig.wire.contracts:
        if contract['task'] in ('agent_solve', 'agent_review'):
            assert 'question_brief' not in contract
        if contract['task'] == 'agent_solve':
            assert 'BRIEF_ONLY_' not in dumps(contract)
            assert 'difficulty' not in contract['question']
            assert 'difficulty_design' not in contract['question']
            assert 'answer' not in contract['question']
        if contract['task'] == 'agent_review':
            assert contract['assignment_requirements'] == {
                k: author['question_brief'][k] for k in ('focus', 'learning_goal', 'requirements')}
            assert 'difficulty' not in contract['assignment_requirements']
    assert all(len(m) == 2 for m in rig.wire.messages)


@pytest.mark.parametrize('obey_kind', [False, True])
def test_mixed_request_still_enforces_the_assigned_kind(agent_case, offline_harness, planner_wire, obey_kind):
    rig = supervisor(agent_case, count=1, settings_options={'agent_max_repairs': 0})
    update_request(rig, question_type='mixed')
    planner_wire.update(kinds={'q1': 'mcq'}, obey_kind=obey_kind)
    assert rig.run()['status'] == ('succeeded' if obey_kind else 'failed'), rig.job()
    author = next(c for c in rig.wire.contracts if c['task'] == 'agent_author')
    assert author['request']['question_type'] == 'mcq'
    if obey_kind:
        assert rig.asset()['questions'][0]['kind'] == 'mcq'
    else:
        attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
        assert attempt['feedback']['validation']['rule'] == 'question_kind_mismatch'
        assert offline_harness == ['plan'] + ['author'] * 3
        assert not rig.contents()


@pytest.mark.parametrize('mutation', ['count', 'difficulty', 'source', 'slot', 'extra', 'requirement_length'])
def test_invalid_plan_spends_no_worker_calls(agent_case, offline_harness, planner_wire, mutation):
    rig = supervisor(agent_case, count=2)

    def alter(value, _):
        if mutation == 'count':
            value['questions'].pop()
        elif mutation == 'extra':
            value['answer'] = 'untrusted answer'
        elif mutation == 'requirement_length':
            value['questions'][0]['requirements'][0] = 'x' * 310
        else:
            field, bad = {'difficulty': ('difficulty', 'easy'), 'source': ('source_ids', ['foreign-source']),
                          'slot': ('slot_id', 'q2')}[mutation]
            value['questions'][0][field] = bad

    planner_wire['plan_mutation'] = alter
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan']
    assert rig.text_call_count() == 1 and not rig.contents()
    state = rig.evidence()['agent']
    assert state['status'] == 'protocol_failure'
    assert all(not s['attempts'] for s in state['slots'])
    assert state['supervisor']['batches'][0]['plan']['response']


def test_cpu_planner_policy_boundary_matches_compiler_schema(agent_case, offline_harness, planner_wire):
    from app.cpu_problem_spec import cpu_spec_contract
    rig = supervisor(agent_case, count=2, settings_options={'agent_question_spec': 'cpu_schedule_v1'})
    # Stop immediately after capturing the planner contract; no CPU author mock
    # is needed to inspect the real planning path.
    planner_wire['plan_mutation'] = lambda value, contract: value['questions'].clear()
    assert rig.run()['status'] == 'failed'
    contract = next(c for c in rig.wire.contracts if c['task'] == 'agent_plan')
    capabilities = contract['worker_capabilities']
    supported = set(cpu_spec_contract()['properties']['scenarios']['items']['discriminator']['mapping'])
    assert set(capabilities['supported_policies']) == supported == {'rr', 'stcf'}
    assert {'sjf', 'fcfs', 'mlfq'} <= set(capabilities['unsupported_policies'])
    assert not supported & set(capabilities['unsupported_policies'])
    assert offline_harness == ['plan']
    assert all(not slot['attempts'] for slot in rig.evidence()['agent']['slots'])


@pytest.mark.parametrize('limit_field', ['agent_max_calls', 'max_daily_calls'])
def test_preflight_includes_planning_call(agent_case, offline_harness, planner_wire, limit_field):
    rig = supervisor(agent_case, count=2, settings_options={limit_field: 7})
    assert rig.run()['status'] == 'failed'
    assert rig.retrieval_calls == [] and offline_harness == []
    assert rig.store.one('SELECT count(*) AS n FROM calls')['n'] == 0


def test_shared_budget_covers_planner_and_workers_and_rejected_attempts(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=2, wire_options={'review_fail_at': 2},
                     settings_options={'agent_max_calls': 8})
    assert rig.run()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['status'] == 'call_limit'
    assert state['slots'][0]['status'] == 'passed'
    assert state['slots'][1]['attempts'][0]['status'] == 'rejected'
    assert rig.text_call_count() == 7
    assert rig.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (rig.job_id,))['n'] == 8
    assert not rig.contents()


def test_resume_in_fresh_pipeline_reuses_plan_and_accepted_worker(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=3, wire_options={'fail_phase': ('agent_review', 2)})
    assert rig.run()['status'] == 'failed'
    before = rig.evidence()
    assert before['agent']['slots'][0]['worker']['status'] == 'completed'
    authorize_resume(rig.store, rig.job_id)
    fresh_store = Store(rig.settings.data_dir)
    fresh_wire = AgentWire()
    providers = ApiProviders(rig.settings, fresh_store, httpx.AsyncClient(transport=httpx.MockTransport(fresh_wire)))
    pipeline = Pipeline(rig.settings, fresh_store, providers, enforce_account_ownership=False)
    try:
        asyncio.run(pipeline.run(rig.job_id))
    finally:
        asyncio.run(providers.close())
    assert rig.job()['status'] == 'succeeded', rig.job()
    assert [c['task'] for c in fresh_wire.contracts] == ['agent_review', 'agent_author', 'agent_solve', 'agent_review']
    after = rig.evidence()
    assert after['agent']['supervisor']['batches'][0]['plan'] == before['agent']['supervisor']['batches'][0]['plan']
    assert after['agent']['slots'][0] == before['agent']['slots'][0]
    assert [s['worker']['dispatch_count'] for s in after['agent']['slots']] == [1, 2, 1]
    assert rig.text_call_count() == 11
    assert len(rig.retrieval_calls) == 1
    history = after['agent']['slots'][1]['attempts'][0]['review']['history']
    assert [h['status'] for h in history] == ['provider_error', 'completed']
    assert [h['resume_count'] for h in history] == [0, 1]


def test_nine_question_plan_resumes_only_interrupted_batch(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=9)
    planner_wire['fail_plan_at'] = 9
    assert rig.run()['status'] == 'failed'
    first = copy.deepcopy(rig.evidence()['agent']['supervisor']['batches'][0])
    assert offline_harness == ['plan', 'plan']
    assert not rig.wire.author_counts
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    state = rig.evidence()['agent']
    assert state['supervisor']['batches'][0] == first
    assert rig.wire.stage_counts[('agent_plan', 1)] == 1
    assert rig.wire.stage_counts[('agent_plan', 9)] == 2
    plans = [c for c in rig.wire.contracts if c['task'] == 'agent_plan']
    assert [len(c['question_slots']) for c in plans] == [8, 1, 1]
    assert len(plans[-1]['previous_briefs']) == 8
    assert 'BRIEF_ONLY_REQUIREMENT_' not in dumps(plans[-1]['previous_briefs'])
    assert [q['slot_id'] for q in rig.asset()['questions']] == [f'q{i}' for i in range(1, 10)]
    assert rig.text_call_count() == 30  # 27 roles, two useful plans, one failed request.


def test_duplicate_brief_across_batches_is_rejected_before_any_worker(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=2, settings_options={'agent_plan_batch_size': 1})

    def duplicate(value, _):
        for brief in value['questions']:
            brief.update(focus='SAME focus', learning_goal='same goal')

    planner_wire['plan_mutation'] = duplicate
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'plan'] and not rig.wire.author_counts
    assert rig.evidence()['agent']['supervisor']['batches'][1]['status'] == 'invalid_plan'
    assert not rig.contents()


def test_worker_cannot_cite_unassigned_root_source(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=1, settings_options={'agent_max_repairs': 0})
    second = copy.deepcopy(SOURCE) | {'id': 'root-unassigned-source'}
    original = rig.pipeline.retrieve

    async def retrieve(*args, **kwargs):
        sources, trace = await original(*args, **kwargs)
        return sources + [second], trace

    rig.pipeline.retrieve = retrieve
    planner_wire['author_mutation'] = lambda value, _: value['questions'][0].update(citation_ids=[second['id']])
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan'] + ['author'] * 3
    assert len(rig.evidence()['sources']) == 2
    assert not rig.contents()


def test_existing_answer_review_gate_still_blocks_assembly(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=2, wire_options={'review_fail_at': 2},
                     settings_options={'agent_max_repairs': 0})
    assert rig.run()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['slots'][0]['status'] == 'passed'
    assert state['slots'][1]['worker']['status'] == 'quality_failed'
    assert state['slots'][1]['attempts'][0]['quality_checks']['correctness']['status'] == 'failed'
    assert 'answer_correct' in state['slots'][1]['attempts'][0]['feedback']['issues']
    assert 'assembly_inputs' not in state['supervisor']
    assert not rig.contents()


def test_exact_repeat_candidate_still_fails_before_solving(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=2, settings_options={'agent_max_repairs': 0})

    def duplicate(value, contract):
        if contract['set_position'] == 2:
            value['questions'][0]['stem'] = value['questions'][0]['stem'].replace('Fixture 02.', 'Fixture 01.')

    planner_wire['author_mutation'] = duplicate
    assert rig.run()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['slots'][1]['attempts'][0]['feedback']['issues'] == ['duplicate_question']
    assert offline_harness == ['plan', 'author', 'solve', 'review'] + ['author'] * 3
    assert not rig.contents()


def test_unfulfilled_brief_blocks_delivery_even_when_all_boolean_checks_pass(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=1, settings_options={'agent_max_repairs': 0})
    planner_wire['review_mutation'] = lambda value, _: value.update(issues=['The assignment asks for a recovery constraint that the question omits.'])
    assert rig.run()['status'] == 'failed'
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert 'review_reported_issues' in attempt['feedback']['issues']
    assert not rig.contents()


def test_resume_does_not_reset_cross_day_shared_budget(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=2, wire_options={'fail_phase': ('agent_solve', 2)},
                     settings_options={'agent_max_calls': 8})
    assert rig.run()['status'] == 'failed'
    assert rig.text_call_count() == 6
    rig.store.execute("UPDATE calls SET created_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (rig.job_id,))
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert rig.wire.stage_counts[('agent_plan', 1)] == 1
    assert rig.wire.stage_counts[('agent_review', 2)] == 0
    assert rig.text_call_count() == 7
    assert not rig.contents()


@pytest.mark.parametrize('new_batch_size', [1, 10])
def test_changed_planning_configuration_cannot_reuse_checkpoint(agent_case, offline_harness, planner_wire, new_batch_size):
    rig = supervisor(agent_case, count=2, wire_options={'fail_phase': ('agent_review', 2)})
    assert rig.run()['status'] == 'failed'
    authorize_resume(rig.store, rig.job_id)
    before = copy.deepcopy(rig.evidence())
    calls = rig.text_call_count()
    rig.settings.agent_plan_batch_size = new_batch_size
    assert rig.run()['status'] == 'failed'
    assert rig.text_call_count() == calls
    assert rig.evidence() == before
    assert not rig.contents()


def attach_owned_documents(rig, source_ids):
    rig.store.execute('INSERT INTO users VALUES(?,?,?,?,?)', ('fixture-user', 'fixture@example.invalid', 'not-a-password', 'Fixture', now()))
    rig.store.execute('INSERT INTO course_owners VALUES(?,?)', ('course-1', 'fixture-user'))
    rig.store.execute('INSERT INTO job_owners VALUES(?,?)', (rig.job_id, 'fixture-user'))
    for document_id in source_ids:
        rig.store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                          (document_id, 'course-1', document_id + '.md', document_id, 'indexed', 1, now()))
    rig.pipeline.enforce_account_ownership = True


@pytest.mark.parametrize('change', ['disabled', 'deleted', 'foreign_course'])
def test_worker_checks_full_root_scope_even_for_unassigned_document(agent_case, offline_harness, planner_wire, change):
    rig = supervisor(agent_case, count=1)
    second = copy.deepcopy(SOURCE) | {'id': 'unassigned-source', 'document_id': 'unassigned-doc'}
    attach_owned_documents(rig, [SOURCE['document_id'], second['document_id']])
    original = rig.pipeline.retrieve

    async def retrieve(*args, **kwargs):
        sources, trace = await original(*args, **kwargs)
        return sources + [second], trace

    rig.pipeline.retrieve = retrieve

    def revoke_after_planning(value, contract):
        if change == 'foreign_course':
            rig.store.execute('INSERT INTO courses VALUES(?,?,?)', ('foreign-course', 'Other course', now()))
            rig.store.execute('UPDATE documents SET course_id=? WHERE id=?', ('foreign-course', second['document_id']))
        else:
            rig.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)',
                              (second['document_id'], 0, now() if change == 'deleted' else None))

    planner_wire['plan_mutation'] = revoke_after_planning
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan']
    state = rig.evidence()['agent']
    assert state['status'] == 'scope_changed'
    assert state['slots'][0]['worker']['source_ids'] == [SOURCE['id']]
    assert state['slots'][0]['worker']['status'] == 'scope_changed'
    assert len(rig.evidence()['sources']) == 2 and not rig.contents()


def test_disabling_source_after_review_prevents_assembly_publication(agent_case, offline_harness, planner_wire):
    rig = supervisor(agent_case, count=1)
    attach_owned_documents(rig, [SOURCE['document_id']])

    def disable_after_review(value, contract):
        rig.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', (SOURCE['document_id'], 0, None))

    planner_wire['review_mutation'] = disable_after_review
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review']
    assert rig.evidence()['agent']['slots'][0]['status'] == 'passed'
    assert not rig.contents()
