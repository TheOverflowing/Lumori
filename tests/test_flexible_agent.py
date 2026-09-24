"""Flexible authoring integration with preset role judgments and fake HTTP.

These checks establish controller contracts, gates and recovery. They do not
establish a model's ability to judge a humanities argument or educational quality.
"""
import copy
import json
import asyncio
import re

import httpx
import pytest

from app.cpu_problem_spec import compile_cpu_spec
from app.generation_agent import GenerationAgent, authorize_resume, fingerprint
from app import generation_agent
from app.models import GenerateRequest
from app.providers import ProviderError
from app.store import dumps
from test_cpu_spec_agent import SPEC
from test_generation_agent import AgentWire, SOURCE, STEMS, agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401
from test_supervisor_agent import attach_owned_documents


@pytest.fixture
def flexible_wire(monkeypatch):
    options = {'briefs': {}, 'specs': {}, 'current_position': 1}
    original = AgentWire.__call__

    def log(self, contract, messages, task, position):
        self.contracts.append(copy.deepcopy(contract))
        self.messages.append(copy.deepcopy(messages))
        self.stage_counts[(task, position)] += 1

    def respond(self, request):
        messages = json.loads(request.content)['messages']
        contract = json.loads(messages[1]['content'])
        task = contract['task']
        if task == 'agent_plan':
            first = int(contract['question_slots'][0]['slot_id'][1:])
            log(self, contract, messages, task, first)
            value = {'questions': [
                {'slot_id': slot['slot_id'], 'difficulty': slot['difficulty'],
                 'kind': slot.get('kind', 'short_answer'),
                 'focus': 'PLAN_ONLY_FOCUS_' + slot['slot_id'],
                 'learning_goal': 'PLAN_ONLY_GOAL_' + slot['slot_id'],
                 'answer_policy': options.get('answer_policy', 'multiple_defensible'),
                 'requirements': [
                     {'id': 'r1', 'kind': 'reasoning', 'description': 'Give a defensible argument using the course evidence.',
                      'source_ids': [SOURCE['id']]},
                     {'id': 'r2', 'kind': 'comparison', 'description': 'Compare the stated alternatives and their constraints.',
                      'source_ids': [SOURCE['id']]}],
                 'source_ids': [SOURCE['id']]}
                for slot in contract['question_slots']]}
            if options.get('plan_mutation'):
                options['plan_mutation'](value, contract)
            options['briefs'].update({b['slot_id']: copy.deepcopy(b) for b in value['questions']})
            return self.response(value)
        if task == 'agent_author_cpu_spec':
            position = contract['set_position']
            options['current_position'] = position
            log(self, contract, messages, task, position)
            self.author_counts[position] += 1
            spec = copy.deepcopy(SPEC)
            options['specs'][position] = spec
            return self.response({'evidence_sufficient': True, 'spec': spec, 'citation_ids': [SOURCE['id']]})
        if task == 'agent_compose_cpu_narrative':
            position = options['current_position']
            log(self, contract, messages, task, position)
            count = self.stage_counts[(task, position)]
            if options.get('fail_compose_at') == position and count == 1:
                return httpx.Response(429, text='fixture composition interrupted')
            brief = options['briefs'][f'q{position}']
            value = {'evidence_sufficient': True,
                     'additional_task': f'Fixture {position:02d}. {STEMS[brief["difficulty"]]} Defend the policy tradeoff.',
                     'answer': f'ADDITIONAL_DEFENSE_V{count}: A feasible choice depends on its objective and constraints.',
                     'explanation': 'The source permits different defensible priorities; compare the computed metrics.',
                     'citation_ids': [SOURCE['id']]}
            if options.get('compose_mutation'):
                options['compose_mutation'](value, contract, count)
            return self.response(value)
        if task == 'agent_solve' and 'Defend the policy tradeoff.' in contract['question']['stem']:
            position, level = AgentWire.identity(contract)
            log(self, contract, messages, task, position)
            if options.get('fail_solve_at') == position and self.stage_counts[(task, position)] == 1:
                return httpx.Response(429, text='fixture independent solving interrupted')
            compiled = compile_cpu_spec(options['specs'][position], language='en', difficulty=level, source_ids=[SOURCE['id']])
            value = {'answerable': True, 'ambiguity_free': True, 'assessed_difficulty': level,
                'confidence': 'high', 'answer': 'A DIFFERENT DEFENSIBLE INDEPENDENT POSITION',
                'explanation': 'Check the stated objectives without assuming the author conclusion.',
                'requires_calculation': True,
                'tool_requests': [copy.deepcopy(t['request']) for t in compiled['evidence']['tool_results']]}
            if 'question_checks' in contract['schema']['properties']:
                value['question_checks'] = {'condition_issues': [], 'option_checks': []}
            if options.get('solver_mutation'):
                options['solver_mutation'](value, contract, self.stage_counts[(task, position)])
            return self.response(value)
        response = original(self, request)
        if response.status_code != 200:
            return response
        if task == 'agent_review' and options.get('fail_review_call'):
            position, _ = AgentWire.identity(contract)
            if self.stage_counts[(task, position)] == options['fail_review_call']:
                return httpx.Response(429, text='fixture evidence re-evaluation interrupted')
        value = json.loads(response.json()['choices'][0]['message']['content'])
        if task == 'agent_author':
            value['questions'][0]['answer'] = 'AUTHOR DEFENDS A PRIORITY THAT DIFFERS FROM THE SOLVER.'
            if options.get('author_mutation'):
                options['author_mutation'](value, contract)
        if task == 'agent_solve':
            value['answer'] = 'SOLVER DEFENDS A DIFFERENT PRIORITY WITH VALID REASONS.'
        if task == 'agent_review':
            position, _ = AgentWire.identity(contract)
            brief = options['briefs'][f'q{position}']
            question = contract['question']
            value['requirement_checks'] = [
                {'id': req['id'], 'status': 'met', 'stem_evidence': question['stem'][:90],
                 'answer_evidence': question['answer'][:90],
                 'explanation': 'Preset fixture coverage judgment, not a real semantic evaluation.',
                 'source_ids': list(req['source_ids'])}
                for req in brief['requirements']]
            if options.get('review_mutation'):
                options['review_mutation'](value, contract, self.stage_counts[(task, position)])
        return self.response(value)

    monkeypatch.setattr(AgentWire, '__call__', respond)
    return options


