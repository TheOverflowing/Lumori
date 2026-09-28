"""Offline checks for flexible brief scope and quote-backed coverage contracts."""
from copy import deepcopy
import json

import pytest

from app.assessment_contracts import (AssessmentBrief, AssessmentPlan, AssessmentRequirement, CpuNarrative,
    FLEXIBLE_PLANNER_REVISION, FLEXIBLE_PLANNER_SYSTEM, REQUIREMENT_COVERAGE_NOTE, RequirementCheck,
    flexible_planner_contract, validate_flexible_plan, validate_requirement_checks,
    reconcile_flexible_plan_sources)
from app.models import GenerateRequest, Question


BRIEF = {'slot_id': 'q1', 'difficulty': 'hard', 'kind': 'short_answer', 'focus': 'Response and turnaround',
    'learning_goal': 'Explain why ordering by response can differ from ordering by turnaround.',
    'requirements': [{'id': 'r1', 'kind': 'comparison', 'description': 'Compare response and turnaround order for two jobs.',
                      'source_ids': ['source-en']}],
    'source_ids': ['source-en', 'source-zh'], 'answer_policy': 'single_outcome'}
SLOTS = [{'slot_id': 'q1', 'difficulty': 'hard'}]
SOURCES = [{'id': 'source-en', 'text': 'Scheduling notes.', 'extraction': {'extraction_warnings': ['OCR warning']}},
           {'id': 'source-zh', 'text': '中文调度说明。'}]
QUESTION = {'slot_id': 'q1', 'difficulty': 'hard', 'kind': 'short_answer',
    'stem': 'Compare response time and turnaround time for C and D.', 'options': [],
    'answer': 'C responds earlier but completes later than D.',
    'explanation': 'Response measures first CPU access; turnaround includes all later waiting.',
    'difficulty_reason': 'Compare metrics under explicit conditions.', 'citation_ids': ['source-en', 'source-zh']}
CHECK = {'id': 'r1', 'status': 'met', 'stem_evidence': 'Compare response time',
    'answer_evidence': 'C responds earlier', 'explanation': 'The task and response both compare the requested metrics.',
    'source_ids': ['source-en']}
NARRATIVE = {'evidence_sufficient': True, 'additional_task': 'Explain the difference in job order.',
    'answer': 'C responds before D but completes after D.', 'explanation': 'First access and final completion measure different events.',
    'citation_ids': ['source-en']}


def plan(brief=None, slots=None, question_type='short_answer'):
    return validate_flexible_plan({'questions': [deepcopy(BRIEF) if brief is None else brief]},
        deepcopy(SLOTS) if slots is None else slots, question_type, {'source-en', 'source-zh'})


def coverage(checks=None, brief=None, question=None, sources=None):
    return validate_requirement_checks([deepcopy(CHECK)] if checks is None else checks,
        deepcopy(BRIEF) if brief is None else brief, deepcopy(QUESTION) if question is None else question,
        deepcopy(SOURCES) if sources is None else sources)


def test_valid_flexible_plan_and_coverage_return_typed_plan_and_no_issues():
    raw = deepcopy(BRIEF)
    result = plan(raw)
    assert isinstance(result, AssessmentPlan) and isinstance(result.questions[0], AssessmentBrief)
    assert isinstance(result.questions[0].requirements[0], AssessmentRequirement)
    assert result.questions[0].model_dump() == raw
    assert coverage(brief=result.questions[0], question=Question.model_validate(QUESTION)) == []
    assert coverage(sources={'source-en', 'source-zh'}) == []
    assert 'semantic' in REQUIREMENT_COVERAGE_NOTE


def test_reconcile_only_a_requirement_source_already_supplied_to_planner():
    brief = deepcopy(BRIEF)
    brief['source_ids'] = ['source-en']
    brief['requirements'][0]['source_ids'] = ['source-zh']
    raw = {'questions': [brief]}
    with pytest.raises(ValueError, match='subset'):
        validate_flexible_plan(raw, SLOTS, 'short_answer', {'source-en', 'source-zh'})
    repaired, changes = reconcile_flexible_plan_sources(raw, {'source-en', 'source-zh'})
    assert changes == [{'slot_id': 'q1', 'added_source_ids': ['source-zh']}]
    assert repaired['questions'][0]['source_ids'] == ['source-en', 'source-zh']
    assert raw['questions'][0]['source_ids'] == ['source-en']
    assert validate_flexible_plan(repaired, SLOTS, 'short_answer', {'source-en', 'source-zh'})
    rejected, changes = reconcile_flexible_plan_sources(raw, {'source-en'})
    assert rejected == raw and not changes
    with pytest.raises(ValueError, match='subset'):
        validate_flexible_plan(rejected, SLOTS, 'short_answer', {'source-en'})


