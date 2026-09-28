"""The protocol must abstain when evidence cannot justify an actionable grade."""
from copy import deepcopy
import json

import pytest

from app import difficulty_review_v2 as review


@pytest.fixture
def case():
    return {'learner_profile': 'Learner has repeatedly practised single-digit addition.',
            'student_visible_text': 'Use addition. What is 1 + 1?',
            'reference_answer': '2', 'reference_explanation': 'One plus one is two.',
            'sources': [{'id': 'math', 'text': 'Adding one to one gives two.'}],
            'target_difficulty': 'PRIVATE_TARGET', 'original_difficulty': 'PRIVATE_OLD',
            'kind': 'mcq', 'options': ['2', '3']}


@pytest.fixture
def analysis_raw():
    return {'eligibility': 'eligible', 'eligibility_reason': 'Conditions and preparation are clear.',
            'solution': '2', 'tasks': [{'request_ids': ['S002'], 'condition_ids': ['S001'],
                'provided_ids': [], 'residual_work': 'Apply practised addition.', 'mode': 'apply_familiar'}],
            'decision': {'candidates': ['easy'], 'boundary_reason': 'Immediate practised application, no new integration.',
                         'hard_task_index': None}}


def verification_raw(case, analysis):
    catalogue = review._verification_catalogue(review._documents(case, analysis))

    def evidence(identity, quote):
        found = next((item['id'] for item in catalogue
                      if item['document_id'] == identity and quote in item['text']), None)
        return found or next(item['id'] for item in catalogue if item['document_id'] == 'reference_answer')

    def check(*items):
        return {'status': 'pass', 'reason': 'The supplied excerpts support this check.', 'evidence_ids': list(items)}

    raw = {'checks': {
        'conditions_complete': check(evidence('student', 'What is 1 + 1?')),
        'solution_correct': check(evidence('analysis', 'Apply practised addition.'), evidence('student', '1 + 1')),
        'reference_correct': check(evidence('reference_answer', '2'), evidence('student', '1 + 1')),
        'explanation_consistent': check(evidence('reference_explanation', 'One plus one is two.'),
                                         evidence('student', '1 + 1')),
        'source_support': check(evidence('source:math', 'Adding one to one gives two.'),
                                evidence('reference_answer', '2')),
        'demand_supported': check(evidence('analysis', 'Apply practised addition.'),
                                  evidence('student', 'What is 1 + 1?'),
                                  evidence('learner_profile', 'Learner has repeatedly practised'))}}
    source = raw['checks']['source_support']
    source['source_ids'], source['claim_ids'] = source['evidence_ids'][:1], source.pop('evidence_ids')[1:]
    demand = raw['checks']['demand_supported']
    selected = demand.pop('evidence_ids')
    demand.update(analysis_ids=selected[:1], student_ids=selected[1:2], profile_ids=selected[2:])
    return raw


def records(case, raw):
    analysis = review.validate_analysis(raw, case)
    verification = review.validate_verification(verification_raw(case, analysis), case, analysis)
    return ({'status': 'validated', 'result': analysis},
            {'status': 'validated', 'result': verification})


def test_blind_analysis_cannot_leak_answers_sources_or_labels(case):
    messages = review.analysis_messages(case)
    payload = json.loads(messages[1]['content'])
    assert set(payload) == {'learner_profile', 'student_visible_text', 'catalogue', 'schema'}
    for secret in ('PRIVATE_TARGET', 'PRIVATE_OLD', case['reference_explanation'], case['sources'][0]['text']):
        assert secret not in json.dumps(messages)