def flexible_case(agent_case, *, cpu=False, **kwargs):
    settings = {'agent_runtime': 'deepseek_harness', 'agent_orchestration': 'supervisor_v2',
                'agent_question_spec': 'cpu_schedule_v1' if cpu else 'none', 'agent_max_repairs': 0}
    settings.update(kwargs.pop('settings_options', {}))
    return agent_case(settings_options=settings, **kwargs)


@pytest.mark.parametrize('material', ['quiz', 'assignment'])
def test_request_opt_in_runs_native_per_question_workers(agent_case, flexible_wire, material):
    rig = agent_case(count=2, include_explanations=False, settings_options={'agent_runtime': 'native',
                     'agent_orchestration': 'sequential_v1'})
    payload = json.loads(rig.job()['payload']) | {'material': material, 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))

    assert rig.run()['status'] == 'succeeded', rig.job()
    evidence = rig.evidence()
    assert evidence['configuration']['orchestration']['mode'] == 'supervisor_v2'
    assert evidence['configuration']['orchestration']['dispatch'] == 'bounded_parallel_workers_v1'
    assert evidence['configuration']['orchestration']['max_parallel_questions'] == 20
    assert [slot['worker']['status'] for slot in evidence['agent']['slots']] == ['completed'] * 2
    assert rig.asset()['questions'] == [slot['accepted_asset']['questions'][0]
                                      for slot in evidence['agent']['slots']]
    assert rig.asset()['sections'] == []
    assert [entry['task'] for entry in rig.wire.contracts].count('agent_plan') == 1
    assert rig.text_call_count() == 7


@pytest.mark.parametrize('limit,count', [(1, 3), (2, 3), (3, 3), (4, 4), (20, 40), (40, 40)])
def test_opt_in_question_workers_overlap_only_up_to_limit(agent_case, flexible_wire, limit, count):
    rig = agent_case(count=count, include_explanations=False, settings_options={
        'agent_runtime': 'native', 'agent_orchestration': 'sequential_v1',
        'agent_max_parallel_questions': limit})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    original = rig.providers.generate
    in_flight = peak = 0

    async def observed(messages, job_id):
        nonlocal in_flight, peak
        contract = json.loads(messages[1]['content'])
        if contract['task'] == 'agent_plan':
            return await original(messages, job_id)
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            return await original(messages, job_id)
        finally:
            in_flight -= 1

    rig.providers.generate = observed
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert peak == limit
    assert rig.text_call_count() == (count + rig.settings.agent_plan_batch_size - 1) // rig.settings.agent_plan_batch_size + 3 * count
    assert [q['slot_id'] for q in rig.asset()['questions']] == [f'q{n}' for n in range(1, count + 1)]
    assert rig.evidence()['configuration']['orchestration']['max_parallel_questions'] == limit


def test_parallel_failure_stops_new_dispatch_but_keeps_sibling_checkpoint(agent_case, flexible_wire):
    rig = agent_case(count=3, include_explanations=False,
        wire_options={'fail_phase': ('agent_author', 1)}, settings_options={
        'agent_runtime': 'native', 'agent_max_parallel_questions': 2})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    original = rig.providers.generate

    async def delayed_second(messages, job_id):
        contract = json.loads(messages[1]['content'])
        if contract['task'] == 'agent_author' and contract['set_position'] == 2:
            await asyncio.sleep(0.03)
        return await original(messages, job_id)

    rig.providers.generate = delayed_second
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    slots = rig.evidence()['agent']['slots']
    assert slots[0]['status'] != 'passed'
    assert slots[1]['status'] == 'passed'
    assert slots[2]['status'] == 'pending'
    assert not any(c.get('set_position') == 3 for c in rig.wire.contracts)


