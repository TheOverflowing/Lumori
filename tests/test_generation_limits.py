"""Bounded question counts across authoring, review, and difficulty checks."""
import pytest
from pydantic import ValidationError

from app.difficulty import DifficultyAssessment
from app.models import DifficultyDistribution, EvaluationRequest, GenerateRequest, LearningAsset


def generation(**changes):
    return GenerateRequest.model_validate({
        'course_id': 'fixture-course', 'topic': 'Compare scheduling policies',
        'request_key': 'fixture-question-limit', **changes,
    })


@pytest.mark.parametrize('material', ['quiz', 'assignment'])
@pytest.mark.parametrize('count', [1, 11, 50])
def test_question_requests_accept_counts_through_fifty(material, count):
    assert generation(material=material, count=count).count == count


@pytest.mark.parametrize('count', [0, -1, 51, True, 50.0, '50'])
def test_question_request_count_remains_a_bounded_strict_integer(count):
    with pytest.raises(ValidationError):
        generation(count=count)


@pytest.mark.parametrize('level', ['easy', 'medium', 'hard'])
def test_each_difficulty_can_receive_all_fifty_questions(level):
    distribution = {'easy': 0, 'medium': 0, 'hard': 0, level: 50}
    request = generation(count=50, difficulty_distribution=distribution)
    assert request.difficulty_distribution.model_dump() == distribution
    with pytest.raises(ValidationError):
        DifficultyDistribution.model_validate(distribution | {level: 51})


def test_fifty_question_mixed_distribution_requires_exact_total():
    distribution = {'easy': 20, 'medium': 15, 'hard': 15}
    assert generation(count=50, difficulty_distribution=distribution).difficulty_distribution.model_dump() == distribution
    for hard in (14, 16):
        with pytest.raises(ValidationError, match='之和必须等于题目总数'):
            generation(count=50, difficulty_distribution=distribution | {'hard': hard})


@pytest.mark.parametrize('value', [-1, 0.5, False, '0'])
def test_distribution_entries_keep_strict_nonnegative_integer_rule(value):
    with pytest.raises(ValidationError):
        DifficultyDistribution(easy=value, medium=0, hard=50)


def test_lesson_keeps_depth_only_and_rejects_question_allocation():
    request = generation(material='lesson', difficulty='hard')
    assert request.count == 3 and request.difficulty_distribution is None
    with pytest.raises(ValidationError, match='不分配题目难度'):
        generation(material='lesson', count=50, difficulty_distribution={'easy': 0, 'medium': 0, 'hard': 50})


def asset(question_count=50, section_count=1):
    return {
        'title': 'Fixture questions', 'evidence_sufficient': True,
        'sections': [{'heading': 'Context', 'text': 'Source-backed explanation.', 'citation_ids': ['source-1']} for _ in range(section_count)],
        'questions': [
            {'slot_id': f'q{index + 1}', 'kind': 'short_answer', 'stem': 'Explain the stated rule.',
             'answer': 'A supported answer.', 'explanation': 'A supported explanation.',
             'difficulty_reason': 'A direct explanation.', 'citation_ids': ['source-1']}
            for index in range(question_count)
        ],
    }


def test_learning_asset_accepts_fifty_questions_and_rejects_fifty_one():
    assert len(LearningAsset.model_validate(asset()).questions) == 50
    with pytest.raises(ValidationError):
        LearningAsset.model_validate(asset(question_count=51))


def test_section_limit_and_evidence_rules_do_not_expand_with_question_limit():
    assert len(LearningAsset.model_validate(asset(section_count=10)).sections) == 10
    with pytest.raises(ValidationError):
        LearningAsset.model_validate(asset(section_count=11))
    assert LearningAsset.model_validate(asset(section_count=0)).questions
    with pytest.raises(ValidationError, match='必须包含学习讲解或题目'):
        LearningAsset.model_validate(asset(section_count=0, question_count=0))
    with pytest.raises(ValidationError, match='证据不足'):
        LearningAsset.model_validate(asset() | {'evidence_sufficient': False})


def test_scoped_materials_preserve_all_fifty_question_contexts():
    value = LearningAsset.model_validate(asset(section_count=50) | {'section_scope': 'per_question'})
    assert len(value.sections) == len(value.questions) == 50
    assert value.section_scope == 'per_question'


@pytest.mark.parametrize('defect', ['missing_section', 'extra_section', 'missing_slot', 'duplicate_slot', 'empty'])
def test_scoped_materials_require_exact_ordered_question_alignment(defect):
    value = asset(question_count=2, section_count=2) | {'section_scope': 'per_question'}
    if defect == 'missing_section': value['sections'].pop()
    elif defect == 'extra_section': value['sections'].append(value['sections'][0])
    elif defect == 'missing_slot': value['questions'][1].pop('slot_id')
    elif defect == 'duplicate_slot': value['questions'][1]['slot_id'] = 'q1'
    elif defect == 'empty': value.update(questions=[], sections=[], evidence_sufficient=False)
    with pytest.raises(ValidationError, match='逐题资料'):
        LearningAsset.model_validate(value)


def test_teacher_evaluation_accepts_all_fifty_ratings_but_rejects_fifty_one():
    ratings = [{'slot_id': f'q{index + 1}', 'assessed_difficulty': 'medium'} for index in range(51)]
    base = {'version': 1, 'correctness': 4, 'groundedness': 4, 'difficulty_match': 4}
    assert len(EvaluationRequest.model_validate(base | {'question_difficulties': ratings[:50]}).question_difficulties) == 50
    assert EvaluationRequest.model_validate(base).question_difficulties == []
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(base | {'question_difficulties': ratings})


def test_model_assessment_accepts_fifty_items_but_rejects_fifty_one():
    items = [
        {'slot_id': f'q{index + 1}', 'assessed_difficulty': 'medium', 'confidence': 'high',
         'rationale': 'Applies the supplied rule.', 'answerable_from_sources': True, 'ambiguity_free': True}
        for index in range(51)
    ]
    assert len(DifficultyAssessment(items=items[:50]).items) == 50
    with pytest.raises(ValidationError):
        DifficultyAssessment(items=items)
