"""Questions-only contracts with preset judgments; no live model calls."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.generation_agent import authorize_resume
from app.generation_preparation import fingerprint as preparation_fingerprint
from app.models import GenerateRequest, LearningAsset
from app.pipeline import AssetRuleError, validate_asset, student_audio_transcript
from app.store import dumps
from test_generation_agent import SOURCE, agent_case  # noqa: F401
from test_difficulty_pipeline import question, run_case  # noqa: F401
from test_flexible_agent import flexible_case, flexible_wire  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401


def request(**updates):
    return GenerateRequest(course_id='course-1', topic='Course evidence', count=1,
                           difficulty='easy', request_key='optional-context-fixture', **updates)


def asset(**updates):
    return LearningAsset.model_validate(dict(title='A question', evidence_sufficient=True,
        sections=[], questions=[question({'slot_id': 'q1', 'difficulty': 'easy'}, 1, [SOURCE['id']])]) | updates)


def test_new_request_defaults_to_questions_only_and_preserves_old_fingerprint_shape():
    legacy = request()
    assert legacy.include_explanations is False
    assert 'include_explanations' not in legacy.model_dump()
    explicit = request(include_explanations=False)
    assert explicit.model_dump()['include_explanations'] is False
    assert preparation_fingerprint(explicit) != preparation_fingerprint(legacy)
    validate_asset(asset(), legacy, [SOURCE], enforce_difficulty=True)
    assert asset().questions[0].answer and asset().questions[0].explanation


@pytest.mark.parametrize('bad', ['false', 'true', 0, 1, None])
def test_introduction_option_is_strict_boolean(bad):
    with pytest.raises(ValidationError):
        request(include_explanations=bad)


@pytest.mark.parametrize('preamble', [
    {'sections': [{'heading': 'Introduction', 'text': 'Necessary data.', 'citation_ids': [SOURCE['id']]}]},
    {'learning_objectives': ['A pre-question objective.']},
])
def test_questions_only_rejects_preamble_instead_of_silently_deleting_it(preamble):
    original = asset(**preamble)
    before = original.model_dump()
    with pytest.raises(AssetRuleError) as failure:
        validate_asset(original, request(), [SOURCE], author_output=True)
    assert failure.value.rule == 'assessment_preamble_disabled'
    assert original.model_dump() == before


def test_enabled_introduction_and_lessons_require_teaching_sections():
    for req in (request(include_explanations=True), request(material='lesson')):
        with pytest.raises(AssetRuleError) as failure:
            validate_asset(asset(), req, [SOURCE])
        assert failure.value.rule == 'teaching_sections_required'


@pytest.mark.parametrize('material', ['quiz', 'assignment'])
@pytest.mark.parametrize('include', [False, True])
def test_legacy_generation_honors_choice_before_blind_review(run_case, material, include):
    job, evidence, content, wire = run_case(material=material, include_explanations=include)
    assert job['status'] == 'succeeded', job
    assert bool(content['asset']['sections']) is include
    assert content['config']['include_explanations'] is include
    assert all(q['answer'] and q['explanation'] for q in content['asset']['questions'])
    contract = json.loads(wire.generations[0]['messages'][1]['content'])
    assert contract['schema']['properties']['sections']['maxItems'] == (10 if include else 0)
    review = json.loads(wire.assessments[0]['messages'][1]['content'])
    assert review['student_visible_context']['sections'] == content['asset']['sections']


def test_lesson_content_is_unchanged_when_introduction_is_false(run_case):
    job, _, content, _ = run_case(material='lesson', include_explanations=False)
    assert job['status'] == 'succeeded', job
    assert content['asset']['sections'] and not content['asset']['questions']


@pytest.mark.parametrize('runtime', ['native', 'deepseek_harness'])
@pytest.mark.parametrize('include', [False, True])
def test_agent_author_solver_and_reviewer_see_same_presentation_choice(
        agent_case, offline_harness, runtime, include):
    rig = agent_case(count=2, include_explanations=include, settings_options={'agent_runtime': runtime})
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert bool(rig.asset()['sections']) is include
    assert bool(rig.asset()['learning_objectives']) is include
    assert rig.evidence()['configuration']['include_explanations'] is include
    for contract in rig.wire.contracts:
        if contract['task'] == 'agent_author':
            assert contract['request']['include_explanations'] is include
            assert contract['schema']['properties']['sections']['maxItems'] == (2 if include else 0)
        else:
            assert bool(contract['student_visible_context']['sections']) is include
    assert len(rig.asset()['questions']) == 2
    assert all(q['answer'] and q['explanation'] for q in rig.asset()['questions'])


@pytest.mark.parametrize('cpu', [False, True])
def test_supervisor_questions_only_assembly_preserves_reviewed_stems_and_answers(
        agent_case, offline_harness, flexible_wire, cpu):
    rig = flexible_case(agent_case, count=2, cpu=cpu, include_explanations=False)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.asset()['sections'] == [] and rig.asset()['learning_objectives'] == []
    assert rig.asset()['section_scope'] == 'shared'
    slots = rig.evidence()['agent']['slots']
    assert rig.asset()['questions'] == [slot['accepted_asset']['questions'][0] for slot in slots]
    for slot in slots:
        accepted = slot['accepted_asset']['questions'][0]
        if cpu:
            base = slot['attempts'][0]['base_asset']
            assert base['sections'] == []
            assert base['questions'][0]['stem'] in accepted['stem']
            assert base['questions'][0]['answer'] in accepted['answer']
    for contract in rig.wire.contracts:
        if contract['task'] in ('agent_solve', 'agent_review', 'agent_compose_cpu_narrative'):
            assert contract['student_visible_context']['sections'] == []


def test_legacy_checkpoint_resume_keeps_existing_teaching_content(agent_case):
    rig = agent_case(count=2, include_explanations=True,
                     wire_options={'fail_phase': ('agent_review', 2)})
    assert rig.run()['status'] == 'failed'
    saved = rig.evidence()
    prior = deepcopy(saved['agent']['slots'][0]['accepted_asset'])
    # Emulate an authentic checkpoint/payload created before the new field.
    payload = json.loads(rig.job()['payload'])
    payload.pop('include_explanations')
    saved['configuration'].pop('include_explanations')
    saved['request'].pop('include_explanations')
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    rig.store.save_job_evidence(rig.job_id, saved)
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.evidence()['agent']['slots'][0]['accepted_asset'] == prior
    assert rig.asset()['sections']
    assert 'include_explanations' not in rig.evidence()['configuration']


def test_new_job_without_explicit_flag_freezes_questions_only_default(agent_case):
    rig = agent_case(count=1, include_explanations=False)
    payload = json.loads(rig.job()['payload'])
    payload.pop('include_explanations')
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.evidence()['configuration']['include_explanations'] is False
    assert rig.asset()['sections'] == []
    assert rig.wire.contracts[0]['request']['include_explanations'] is False


def test_resume_cannot_change_a_new_jobs_frozen_presentation_choice(agent_case):
    rig = agent_case(count=1, include_explanations=False,
                     wire_options={'fail_phase': ('agent_review', 1)})
    assert rig.run()['status'] == 'failed'
    calls = rig.text_call_count()
    payload = json.loads(rig.job()['payload']) | {'include_explanations': True}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'failed'
    assert rig.text_call_count() == calls
    assert not rig.contents()


def test_question_audio_reads_prompts_and_options_without_answer_leakage():
    value = asset().model_dump()
    value['questions'][0].update(answer='PRIVATE ANSWER', explanation='PRIVATE EXPLANATION',
                                options=['First option', 'Second option'])
    transcript = student_audio_transcript(value)
    assert value['title'] in transcript and value['questions'][0]['stem'] in transcript
    assert 'A. First option' in transcript and 'B. Second option' in transcript
    assert 'PRIVATE ANSWER' not in transcript and 'PRIVATE EXPLANATION' not in transcript


def test_teaching_audio_keeps_original_section_behavior():
    value = asset(sections=[{'heading': 'Existing heading', 'text': 'Existing teaching text.',
                            'citation_ids': [SOURCE['id']]}]).model_dump()
    assert student_audio_transcript(value) == 'Existing heading。Existing teaching text.'