@pytest.mark.parametrize('field,value', [
    ('id', 'r0'), ('id', 'r7'), ('id', 'r01'), ('id', 'r1 '), ('id', 1),
    ('kind', 'answer'), ('description', ' '), ('description', 'x' * 301),
    ('source_ids', []), ('source_ids', [' ']), ('source_ids', ['source-en', 'source-en']),
    ('source_ids', ['outside-brief']), ('source_ids', [1]),
    ('answer', 'A predetermined answer'), ('score', 100),
])
def test_invalid_requirement_contract_is_rejected(field, value):
    brief = deepcopy(BRIEF)
    brief['requirements'][0][field] = value
    with pytest.raises(ValueError):
        plan(brief)


@pytest.mark.parametrize('requirements', [[], ['legacy plain text'],
    [dict(BRIEF['requirements'][0], id='r2')],
    [BRIEF['requirements'][0], BRIEF['requirements'][0]],
    [dict(BRIEF['requirements'][0], id='r2'), BRIEF['requirements'][0]],
    [dict(BRIEF['requirements'][0], id=f'r{i}') for i in range(1, 8)]])
def test_requirement_count_and_exact_order_are_controller_constraints(requirements):
    with pytest.raises(ValueError):
        plan(dict(BRIEF, requirements=requirements))


def test_all_six_kinds_and_six_requirement_limit_work():
    kinds = ['knowledge', 'evidence', 'reasoning', 'comparison', 'constraint', 'interpretation']
    brief = dict(BRIEF, requirements=[dict(BRIEF['requirements'][0], id=f'r{i}', kind=kind)
                                    for i, kind in enumerate(kinds, 1)])
    assert len(plan(brief).questions[0].requirements) == 6


@pytest.mark.parametrize('field,value', [
    ('slot_id', 'q2'), ('slot_id', 'q51'), ('difficulty', 'easy'), ('kind', 'mcq'),
    ('focus', ' '), ('focus', 'x' * 161), ('learning_goal', 'x' * 601),
    ('source_ids', ['source-en', 'unknown']), ('answer_policy', 'choose_favorite'),
    ('answer', 'Prescribed conclusion'), ('score', 100),
])
def test_plan_preserves_existing_quota_scope_and_strict_fields(field, value):
    with pytest.raises(ValueError):
        plan(dict(BRIEF, **{field: value}))


def test_open_answer_policy_is_supported_but_not_for_mcq():
    assert plan(dict(BRIEF, answer_policy='multiple_defensible')).questions[0].answer_policy == 'multiple_defensible'
    with pytest.raises(ValueError, match='single_outcome'):
        plan(dict(BRIEF, kind='mcq', answer_policy='multiple_defensible'), question_type='mixed')
    assert plan(dict(BRIEF, kind='mcq'), question_type='mcq').questions[0].kind == 'mcq'


def test_mixed_bilingual_plan_still_preserves_slot_order_and_duplicate_guard():
    second = dict(BRIEF, slot_id='q50', difficulty='medium', focus='调度观点',
                  learning_goal='根据证据比较不同策略的适用范围。', answer_policy='multiple_defensible')
    slots = [SLOTS[0], {'slot_id': 'q50', 'difficulty': 'medium'}]
    assert len(validate_flexible_plan({'questions': [BRIEF, second]}, slots, 'mixed', {'source-en', 'source-zh'}).questions) == 2
    with pytest.raises(ValueError):
        validate_flexible_plan({'questions': [second, BRIEF]}, slots, 'mixed', {'source-en', 'source-zh'})
    duplicate = dict(BRIEF, slot_id='q50', difficulty='medium', focus='RESPONSE  AND TURNAROUND')
    with pytest.raises(ValueError, match='duplicate focus'):
        validate_flexible_plan({'questions': [BRIEF, duplicate]}, slots, 'mixed', {'source-en', 'source-zh'})
    with pytest.raises(ValueError, match='count'):
        validate_flexible_plan({'questions': [BRIEF]}, slots, 'mixed', {'source-en', 'source-zh'})