def test_sibling_failure_does_not_block_inflight_workers_own_repair(agent_case, flexible_wire):
    rig = agent_case(count=3, include_explanations=False,
        wire_options={'fail_phase': ('agent_author', 1)}, settings_options={
        'agent_runtime': 'native', 'agent_max_parallel_questions': 2})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    author_count = 0

    def first_second_author_invalid(value, contract):
        nonlocal author_count
        if contract['set_position'] == 2:
            author_count += 1
            if author_count == 1:
                value['questions'][0]['answer'] = ''

    flexible_wire['author_mutation'] = first_second_author_invalid
    original = rig.providers.generate

    async def delayed_second(messages, job_id):
        contract = json.loads(messages[1]['content'])
        if contract['task'] == 'agent_author' and contract['set_position'] == 2:
            await asyncio.sleep(0.02)
        return await original(messages, job_id)

    rig.providers.generate = delayed_second
    assert rig.run()['status'] == 'failed'
    slots = rig.evidence()['agent']['slots']
    assert slots[1]['status'] == 'passed'
    assert [a['status'] for a in slots[1]['attempts']] == ['rejected', 'passed']
    assert slots[2]['status'] == 'pending'


def test_research_full_exposure_can_finish_all_slots_without_partial_publish(agent_case, flexible_wire, monkeypatch):
    monkeypatch.setattr(GenerationAgent, 'continue_after_worker_failure', True, raising=False)
    rig = agent_case(count=3, include_explanations=False,
        wire_options={'fail_phase': ('agent_author', 1)}, settings_options={
        'agent_runtime': 'native', 'agent_max_parallel_questions': 2})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert [slot['status'] for slot in rig.evidence()['agent']['slots']] == ['pending', 'passed', 'passed']


def test_existing_serial_opt_in_checkpoint_resumes_without_parallel_conversion(agent_case, flexible_wire):
    rig = agent_case(count=2, include_explanations=False,
        wire_options={'fail_phase': ('agent_author', 1)}, settings_options={
        'agent_runtime': 'native', 'agent_max_parallel_questions': 3})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    assert rig.run()['status'] == 'failed'
    evidence = rig.evidence()
    evidence['configuration']['orchestration']['dispatch'] = 'serial_isolated_workers'
    evidence['configuration']['orchestration'].pop('max_parallel_questions')
    rig.store.save_job_evidence(rig.job_id, evidence)
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.evidence()['configuration']['orchestration']['dispatch'] == 'serial_isolated_workers'
    assert [q['slot_id'] for q in rig.asset()['questions']] == ['q1', 'q2']


def test_existing_parallel_checkpoint_keeps_frozen_limit_after_default_changes(agent_case, flexible_wire):
    rig = agent_case(count=3, include_explanations=False,
        wire_options={'fail_phase': ('agent_author', 1)}, settings_options={
        'agent_runtime': 'native', 'agent_max_parallel_questions': 4})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['configuration']['orchestration']['max_parallel_questions'] == 4

    rig.settings.agent_max_parallel_questions = 20
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.evidence()['configuration']['orchestration']['max_parallel_questions'] == 4
    assert [q['slot_id'] for q in rig.asset()['questions']] == ['q1', 'q2', 'q3']


def test_parallel_late_duplicate_is_rejected_before_assembly(agent_case, flexible_wire, monkeypatch):
    rig = agent_case(count=2, include_explanations=False, settings_options={
        'agent_runtime': 'native', 'agent_max_parallel_questions': 2})
    payload = json.loads(rig.job()['payload']) | {'material': 'quiz', 'use_subagents': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    normalize = generation_agent.normalized_stem
    monkeypatch.setattr(generation_agent, 'normalized_stem',
        lambda stem: normalize(re.sub(r'Fixture \d+\.', '', stem)))
    original = rig.providers.generate

    async def delay_second_review(messages, job_id):
        contract = json.loads(messages[1]['content'])
        if contract['task'] == 'agent_review' and AgentWire.identity(contract)[0] == 2:
            await asyncio.sleep(0.03)
        return await original(messages, job_id)

    rig.providers.generate = delay_second_review
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    slots = rig.evidence()['agent']['slots']
    assert slots[0]['status'] == 'passed'
    assert slots[1]['status'] == 'failed'
    assert any('duplicate_question' in a.get('feedback', {}).get('issues', [])
               for a in slots[1]['attempts'])


def test_request_opt_out_overrides_server_supervisor(agent_case):
    rig = agent_case(count=1, settings_options={'agent_runtime': 'native',
                     'agent_orchestration': 'supervisor_v2'})
    payload = json.loads(rig.job()['payload']) | {'use_subagents': False}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))

    assert rig.run()['status'] == 'succeeded', rig.job()
    assert 'orchestration' not in rig.evidence()['configuration']
    assert not any(entry['task'] == 'agent_plan' for entry in rig.wire.contracts)
    assert rig.text_call_count() == 3


