"""Auditable question checks; structural agreement is not semantic proof."""
from typing import Literal

from pydantic import Field

from .models import Model

REVISION = '20260922-quality-contract-v1'


class OptionCheck(Model):
    label: str = Field(pattern=r'^[A-Z]$')
    verdict: Literal['correct', 'incorrect', 'uncertain']
    reason: str = Field(min_length=1, max_length=800)


class QuestionChecks(Model):
    condition_issues: list[str] = Field(max_length=8)
    option_checks: list[OptionCheck] = Field(max_length=26)


CONDITIONS = (
    'State all exercise-specific assumptions that affect the answer: initial state, existence, '
    'ordering, inequalities, units and relevant boundary cases. Learned definitions need not be '
    'restated, but private reference text cannot fill a missing scenario condition. If the question '
    'intentionally leaves a value open, explicitly request a conditional answer and cover the '
    'allowed cases; do not infer a unique outcome. Check that the described operations actually '
    'produce the claimed intermediate state before reasoning about its consequences. '
    'Every MCQ must have exactly one defensible answer under an ordinary reading of the wording. '
    'Distractors must be substantively wrong, not merely alternate wording of a correct statement. '
    'Explanations must accurately describe each option and its operation order, and must not '
    'introduce missing assumptions to defend the key. Cite text supporting each substantive claim.'
)

DIFFICULTY = (
    'Judge cognitive demands after removing facts and solution steps already supplied in the stem. '
    'Following an explicitly supplied answer or recognizing a paraphrase is not hard. '
    'Hard tasks require the learner to combine learned concepts to evaluate competing decisions '
    'under concrete interacting constraints and justify a choice or counterexample. '
    'Make that reasoning necessary; do not supply the conclusion or complete method in the stem. '
    'Length, terminology, number of subparts and missing assumptions do not establish difficulty.'
)

CHECK_INSTRUCTION = (
    'Return question_checks. condition_issues contains only concrete missing or contradictory '
    'exercise conditions, not optional stylistic preferences. For MCQ return one option_checks '
    'entry per option in order, labels A, B, ... . Independently classify the entire statement '
    'as correct, incorrect or uncertain under ordinary defensible readings and provide a short '
    'verifiable reason. If two options are defensible or a necessary assumption is missing, '
    'do not force the advertised single-answer format to choose one. For short_answer return '
    'option_checks=[]; multiple grounded viewpoints alone do not make an open-ended task ambiguous. '
    'These are concise checking results, not hidden reasoning or a chain-of-thought transcript.'
)


def question_check_issues(question, checks, *, check_key=False, defer_option_verdicts=False):
    issues = ['question_conditions_invalid'] if any(x.strip() for x in checks.condition_issues) else []
    expected = [chr(65 + i) for i in range(len(question.options))] if question.kind == 'mcq' else []
    if [x.label for x in checks.option_checks] != expected:
        return issues + ['option_check_coverage_invalid']
    if not expected or defer_option_verdicts:
        return issues
    correct = [x.label for x in checks.option_checks if x.verdict == 'correct']
    if len(correct) != 1 or any(x.verdict == 'uncertain' for x in checks.option_checks):
        issues.append('ambiguous_options')
    elif check_key and correct[0] != question.answer:
        issues.append('option_key_mismatch')
    return issues


def solver_check_issues(question, solution):
    # A tool-first solver has not seen computation results. It must cover the
    # option labels but may defer truth judgments; the reviewer sees the tools.
    issues = question_check_issues(question, solution.question_checks,
        defer_option_verdicts=bool(solution.tool_requests))
    if question.kind == 'mcq' and not issues and not solution.tool_requests:
        correct = next(x.label for x in solution.question_checks.option_checks if x.verdict == 'correct')
        if solution.answer != correct:
            issues.append('solver_option_answer_inconsistent')
    return issues


def tool_check_scope(solution, results, numerical_cpu=False):
    return {'inputs_match_applicable': bool(solution.tool_requests or results),
        'calculation_required': bool(solution.requires_calculation or numerical_cpu or solution.tool_requests or results),
        'note': 'Input matching is not applicable when there are no requested or executed tools. '
                'calculations_verified still must be true: missing required computation is never excused by absent tools.'}


def review_boolean_issues(review, scope):
    return [key for key, value in review.model_dump().items()
        if type(value) is bool and not value
        and not (key == 'tool_inputs_match_question' and not scope['inputs_match_applicable'])]