def test_flexible_contract_keeps_capabilities_and_provenance_without_past_answers():
    request = GenerateRequest(course_id='course', topic='Scheduling', question_type='short_answer', count=1,
                              request_key='private-key')
    capabilities = {'mode': 'cpu_schedule_v1', 'supported_policies': ['rr', 'stcf'],
                    'narrative': 'Bounded conceptual reasoning may accompany deterministic calculations.'}
    sources_before, capabilities_before = deepcopy(SOURCES), deepcopy(capabilities)
    previous = dict(BRIEF, slot_id='q2', answer='PRIVATE_ANSWER', history=['PRIVATE_HISTORY'])
    contract = flexible_planner_contract(request, SLOTS, SOURCES, [previous], capabilities)
    assert contract['task'] == 'agent_plan'
    assert contract['planner_revision'] == FLEXIBLE_PLANNER_REVISION
    assert contract['schema']['properties']['questions']['minItems'] == contract['schema']['properties']['questions']['maxItems'] == 1
    assert contract['schema']['$defs']['AssessmentBrief']['properties']['requirements']['maxItems'] == 6
    assert contract['schema']['$defs']['AssessmentRequirement']['additionalProperties'] is False
    assert set(contract['previous_briefs'][0]) == {'slot_id', 'focus', 'learning_goal'}
    assert 'PRIVATE_' not in json.dumps(contract)
    assert 'request_key' not in contract['request']
    assert contract['reference_chunks'] == sources_before
    assert contract['worker_capabilities'] == capabilities_before
    contract['worker_capabilities']['supported_policies'].append('unsupported')
    assert capabilities == capabilities_before
    assert 'authoritative hard boundary' in FLEXIBLE_PLANNER_SYSTEM
    assert 'multiple_defensible' in FLEXIBLE_PLANNER_SYSTEM
    assert 'preferred conclusion' in FLEXIBLE_PLANNER_SYSTEM
    assert 'argument_with_counterargument must use reasoning or comparison' in FLEXIBLE_PLANNER_SYSTEM
    assert 'argument_with_counterargument is not a requirement kind' in contract['output_contract']
    with pytest.raises(ValueError, match='capabilities'):
        flexible_planner_contract(request, SLOTS, SOURCES, [], {})


@pytest.mark.parametrize('status', ['partial', 'missing', 'unsupported'])
def test_every_non_met_status_returns_an_issue_without_self_scoring(status):
    check = dict(CHECK, status=status, stem_evidence='', answer_evidence='', source_ids=[])
    assert coverage([check]) == [f'requirement_coverage:r1:{status}']


@pytest.mark.parametrize('checks', [[], [CHECK, CHECK], [dict(CHECK, id='r2')], (CHECK,)])
def test_checks_must_cover_exact_requirement_ids_once_in_order(checks):
    with pytest.raises(ValueError):
        coverage(checks)


def test_multiple_checks_cannot_be_reordered_or_hidden_by_met_items():
    brief = deepcopy(BRIEF)
    brief['requirements'].append(dict(brief['requirements'][0], id='r2', kind='interpretation'))
    partial = dict(CHECK, id='r2', status='partial', stem_evidence='', answer_evidence='', source_ids=[])
    assert coverage([CHECK, partial], brief=brief) == ['requirement_coverage:r2:partial']
    with pytest.raises(ValueError, match='IDs and order'):
        coverage([partial, CHECK], brief=brief)


@pytest.mark.parametrize('field,value', [
    ('stem_evidence', 'the'), ('stem_evidence', '        '), ('stem_evidence', 'compare response time'),
    ('stem_evidence', 'Compare response\ntime'), ('stem_evidence', 'invented exact quotation'),
    ('answer_evidence', 'C'), ('answer_evidence', 'D responds earlier'),
    ('answer_evidence', 'Compare response time'), ('answer_evidence', 'C responds earlier … completes'),
    ('explanation', ''), ('status', 'approved'), ('status', True),
    ('source_ids', []), ('source_ids', ['source-en', 'source-en']),
    ('score', 1.0), ('confidence', 'high'),
])
def test_met_requires_substantial_verbatim_evidence_and_strict_contract(field, value):
    with pytest.raises(ValueError):
        coverage([dict(CHECK, **{field: value})])