def test_lesson_rejects_per_question_workers():
    with pytest.raises(ValueError, match='逐题子代理'):
        GenerateRequest(course_id='course-1', topic='Course evidence', material='lesson',
                        request_key='lesson-subagents', use_subagents=True)


def test_defensible_nonidentical_answers_are_not_rejected_by_string_matching(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, count=1)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert offline_harness == ['plan', 'author', 'solve', 'review']
    review = next(c for c in rig.wire.contracts if c['task'] == 'agent_review')
    assert review['question']['answer'] != review['independent_solution']['answer']
    assert rig.asset()['questions'][0]['answer'].startswith('AUTHOR DEFENDS')
    assert 'multiple_defensible' in dumps(review)
    review_messages = next(m for m in rig.wire.messages
                           if json.loads(m[1]['content'])['task'] == 'agent_review')
    assert 'one possible response, not the gold answer' in review_messages[0]['content']
    assert 'Different well-supported theses' in review_messages[0]['content']
    assert 'Do not confuse an interpretive question' in review_messages[0]['content']
    assert not any(c['task'] == 'agent_compose_cpu_narrative' for c in rig.wire.contracts)


def test_general_three_question_set_assembles_exact_verified_assets(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, count=3, distribution={'easy': 1, 'medium': 1, 'hard': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    slots = rig.evidence()['agent']['slots']
    assert rig.asset()['questions'] == [s['accepted_asset']['questions'][0] for s in slots]
    assert [q['slot_id'] for q in rig.asset()['questions']] == ['q1', 'q2', 'q3']
    assert rig.asset()['section_scope'] == 'per_question'
    assert len(rig.asset()['sections']) == 3
    for section, slot in zip(rig.asset()['sections'], slots):
        for original in slot['accepted_asset']['sections']:
            assert original['heading'] in section['text']
            assert original['text'] in section['text']
            assert set(original['citation_ids']) <= set(section['citation_ids'])
    assert rig.evidence()['agent']['supervisor']['assembled_asset_sha256'] == fingerprint(rig.asset())
    assert offline_harness == ['plan'] + ['author', 'solve', 'review'] * 3
    assert rig.text_call_count() == 10


@pytest.mark.parametrize('status', ['missing', 'partial', 'unsupported'])
def test_nonmet_requirement_cannot_be_overridden_by_all_positive_booleans(
        agent_case, offline_harness, flexible_wire, status):
    flexible_wire['review_mutation'] = lambda value, contract, n: value['requirement_checks'][0].update(status=status)
    rig = flexible_case(agent_case, count=1)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert rig.evidence()['agent']['slots'][0]['status'] != 'passed'


@pytest.mark.parametrize('mutation', ['fake_stem', 'fake_answer', 'unknown_source', 'empty_source',
                                    'missing_check', 'duplicate_check', 'unknown_check'])
def test_fabricated_or_incomplete_requirement_evidence_is_never_accepted(
        agent_case, offline_harness, flexible_wire, mutation):
    def alter(value, contract, n):
        checks = value['requirement_checks']
        if mutation == 'fake_stem':
            checks[0]['stem_evidence'] = 'THIS LONG QUOTE NEVER OCCURRED IN THE QUESTION.'
        elif mutation == 'fake_answer':
            checks[0]['answer_evidence'] = 'THIS LONG QUOTE NEVER OCCURRED IN THE ANSWER.'
        elif mutation == 'unknown_source':
            checks[0]['source_ids'] = ['other-account-evidence']
        elif mutation == 'empty_source':
            checks[0]['source_ids'] = []
        elif mutation == 'missing_check':
            checks.pop()
        elif mutation == 'duplicate_check':
            checks[1] = copy.deepcopy(checks[0])
        else:
            checks[0]['id'] = 'r6'

    flexible_wire['review_mutation'] = alter
    rig = flexible_case(agent_case, count=1)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert rig.evidence()['agent']['slots'][0]['status'] != 'passed'


def test_open_answer_policy_preserves_correctness_gate(agent_case, offline_harness, flexible_wire):
    flexible_wire['review_mutation'] = lambda value, contract, n: value.update(answer_correct=False)
    rig = flexible_case(agent_case, count=1)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()


def test_plan_requirement_cannot_expand_source_scope(agent_case, offline_harness, flexible_wire):
    flexible_wire['plan_mutation'] = lambda value, contract: value['questions'][0]['requirements'][0].update(
        source_ids=['foreign-source'])
    rig = flexible_case(agent_case, count=1)
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan']
    assert not rig.contents()


def test_cpu_composition_appends_to_base_and_is_visible_to_blind_solver(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, cpu=True, count=1)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert offline_harness == ['plan', 'author', 'compose', 'solve', 'review']
    base = compile_cpu_spec(SPEC, language='en', difficulty='hard', source_ids=[SOURCE['id']])['question']
    actual = rig.asset()['questions'][0]
    assert actual['stem'].startswith(base['stem'])
    assert actual['answer'].startswith(base['answer'])
    assert actual['explanation'].startswith(base['explanation'])
    assert 'Defend the policy tradeoff.' in actual['stem']
    assert 'ADDITIONAL_DEFENSE_V1' in actual['answer']
    compose = next(c for c in rig.wire.contracts if c['task'] == 'agent_compose_cpu_narrative')
    visible = compose['student_visible_context']
    assert visible['stem'] == base['stem']
    assert set(visible) == {'stem', 'sections'}
    assert visible['sections'] == rig.evidence()['agent']['slots'][0]['attempts'][0]['base_asset']['sections']
    assert 'ADDITIONAL_DEFENSE' not in dumps(visible)
    solve = next(c for c in rig.wire.contracts if c['task'] == 'agent_solve')
    assert solve['question']['stem'] == actual['stem']
    assert set(solve['question']) == {'kind', 'stem', 'options'}
    assert 'ADDITIONAL_DEFENSE' not in dumps(solve)
    assert 'PLAN_ONLY_' not in dumps(solve)
    review = next(c for c in rig.wire.contracts if c['task'] == 'agent_review')
    assert review['question']['answer'] == actual['answer']
    assert review['tool_results'] == rig.evidence()['agent']['slots'][0]['attempts'][0]['compiled_spec']['tool_results']
    assert rig.text_call_count() == 5


@pytest.mark.parametrize('mutation', ['foreign_citation', 'empty_citation', 'override_question', 'override_spec'])
def test_composer_cannot_replace_base_or_expand_source_scope(
        agent_case, offline_harness, flexible_wire, mutation):
    def alter(value, contract, count):
        if mutation == 'foreign_citation':
            value['citation_ids'] = ['foreign-source']
        elif mutation == 'empty_citation':
            value['citation_ids'] = []
        elif mutation == 'override_question':
            value['question'] = {'answer': '999', 'stem': 'Replace all original conditions.'}
        else:
            value['spec'] = {'processes': []}

    flexible_wire['compose_mutation'] = alter
    rig = flexible_case(agent_case, cpu=True, count=1)
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan'] + ['author', 'compose'] * 3
    assert not rig.contents()


def test_repair_can_change_explanation_without_changing_numeric_spec(
        agent_case, offline_harness, flexible_wire):
    def require_second_narrative(value, contract, count):
        if count == 1:
            value['requirement_checks'][0]['status'] = 'missing'

    flexible_wire['review_mutation'] = require_second_narrative
    rig = flexible_case(agent_case, cpu=True, count=1, settings_options={'agent_max_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert [a['status'] for a in attempts] == ['rejected', 'passed']
    assert attempts[0]['compiled_spec']['spec_sha256'] == attempts[1]['compiled_spec']['spec_sha256']
    assert attempts[0]['compiled_asset_sha256'] == attempts[1]['compiled_asset_sha256']
    assert attempts[0]['compose']['response']['answer'] != attempts[1]['compose']['response']['answer']
    assert 'ADDITIONAL_DEFENSE_V2' in rig.asset()['questions'][0]['answer']
    assert len([c for c in rig.wire.contracts if c['task'] == 'agent_compose_cpu_narrative']) == 2
    assert len([c for c in rig.wire.contracts if c['task'] == 'agent_author_cpu_spec']) == 1
    assert attempts[0]['feedback']['repair_target'] == 'narrative'
    assert attempts[1]['author']['origin'] == 'reused_frozen_spec'
    assert attempts[1]['author']['history'] == []


def test_resume_after_compose_keeps_its_cached_output_and_only_retries_solver(
        agent_case, offline_harness, flexible_wire):
    flexible_wire['fail_solve_at'] = 1
    rig = flexible_case(agent_case, cpu=True, count=1)
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(rig.evidence()['agent']['slots'][0]['attempts'][0]['compose'])
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    after = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert after['compose'] == before
    assert flexible_wire['briefs']['q1'] == rig.evidence()['agent']['slots'][0]['brief']
    assert offline_harness == ['plan', 'author', 'compose', 'solve', 'solve', 'review']
    assert rig.text_call_count() == 6


def test_resume_keeps_previously_accepted_cpu_question_and_second_compose(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, cpu=True, count=2, wire_options={'fail_phase': ('agent_review', 2)})
    assert rig.run()['status'] == 'failed', rig.job()
    before = copy.deepcopy(rig.evidence()['agent']['slots'])
    assert before[0]['status'] == 'passed'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    after = rig.evidence()['agent']['slots']
    assert after[0] == before[0]
    assert after[1]['attempts'][0]['compose'] == before[1]['attempts'][0]['compose']
    assert rig.wire.stage_counts[('agent_compose_cpu_narrative', 1)] == 1
    assert rig.wire.stage_counts[('agent_compose_cpu_narrative', 2)] == 1
    assert [q['slot_id'] for q in rig.asset()['questions']] == ['q1', 'q2']


def test_cpu_budget_preflight_includes_compose_per_question(agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, cpu=True, count=2, settings_options={'agent_max_calls': 9})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == [] and rig.retrieval_calls == []
    assert rig.text_call_count() == 0


def test_compose_shares_job_budget_with_plan_and_review(agent_case, offline_harness, flexible_wire):
    flexible_wire['review_mutation'] = lambda value, contract, n: value['requirement_checks'][0].update(status='missing')
    rig = flexible_case(agent_case, cpu=True, count=1,
                        settings_options={'agent_max_calls': 6, 'agent_max_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert offline_harness == ['plan', 'author', 'compose', 'solve', 'review']
    assert rig.text_call_count() == 5
    assert rig.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (rig.job_id,))['n'] == 6
    assert not rig.contents()


def test_scope_revoked_after_composition_blocks_solver(agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, cpu=True, count=1)
    attach_owned_documents(rig, [SOURCE['document_id']])

    def revoke(value, contract, count):
        rig.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', (SOURCE['document_id'], 0, None))

    flexible_wire['compose_mutation'] = revoke
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'compose']
    assert rig.evidence()['agent']['status'] == 'scope_changed'
    assert not rig.contents()


@pytest.mark.parametrize('gate', ['answer_correct', 'source_supported', 'ambiguity_free',
                                 'tool_inputs_match_question', 'calculations_verified', 'distinct_from_previous'])
def test_core_failure_requires_fresh_author_instead_of_only_recomposing(
        agent_case, offline_harness, flexible_wire, gate):
    def first_failure(value, contract, count):
        if count == 1:
            value[gate] = False

    flexible_wire['review_mutation'] = first_failure
    rig = flexible_case(agent_case, cpu=True, count=1, settings_options={'agent_max_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.wire.author_counts[1] == 2
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert [a['status'] for a in attempts] == ['rejected', 'passed']
    assert attempts[0]['feedback'].get('repair_target') != 'narrative'


def test_difficulty_mismatch_requires_new_author_even_when_requirements_are_met(
        agent_case, offline_harness, flexible_wire):
    def first_mismatch(value, contract, count):
        if count == 1:
            value['assessed_difficulty'] = 'easy'

    flexible_wire['solver_mutation'] = first_mismatch
    rig = flexible_case(agent_case, cpu=True, count=1, settings_options={'agent_max_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.wire.author_counts[1] == 2
    assert 'difficulty_mismatch' in rig.evidence()['agent']['slots'][0]['attempts'][0]['feedback']['issues']


def test_fake_quote_is_semantic_rejection_not_schema_retry(agent_case, offline_harness, flexible_wire):
    flexible_wire['review_mutation'] = lambda value, contract, count: value['requirement_checks'][0].update(
        stem_evidence='A fabricated quote that does not occur in this question.')
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review', 'review_evidence_repair']
    assert not rig.contents()


def test_requirement_quote_cannot_cite_a_root_source_absent_from_candidate(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, count=1)
    second = copy.deepcopy(SOURCE) | {'id': 'uncited-source-2', 'text': 'An additional course perspective.'}
    original = rig.pipeline.retrieve

    async def retrieve(*args, **kwargs):
        sources, trace = await original(*args, **kwargs)
        return sources + [second], trace

    rig.pipeline.retrieve = retrieve

    def assign_second_source(value, contract):
        brief = value['questions'][0]
        brief['source_ids'].append(second['id'])
        brief['requirements'][0]['source_ids'] = [second['id']]

    flexible_wire['plan_mutation'] = assign_second_source
    assert rig.run()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['status'] == 'protocol_failure'
    attempt = state['slots'][0]['attempts'][0]
    assert attempt['feedback']['issues'] == ['invalid_requirement_check_evidence']
    assert not rig.contents()


def test_composer_insufficient_evidence_requires_new_author_not_empty_publication(
        agent_case, offline_harness, flexible_wire):
    def first_insufficient(value, contract, count):
        if count == 1:
            value.update(evidence_sufficient=False, additional_task='', answer='', explanation='', citation_ids=[])

    flexible_wire['compose_mutation'] = first_insufficient
    rig = flexible_case(agent_case, cpu=True, count=1, settings_options={'agent_max_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert attempts[0]['feedback']['issues'] == ['composition_insufficient_evidence']
    assert 'solve' not in attempts[0]
    assert attempts[1]['status'] == 'passed' and rig.wire.author_counts[1] == 2
    assert 'ADDITIONAL_DEFENSE_V2' in rig.asset()['questions'][0]['answer']


def test_schema_repair_of_composer_keeps_original_response_and_uses_new_phase(
        agent_case, offline_harness, flexible_wire):
    def extra_field_once(value, contract, count):
        if count == 1:
            value['feedback_note'] = 'INVALID_COMPOSER_RESPONSE_SECRET'

    flexible_wire['compose_mutation'] = extra_field_once
    rig = flexible_case(agent_case, cpu=True, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert offline_harness == ['plan', 'author', 'compose', 'compose_schema_repair', 'solve', 'review']
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert attempt['compose']['response']['feedback_note'] == 'INVALID_COMPOSER_RESPONSE_SECRET'
    assert attempt['compose_schema_repair']['status'] == 'validated'
    repair_messages = attempt['compose_schema_repair']['history'][0]['messages']
    assert 'INVALID_COMPOSER_RESPONSE_SECRET' not in dumps(repair_messages)
    assert 'ADDITIONAL_DEFENSE_V2' in rig.asset()['questions'][0]['answer']


def wrong_solver_quote_once(value, contract, count):
    if count == 1:
        for check in value['requirement_checks']:
            check['answer_evidence'] = contract['independent_solution']['answer']
            check['explanation'] = 'INVALID_REVIEW_EXPLANATION_DO_NOT_FORWARD'
        value['feedback'] = 'INVALID_REVIEW_FEEDBACK_DO_NOT_FORWARD'


def test_wrong_solver_quote_gets_one_fresh_evidence_review_from_actual_candidate(
        agent_case, offline_harness, flexible_wire):
    flexible_wire['review_mutation'] = wrong_solver_quote_once
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert offline_harness == ['plan', 'author', 'solve', 'review', 'review_evidence_repair']
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    initial = attempt['review']['response']
    assert initial['requirement_checks'][0]['answer_evidence'].startswith('SOLVER DEFENDS')
    repair = attempt['review_evidence_repair']
    assert repair['response']['requirement_checks'][0]['answer_evidence'].startswith('AUTHOR DEFENDS')
    messages = repair['history'][0]['messages']
    contract = json.loads(messages[1]['content'])
    assert set(contract['independent_solution']) <= {'answerable', 'ambiguity_free', 'requires_calculation'}
    assert contract['question']['answer'] == rig.asset()['questions'][0]['answer']
    assert 'SOLVER DEFENDS' not in dumps(messages)
    assert 'INVALID_REVIEW_' not in dumps(messages)
    assert attempt['quality_checks']['requirements']['status'] == 'passed'
    assert rig.text_call_count() == 5


@pytest.mark.parametrize('second_failure', ['quote', 'schema'])
def test_evidence_repair_cannot_repair_itself_or_start_schema_repair(
        agent_case, offline_harness, flexible_wire, second_failure):
    def invalid_twice(value, contract, count):
        if count == 1:
            wrong_solver_quote_once(value, contract, count)
        elif second_failure == 'quote':
            value['requirement_checks'][0]['answer_evidence'] = 'SECOND_REVIEW_ALSO_INVENTED_THIS_QUOTE'
        else:
            value['unexpected_field'] = 'Do not open another repair loop.'

    flexible_wire['review_mutation'] = invalid_twice
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review', 'review_evidence_repair']
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert attempt['review_evidence_repair']['response']
    assert 'review_schema_repair' not in attempt
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert not rig.contents()


def test_schema_repair_already_used_for_review_prevents_evidence_repair(
        agent_case, offline_harness, flexible_wire):
    def spend_schema_then_fake(value, contract, count):
        if count == 1:
            value['unexpected_field'] = 'Schema repair required first.'
        else:
            value['requirement_checks'][0]['answer_evidence'] = contract['independent_solution']['answer']

    flexible_wire['review_mutation'] = spend_schema_then_fake
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review', 'review_schema_repair']
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert 'review_evidence_repair' not in attempt
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert not rig.contents()


@pytest.mark.parametrize('status', ['partial', 'missing', 'unsupported'])
def test_valid_negative_requirement_is_quality_failure_without_evidence_repair(
        agent_case, offline_harness, flexible_wire, status):
    flexible_wire['review_mutation'] = lambda value, contract, count: value['requirement_checks'][0].update(
        status=status, stem_evidence='', answer_evidence='', source_ids=[])
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan'] + ['author', 'solve', 'review'] * 3
    assert rig.evidence()['agent']['status'] == 'quality_failed'
    assert not rig.contents()


@pytest.mark.parametrize('limit,status', [('agent_max_calls', 'call_limit'), ('max_daily_calls', 'call_limit')])
def test_evidence_repair_respects_shared_budget_before_any_extra_request(
        agent_case, offline_harness, flexible_wire, limit, status):
    flexible_wire['review_mutation'] = wrong_solver_quote_once
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1, limit: 5})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review']
    assert rig.text_call_count() == 4
    assert rig.evidence()['agent']['status'] == status
    assert not rig.contents()


def test_interrupted_evidence_repair_resumes_only_that_phase(
        agent_case, offline_harness, flexible_wire):
    flexible_wire.update(review_mutation=wrong_solver_quote_once, fail_review_call=2)
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(rig.evidence()['agent']['slots'][0]['attempts'][0]['review'])
    assert rig.evidence()['agent']['status'] == 'provider_error'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert attempt['review'] == before
    assert offline_harness == ['plan', 'author', 'solve', 'review', 'review_evidence_repair', 'review_evidence_repair']
    history = attempt['review_evidence_repair']['history']
    assert [entry['status'] for entry in history] == ['provider_error', 'completed']
    assert [entry['resume_count'] for entry in history] == [0, 1]
    assert rig.text_call_count() == 6


@pytest.mark.parametrize('negative', ['answer_correct', 'source_supported', 'issues', 'low_confidence', 'partial'])
def test_invalid_quote_cannot_use_evidence_repair_to_overturn_existing_negative_judgment(
        agent_case, offline_harness, flexible_wire, negative):
    def negative_and_invalid(value, contract, count):
        wrong_solver_quote_once(value, contract, count)
        if negative in ('answer_correct', 'source_supported'):
            value[negative] = False
        elif negative == 'issues':
            value['issues'] = ['The actual proposed answer contains a concrete unresolved defect.']
        elif negative == 'low_confidence':
            value['confidence'] = 'low'
        else:
            # r2 remains met with an invalid quote, so validation still fails.
            value['requirement_checks'][0]['status'] = 'partial'

    flexible_wire['review_mutation'] = negative_and_invalid
    rig = flexible_case(agent_case, count=1,
                        settings_options={'harness_schema_repairs': 1, 'agent_max_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review']
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert not rig.contents()


def test_completed_evidence_repair_is_reused_after_controller_interruption(
        agent_case, offline_harness, flexible_wire, monkeypatch):
    flexible_wire['review_mutation'] = wrong_solver_quote_once
    original = GenerationAgent.call
    interrupted = False

    async def interrupt_after_saved_response(self, attempt, phase, contract, system):
        nonlocal interrupted
        result = await original(self, attempt, phase, contract, system)
        if phase == 'review_evidence_repair' and not interrupted:
            interrupted = True
            self.state['status'] = 'provider_error'
            self.save()
            raise ProviderError('Fixture interruption after the complete response was persisted.')
        return result

    monkeypatch.setattr(GenerationAgent, 'call', interrupt_after_saved_response)
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(rig.evidence()['agent']['slots'][0]['attempts'][0]['review_evidence_repair'])
    assert before['history'][0]['status'] == 'completed' and before['response']
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    after = rig.evidence()['agent']['slots'][0]['attempts'][0]['review_evidence_repair']
    assert after['history'] == before['history'] and after['response'] == before['response']
    assert offline_harness == ['plan', 'author', 'solve', 'review', 'review_evidence_repair']
    assert rig.text_call_count() == 5


def test_revoked_scope_blocks_evidence_repair_before_network(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    attach_owned_documents(rig, [SOURCE['document_id']])

    def invalid_then_revoke(value, contract, count):
        wrong_solver_quote_once(value, contract, count)
        rig.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', (SOURCE['document_id'], 0, None))

    flexible_wire['review_mutation'] = invalid_then_revoke
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['plan', 'author', 'solve', 'review']
    assert rig.evidence()['agent']['status'] == 'scope_changed'
    assert rig.text_call_count() == 4
    assert not rig.contents()


@pytest.mark.parametrize('negative,expected_issue', [
    ('answer_correct', 'answer_correct'), ('explanation_correct', 'explanation_correct'),
    ('partial', 'requirement_coverage:r1:partial'), ('issues', 'review_reported_issues'),
    ('low_confidence', 'review_low_confidence'),
])
def test_fresh_evidence_review_can_reject_without_being_forced_to_preserve_initial_positive(
        agent_case, offline_harness, flexible_wire, negative, expected_issue):
    def fresh_negative(value, contract, count):
        if count == 1:
            wrong_solver_quote_once(value, contract, count)
        elif negative in ('answer_correct', 'explanation_correct'):
            value[negative] = False
        elif negative == 'partial':
            value['requirement_checks'][0].update(status='partial', stem_evidence='', answer_evidence='', source_ids=[])
        elif negative == 'issues':
            value['issues'] = ['The actual candidate omits a material qualification.']
        else:
            value['confidence'] = 'low'

    flexible_wire['review_mutation'] = fresh_negative
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == (['plan', 'author', 'solve', 'review', 'review_evidence_repair']
                               + ['author', 'solve', 'review'] * 2)
    state = rig.evidence()['agent']
    assert state['status'] == 'quality_failed'
    attempt = state['slots'][0]['attempts'][0]
    assert expected_issue in attempt['feedback']['issues']
    assert attempt['review_evidence_repair']['status'] == 'validated'
    assert not rig.contents()