def test_verifier_sees_only_frozen_evidence_not_target_or_old_grade(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    payload = json.loads(review.verification_messages(case, analysis)[1]['content'])
    assert set(payload['documents']) == {'student', 'learner_profile', 'reference_answer', 'reference_explanation', 'analysis', 'source:math'}
    assert 'PRIVATE_TARGET' not in json.dumps(payload)
    assert 'PRIVATE_OLD' not in json.dumps(payload)
    assert '"solution": "2"' in ''.join(part['text'] for part in payload['documents']['analysis'])


def test_exact_span_offsets_and_unknown_evidence_fail(case, analysis_raw):
    parsed = review.validate_analysis(analysis_raw, case)
    span = parsed['resolved_evidence'][0]['request_ids'][0]
    assert case['student_visible_text'][span['start']:span['end']] == span['text']
    analysis_raw['tasks'][0]['request_ids'] = ['S999']
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


@pytest.mark.parametrize('field', ['request_ids', 'condition_ids', 'provided_ids'])
def test_duplicate_task_evidence_is_rejected(case, analysis_raw, field):
    analysis_raw['tasks'][0][field] = ['S001', 'S001']
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


@pytest.mark.parametrize('candidates', [['easy', 'hard'], ['easy', 'easy'], [], ['easy', 'medium', 'hard']])
def test_eligible_candidate_sets_are_coherent(case, analysis_raw, candidates):
    analysis_raw['decision']['candidates'] = candidates
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


def test_familiar_application_can_be_medium_without_strategy(case, analysis_raw):
    analysis_raw['decision']['candidates'] = ['medium']
    analysis_raw['decision']['boundary_reason'] = 'Integrates two familiar concepts without a supplied combined method.'
    result = review.validate_analysis(analysis_raw, case)
    assert result['estimated_difficulty'] == 'medium'


def test_hard_requires_a_real_strategic_task(case, analysis_raw):
    analysis_raw['decision'].update(candidates=['hard'], hard_task_index=0)
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)
    analysis_raw['tasks'][0]['mode'] = 'select_or_adapt'
    assert review.validate_analysis(analysis_raw, case)['estimated_difficulty'] == 'hard'
    analysis_raw['decision']['hard_task_index'] = True
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


def test_invalid_question_cannot_carry_grade_or_be_rework_candidate(case, analysis_raw):
    analysis_raw['eligibility'] = 'invalid_question'
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)
    analysis_raw['decision']['candidates'] = []
    analysis = review.validate_analysis(analysis_raw, case)
    result = review.decide({'status': 'validated', 'result': analysis}, None, 'hard')
    assert result['status'] == 'ungradable'
    assert result['recommendation'] == 'manual_review'


def test_empty_profile_cannot_be_repaired_by_model(case, analysis_raw):
    case['learner_profile'] = ' '
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)
    analysis_raw['eligibility'] = 'insufficient_profile'
    analysis_raw['decision']['candidates'] = []
    assert review.validate_analysis(analysis_raw, case)['estimated_difficulty'] == 'uncertain'


@pytest.mark.parametrize('mutation', ['coerce_solution', 'extra_field', 'unanswerable', 'reproduced_medium'])
def test_analysis_rejects_coercion_and_internal_inconsistency(case, analysis_raw, mutation):
    if mutation == 'coerce_solution':
        analysis_raw['solution'] = 2
    elif mutation == 'extra_field':
        analysis_raw['confidence'] = 0.99
    elif mutation == 'unanswerable':
        analysis_raw['tasks'][0]['mode'] = 'unanswerable'
    else:
        analysis_raw['tasks'][0]['mode'] = 'reproduce'
        analysis_raw['decision']['candidates'] = ['medium']
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


def test_all_six_checks_are_mandatory_and_empty_findings_never_pass(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    for raw in ({'findings': []}, {'checks': {}}, {'checks': {'conditions_complete': verification_raw(case, analysis)['checks']['conditions_complete']}}):
        with pytest.raises(ValueError):
            review.validate_verification(raw, case, analysis)


@pytest.mark.parametrize('mutation', ['unknown_doc', 'fabricated_quote', 'duplicate_quote', 'no_evidence', 'coerced_status'])
def test_verification_requires_exact_and_explicit_evidence(case, analysis_raw, mutation):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    check = raw['checks']['conditions_complete']
    if mutation == 'unknown_doc':
        check['evidence_ids'][0] = 'E9999'
    elif mutation == 'fabricated_quote':
        check['evidence'] = [{'document_id': 'student', 'quote': 'The author is always correct.'}]
    elif mutation == 'duplicate_quote':
        check['evidence_ids'].append(check['evidence_ids'][0])
    elif mutation == 'no_evidence':
        check['evidence_ids'] = []
    else:
        check['status'] = True
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


@pytest.mark.parametrize('check_name', ['solution_correct', 'reference_correct', 'explanation_consistent', 'source_support', 'demand_supported'])
def test_pass_needs_comparison_evidence_from_both_sides(case, analysis_raw, check_name):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    check = raw['checks'][check_name]
    if check_name == 'source_support':
        check['source_ids'] = []
    elif check_name == 'demand_supported':
        check['profile_ids'] = []
    else:
        check['evidence_ids'] = check['evidence_ids'][:1]
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


def test_missing_sources_can_only_abstain_not_self_certify(case, analysis_raw):
    case['sources'] = []
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    raw['checks']['source_support']['source_ids'] = []
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)
    raw['checks']['source_support']['status'] = 'uncertain'
    verified = review.validate_verification(raw, case, analysis)
    result = review.decide({'status': 'validated', 'result': analysis}, {'status': 'validated', 'result': verified}, 'hard')
    assert result['status'] == 'insufficient_evidence'
    assert result['recommendation'] == 'manual_review'