def test_answer_excerpt_can_be_in_explanation_but_not_across_field_boundaries():
    assert coverage([dict(CHECK, answer_evidence='Response measures first CPU access')]) == []
    question = dict(QUESTION, answer='First piece', explanation='second piece')
    with pytest.raises(ValueError, match='verbatim'):
        coverage([dict(CHECK, answer_evidence='piece second')], question=question)


def test_chinese_eight_character_evidence_and_trimmed_quotes_are_supported():
    question = dict(QUESTION, stem='比较首次响应时间与全部周转时间。', answer='首次响应时间较短不代表周转时间较短。')
    check = dict(CHECK, stem_evidence=' 比较首次响应时间 ', answer_evidence='首次响应时间较短')
    assert len(check['answer_evidence']) == 8
    assert coverage([check], question=question) == []


@pytest.mark.parametrize('scope', ['outside_sources', 'outside_question', 'outside_brief', 'outside_requirement'])
def test_check_citations_stay_inside_all_four_scopes(scope):
    brief, question, sources = deepcopy(BRIEF), deepcopy(QUESTION), deepcopy(SOURCES)
    cited = 'source-en'
    if scope == 'outside_sources':
        sources = [{'id': 'source-zh'}]
    elif scope == 'outside_question':
        question['citation_ids'] = ['source-zh']
    elif scope == 'outside_brief':
        cited = 'third-source'
        sources.append({'id': cited})
        question['citation_ids'].append(cited)
    else:
        cited = 'source-zh'
    with pytest.raises(ValueError, match='source scope'):
        coverage([dict(CHECK, source_ids=[cited])], brief=brief, question=question, sources=sources)


def test_typed_check_mutation_and_evidence_bounds_do_not_bypass_validation():
    check = RequirementCheck.model_validate(CHECK)
    check.stem_evidence = 'x' * 1001
    with pytest.raises(ValueError):
        coverage([check])
    for field, value in [('answer_evidence', 'x' * 1801), ('explanation', 'x' * 1001)]:
        with pytest.raises(ValueError):
            RequirementCheck.model_validate(dict(CHECK, **{field: value}))


def test_quote_membership_is_not_a_semantic_proof():
    brief = deepcopy(BRIEF)
    brief['requirements'][0]['description'] = 'Explain every stated switching rule.'
    # The quoted comparison sentences exist but do not establish this changed
    # requirement. The caller must retain the separate semantic reviewer gate.
    assert coverage(brief=brief) == []
    assert 'semantic' in REQUIREMENT_COVERAGE_NOTE


def test_cpu_narrative_supported_and_insufficient_evidence_shapes():
    assert CpuNarrative.model_validate(NARRATIVE).model_dump() == NARRATIVE
    empty = {'evidence_sufficient': False, 'additional_task': '', 'answer': '', 'explanation': '', 'citation_ids': []}
    assert CpuNarrative.model_validate(empty).model_dump() == empty
    assert CpuNarrative.model_validate(dict(empty, answer='   ')).answer == ''


@pytest.mark.parametrize('field,value', [
    ('evidence_sufficient', 'true'), ('evidence_sufficient', 1),
    ('additional_task', ''), ('additional_task', 'x' * 2001),
    ('answer', ' '), ('answer', 'x' * 6001), ('explanation', ''), ('explanation', 'x' * 6001),
    ('citation_ids', []), ('citation_ids', ['source-en', 'source-en']), ('citation_ids', [' ']),
    ('citation_ids', [1]), ('citation_ids', ['x' * 161]), ('spec', {}), ('score', 100),
])
def test_cpu_narrative_is_bounded_strict_and_has_no_base_replacement_fields(field, value):
    with pytest.raises(ValueError):
        CpuNarrative.model_validate(dict(NARRATIVE, **{field: value}))


@pytest.mark.parametrize('field,value', [('additional_task', 'Task'), ('answer', 'Answer'),
    ('explanation', 'Explanation'), ('citation_ids', ['source-en'])])
def test_unsupported_narrative_cannot_supply_text_or_citations(field, value):
    data = {'evidence_sufficient': False, 'additional_task': '', 'answer': '', 'explanation': '', 'citation_ids': []}
    with pytest.raises(ValueError):
        CpuNarrative.model_validate(dict(data, **{field: value}))