def test_changed_material_or_analysis_invalidates_evidence(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    modified = deepcopy(case)
    modified['sources'][0]['text'] = 'A different document.'
    with pytest.raises(ValueError):
        review.verification_messages(modified, analysis)
    analysis['solution'] = '3'
    with pytest.raises(ValueError):
        review.validate_verification(verification_raw(case, analysis), case, analysis)


def test_advisory_candidate_needs_singleton_complete_checks_and_mismatch(case, analysis_raw):
    first, second = records(case, analysis_raw)
    mismatch = review.decide(first, second, 'medium')
    assert mismatch['status'] == 'assessed'
    assert mismatch['recommendation'] == 'rework_candidate'
    assert mismatch['target_relation'] == 'mismatch'
    assert mismatch['enforcement_enabled'] is False
    assert mismatch['human_validated'] is False
    assert review.decide(first, second, 'easy')['recommendation'] == 'no_difficulty_rework'
    assert review.decide(first, second, None)['recommendation'] == 'manual_review'


def test_adjacent_boundary_is_not_a_candidate_even_when_target_excluded(case, analysis_raw):
    analysis_raw['decision']['candidates'] = ['medium', 'easy']
    first, second = records(case, analysis_raw)
    result = review.decide(first, second, 'hard')
    assert result['status'] == 'boundary'
    assert result['candidates'] == ['easy', 'medium']
    assert result['recommendation'] == 'manual_review'
    assert review.decide(first, second, 'easy')['target_relation'] == 'plausible'


@pytest.mark.parametrize('name', review.CHECKS)
@pytest.mark.parametrize('status', ['fail', 'uncertain'])
def test_any_failed_or_uncertain_check_blocks_candidate(case, analysis_raw, name, status):
    first, _ = records(case, analysis_raw)
    raw = verification_raw(case, first['result'])
    raw['checks'][name]['status'] = status
    second = {'status': 'validated', 'result': review.validate_verification(raw, case, first['result'])}
    result = review.decide(first, second, 'hard')
    assert result['recommendation'] == 'manual_review'
    assert result['status'] == ('content_issue' if status == 'fail' and name in review.CONTENT_CHECKS else 'insufficient_evidence')


def test_failed_interrupted_or_stale_records_cannot_be_candidates(case, analysis_raw):
    first, second = records(case, analysis_raw)
    for status in ('failed', 'in_flight', 'interrupted_not_retried', 'skipped'):
        assert review.decide({**first, 'status': status}, second, 'hard')['status'] == 'unavailable'
        assert review.decide(first, {**second, 'status': status}, 'hard')['status'] == 'unavailable'
    second['result']['analysis_sha256'] = '0' * 64
    assert review.decide(first, second, 'hard')['status'] == 'unavailable'


def test_context_skeleton_is_sizing_only(case):
    assert json.loads(review.verification_context_messages(case)[1]['content'])['documents']['analysis'] == []
    with pytest.raises((ValueError, KeyError)):
        review.verification_messages(case, {})


def test_host_expands_evidence_ids_to_exact_immutable_document_locations(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    payload = json.loads(review.verification_messages(case, analysis)[1]['content'])
    entries = {}
    for document_id, parts in payload['documents'].items():
        position = 0
        for part in parts:
            if 'id' in part:
                entries[part['id']] = {'id': part['id'], 'document_id': document_id,
                    'text': part['text'], 'start': position, 'end': position + len(part['text'])}
            position += len(part['text'])
    originals = review._documents(case, analysis)
    assert list(entries.values()) == review._verification_catalogue(originals)
    for item in entries.values():
        original = originals[item['document_id']]
        assert item['text'] == original[item['start']:item['end']]
    raw = verification_raw(case, analysis)
    verified = review.validate_verification(raw, case, analysis)
    for name, check in verified['checks'].items():
        selected = [identity for field, values in raw['checks'][name].items()
                    if field.endswith('_ids') for identity in values]
        assert check['evidence_ids'] == selected
        assert check['evidence'] == [
            {'document_id': entries[identity]['document_id'], 'quote': entries[identity]['text']}
            for identity in check['evidence_ids']]


def test_learning_objective_cannot_replace_required_profile_evidence(case, analysis_raw):
    case['student_visible_text'] += ' Learning objective: master difficult addition.'
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    catalogue = review._verification_catalogue(review._documents(case, analysis))
    demand = raw['checks']['demand_supported']
    demand['profile_ids'] = [next(item['id'] for item in catalogue
                                if item['document_id'] == 'student' and 'Learning objective' in item['text'])]
    with pytest.raises(ValueError, match='wrong document role'):
        review.validate_verification(raw, case, analysis)
    # An explicit uncertainty is preserved instead of being forced into a pass.
    demand['status'] = 'uncertain'
    demand['profile_ids'] = []
    verified = review.validate_verification(raw, case, analysis)
    result = review.decide({'status': 'validated', 'result': analysis},
                           {'status': 'validated', 'result': verified}, 'hard')
    assert result['status'] == 'insufficient_evidence'
    assert result['recommendation'] == 'manual_review'


def test_model_cannot_reattribute_evidence_id_with_an_invented_document(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    raw['checks']['source_support']['source_ids'][0] = {'id': 'E0001', 'document_id': 'source:math'}
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


@pytest.mark.parametrize('check_name,field,wrong_document', [
    ('source_support', 'source_ids', 'student'),
    ('source_support', 'claim_ids', 'source:math'),
    ('demand_supported', 'analysis_ids', 'student'),
    ('demand_supported', 'student_ids', 'analysis'),
    ('demand_supported', 'profile_ids', 'student'),
])
def test_explicit_evidence_roles_cannot_be_filled_from_another_document(case, analysis_raw, check_name, field, wrong_document):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    catalogue = review._verification_catalogue(review._documents(case, analysis))
    raw['checks'][check_name][field] = [next(item['id'] for item in catalogue
                                            if item['document_id'] == wrong_document)]
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)
    raw['checks'][check_name]['status'] = 'uncertain'
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


@pytest.mark.parametrize('check_name,field', [('source_support', 'claim_ids'),
                                            ('demand_supported', 'student_ids')])
def test_live_failure_pattern_cannot_omit_comparison_side(case, analysis_raw, check_name, field):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    del raw['checks'][check_name][field]
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


def test_multicomponent_question_keeps_six_required_tasks(case, analysis_raw):
    case['student_visible_text'] = 'Use the supplied arithmetic rule. ' + ' '.join(
        f'Compute result for case {index}.' for index in range(1, 7))
    analysis_raw['tasks'] = [{**deepcopy(analysis_raw['tasks'][0]),
                              'request_ids': [f'S{index + 1:03d}'],
                              'residual_work': f'Compute the required case {index}.'}
                             for index in range(1, 7)]
    assert len(review.validate_analysis(analysis_raw, case)['tasks']) == 6
    analysis_raw['tasks'].extend(deepcopy(analysis_raw['tasks'][:3]))
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


def test_direct_retrieval_does_not_require_invented_prior_practice(case, analysis_raw):
    case['learner_profile'] = 'Beginner who can read the stated numbers; no earlier exercises are assumed.'
    case['student_visible_text'] = 'The listed result is 2. State the listed result.'
    analysis_raw['tasks'][0].update(request_ids=['S002'], condition_ids=[], provided_ids=['S001'],
        mode='reproduce', residual_work='Locate and repeat the explicitly listed result.')
    analysis_raw['decision']['boundary_reason'] = 'The requested fact is already supplied; no independent calculation or previous practice is needed.'
    parsed = review.validate_analysis(analysis_raw, case)
    assert parsed['estimated_difficulty'] == 'easy'
    assert parsed['resolved_evidence'][0]['provided_ids'][0]['text'] == 'The listed result is 2.'


def test_multiclaim_verification_retains_seven_analysis_and_six_comparison_locations(case, analysis_raw):
    case['student_visible_text'] += ' ' + ' '.join(f'Given condition {index}.' for index in range(1, 9))
    analysis_raw['solution'] += ' ' + ' '.join(f'Final result for subtask {index}.' for index in range(1, 9))
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    catalogue = review._verification_catalogue(review._documents(case, analysis))
    student = [item['id'] for item in catalogue if item['document_id'] == 'student']
    analysed = [item['id'] for item in catalogue if item['document_id'] == 'analysis']
    raw['checks']['reference_correct']['evidence_ids'] = (
        raw['checks']['reference_correct']['evidence_ids'][:1] + student[:5])
    raw['checks']['source_support']['claim_ids'] = student[:3]
    raw['checks']['demand_supported']['analysis_ids'] = analysed[:7]
    raw['checks']['demand_supported']['student_ids'] = student[:7]
    verified = review.validate_verification(raw, case, analysis)
    assert len(verified['checks']['reference_correct']['evidence_ids']) == 6
    assert len(verified['checks']['demand_supported']['evidence_ids']) == 15
    decision = review.decide({'status': 'validated', 'result': analysis},
                             {'status': 'validated', 'result': verified}, 'hard')
    assert decision['status'] == 'assessed'
    raw['checks']['demand_supported']['analysis_ids'] = analysed[:9]
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


@pytest.mark.parametrize('solution_status', ['fail', 'uncertain'])
def test_wrong_blind_solution_does_not_accuse_correct_reference(case, analysis_raw, solution_status):
    # Motivated by laboratory_slots after its holdout was opened: that cohort
    # is now consumed development evidence, not a fresh validation set.
    analysis_raw['solution'] = '3'
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    raw['checks']['solution_correct'].update(status=solution_status,
        reason='The blind solution gives 3; the given addition rule yields 2. The reference answer is correct.')
    verified = review.validate_verification(raw, case, analysis)
    result = review.decide({'status': 'validated', 'result': analysis},
                           {'status': 'validated', 'result': verified}, 'hard')
    assert result['status'] == 'insufficient_evidence'
    prefix = 'failed' if solution_status == 'fail' else 'uncertain'
    assert result['reason_codes'] == [f'{prefix}:solution_correct']
    assert result['recommendation'] == 'manual_review'
    assert verified['checks']['reference_correct']['status'] == 'pass'


@pytest.mark.parametrize('content_check', review.CONTENT_CHECKS)
def test_actual_asset_defect_takes_priority_over_solver_error(case, analysis_raw, content_check):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    raw['checks']['solution_correct']['status'] = 'fail'
    raw['checks'][content_check]['status'] = 'fail'
    verified = review.validate_verification(raw, case, analysis)
    result = review.decide({'status': 'validated', 'result': analysis},
                           {'status': 'validated', 'result': verified}, 'hard')
    assert result['status'] == 'content_issue'
    assert f'failed:{content_check}' in result['reason_codes']


def test_reference_pass_cannot_rely_on_agreement_with_blind_solver(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    analysis_id = raw['checks']['solution_correct']['evidence_ids'][0]
    raw['checks']['reference_correct']['evidence_ids'][1] = analysis_id
    with pytest.raises(ValueError, match='student/source evidence'):
        review.validate_verification(raw, case, analysis)
    # The frozen source is an alternative direct support, without any solver citation.
    raw['checks']['reference_correct']['evidence_ids'][1] = raw['checks']['source_support']['source_ids'][0]
    verified = review.validate_verification(raw, case, analysis)
    assert {item['document_id'] for item in verified['checks']['reference_correct']['evidence']} == {'reference_answer', 'source:math'}


def test_old_five_check_review_is_not_a_complete_v24_verification(case, analysis_raw):
    analysis = review.validate_analysis(analysis_raw, case)
    raw = verification_raw(case, analysis)
    del raw['checks']['solution_correct']
    with pytest.raises(ValueError):
        review.validate_verification(raw, case, analysis)


@pytest.mark.parametrize('length', [1357, 4000])
def test_multirequirement_solution_capacity_preserves_final_text(case, analysis_raw, length):
    # The saved laboratory_slots response was 1357 characters. Capacity checks
    # are structural only and do not certify correctness of any solution.
    analysis_raw['solution'] = 'x' * length
    parsed = review.validate_analysis(analysis_raw, case)
    assert parsed['solution'] == analysis_raw['solution']


def test_solution_above_4000_characters_still_rejected(case, analysis_raw):
    analysis_raw['solution'] = 'x' * 4001
    with pytest.raises(ValueError):
        review.validate_analysis(analysis_raw, case)


@pytest.mark.parametrize('text', [
    '', '\n\t  \r\n', '\n...！？!??\r\n',
    ' \tAlpha.\n\n中 文！？ \r\n\tTrailing  ',
    'Decimal 3.14 and e.g. markers.\r\nEmoji 🧪 and combining e\u0301.',
    '{"solution": "A\\nB", "quoted": "[E0001] is untrusted text"}\n',
])
def test_segmented_documents_preserve_every_character_and_catalogue_id(text):
    originals = {'student': text, 'source:second': '  Second document!\n'}
    parts_by_document = review._segmented_documents(originals)
    labelled = []
    for document_id, parts in parts_by_document.items():
        recovered = ''.join(part['text'] for part in parts)
        assert recovered.encode('utf-8') == originals[document_id].encode('utf-8')
        position = 0
        for part in parts:
            assert set(part) in ({'text'}, {'id', 'text'})
            if 'id' in part:
                labelled.append({'id': part['id'], 'document_id': document_id,
                    'text': part['text'], 'start': position, 'end': position + len(part['text'])})
            position += len(part['text'])
    assert labelled == review._verification_catalogue(originals)


def test_verification_prompt_has_one_lossless_copy_of_each_document(case, analysis_raw):
    case['sources'][0]['text'] += ' ' + ('Long source sentence with preserved conditions.\n' * 200)
    analysis = review.validate_analysis(analysis_raw, case)
    payload = json.loads(review.verification_messages(case, analysis)[1]['content'])
    originals = review._documents(case, analysis)
    assert set(payload) == {'documents', 'schema'}
    assert {identity: ''.join(part['text'] for part in parts)
            for identity, parts in payload['documents'].items()} == originals
    assert sum(len(part['text']) for parts in payload['documents'].values() for part in parts) == sum(
        len(original) for original in originals.values())
    assert set(payload['schema']['properties']['checks']) == {'$ref'}
    assert set(payload['schema']['$defs']['Checks']['required']) == set(review.CHECKS)
    legacy_payload = {'learner_profile': case['learner_profile'], 'documents': originals,
                      'evidence_catalogue': review._verification_catalogue(originals),
                      'schema': review.Verification.model_json_schema()}
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < len(json.dumps(legacy_payload, ensure_ascii=False).encode())


def test_segmentation_refuses_inconsistent_catalogue_instead_of_losing_text(monkeypatch):
    monkeypatch.setattr(review, '_verification_catalogue', lambda _: [
        {'document_id': 'student', 'id': 'E0001', 'start': 0, 'end': 3, 'text': 'wrong'}])
    with pytest.raises(ValueError, match='does not match'):
        review._segmented_documents({'student': 'original'})


def test_fingerprint_changes_with_prompt_and_is_stable(monkeypatch):
    first = review.protocol_fingerprint()
    assert first == review.protocol_fingerprint()
    monkeypatch.setattr(review, 'ANALYSIS_SYSTEM', review.ANALYSIS_SYSTEM + ' A changed rule.')
    assert first != review.protocol_fingerprint()
