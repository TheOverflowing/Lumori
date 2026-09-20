"""Explicit question plans and a separate, blinded model difficulty check.

These are operational authoring rules, not a claim that Bloom levels measure
empirical learner difficulty. Actual calibration still needs teacher/student data.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


RUBRIC_VERSION = 'difficulty-rubric-v1'
DIFFICULTY_RUBRIC = {
    'easy': {
        'description': '直接记忆或解释资料中的一个明确知识点；条件完整，无需选择复杂策略。',
        'cognitive_processes': ['remember', 'understand'],
        'expected_steps': {'min': 1, 'max': 2},
        'requirement': '评分要点为 1–2 个简短关键步骤；答案能由资料直接支持。',
    },
    'medium': {
        'description': '把已学概念应用到具体情境，或比较、分析相关概念及其关系。',
        'cognitive_processes': ['apply', 'analyze'],
        'expected_steps': {'min': 2, 'max': 3},
        'requirement': '评分要点为 2–3 个相互关联的步骤，需要应用或分析，不能只是改写定义。',
    },
    'hard': {
        'description': '在资料范围内综合概念，处理情境约束，进行有依据的分析、评价或设计。',
        'cognitive_processes': ['analyze', 'evaluate', 'create'],
        'expected_steps': {'min': 3, 'max': 6},
        'requirement': '评分要点为 3–6 个关键步骤；题干必须要求处理具体情境约束并论证判断或设计。'
                       '困难来自知识整合与推理，不能只靠复述、冷门术语、冗长措辞或资料以外的知识。',
    },
}
RUBRIC_NOTE = ('以上为相对于 learner_profile 的操作性出题标准。认知过程和评分步骤是设计约束，'
               '不能单独证明实际难度；不得把 Bloom 层级或步骤数量等同于实测难度。'
               'expected_steps 只写面向学生和教师的简短评分要点，不输出隐藏思维链。')
ASSESSMENT_NOTE = '独立调用的模型复核，尚非教师验证或学习者实测难度。'


class AssetRuleError(ValueError):
    def __init__(self, message, rule, **details):
        super().__init__(message)
        self.rule = rule
        self.details = details


class AssessmentItem(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    slot_id: str = Field(min_length=1)
    assessed_difficulty: Literal['easy', 'medium', 'hard']
    confidence: Literal['low', 'medium', 'high']
    rationale: str = Field(min_length=1)
    answerable_from_sources: StrictBool
    ambiguity_free: StrictBool


class DifficultyAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[AssessmentItem] = Field(min_length=1, max_length=50)


def build_difficulty_plan(request):
    if request.material == 'lesson':
        return []
    distribution = request.difficulty_distribution
    levels = ([level for level in ('easy', 'medium', 'hard')
               for _ in range(getattr(distribution, level))]
              if distribution is not None else [request.difficulty] * request.count)
    return [{'slot_id': f'q{index}', 'difficulty': level}
            for index, level in enumerate(levels, 1)]


def validate_difficulty_design(asset, request):
    """Check the fixed plan and authoring structure before any semantic review."""
    plan = build_difficulty_plan(request)
    if len(asset.questions) != len(plan):
        raise AssetRuleError('题目数量不符合难度计划。', 'difficulty_plan_count_mismatch',
                             expected_questions=len(plan), actual_questions=len(asset.questions))
    for question, slot in zip(asset.questions, plan):
        if question.slot_id != slot['slot_id']:
            raise AssetRuleError('题目槽位或顺序不符合难度计划。', 'difficulty_slot_mismatch',
                                 expected_slot=slot['slot_id'])
        if question.difficulty != slot['difficulty']:
            raise AssetRuleError('题目难度标签不符合难度计划。', 'difficulty_label_mismatch', **slot)
        design = question.difficulty_design
        if design is None:
            raise AssetRuleError('新生成题目缺少难度设计依据。', 'difficulty_design_required', **slot)
        rubric = DIFFICULTY_RUBRIC[slot['difficulty']]
        if design.cognitive_process not in rubric['cognitive_processes']:
            raise AssetRuleError('题目的认知过程不符合该档难度的设计规则。',
                                 'difficulty_cognitive_process_mismatch', **slot,
                                 allowed_processes=rubric['cognitive_processes'])
        if not design.concepts or any(not concept.strip() for concept in design.concepts):
            raise AssetRuleError('难度设计必须列出明确的课程概念。', 'difficulty_concepts_required', **slot)
        step_limits = rubric['expected_steps']
        if (not step_limits['min'] <= len(design.expected_steps) <= step_limits['max']
                or any(not step.strip() for step in design.expected_steps)):
            raise AssetRuleError('简短评分要点的数量或内容不符合该档难度的设计规则。',
                                 'difficulty_expected_steps_mismatch', **slot,
                                 expected_steps=step_limits)


def blind_assessment_contract(asset, sources, learner_profile):
    """Allowlist fields so author labels, plans and rationales cannot prime review."""
    return {
        'task': 'difficulty_assessment',
        'learner_profile': learner_profile,
        'rubric_version': RUBRIC_VERSION,
        'rubric': DIFFICULTY_RUBRIC,
        'rubric_note': RUBRIC_NOTE,
        'questions': [
            {'slot_id': question.slot_id, 'kind': question.kind, 'stem': question.stem,
             'options': question.options, 'answer': question.answer,
             'explanation': question.explanation}
            for question in asset.questions
        ],
        'reference_chunks': sources,
        'schema': DifficultyAssessment.model_json_schema(),
    }


def validate_assessment(raw, plan, *, allow_difficulty_mismatch=False):
    """Malformed assessments raise ValueError; valid failed checks are repairable."""
    assessment = DifficultyAssessment.model_validate(raw)
    ids = [item.slot_id for item in assessment.items]
    expected = {slot['slot_id'] for slot in plan}
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError('难度复核响应必须覆盖全部且唯一的题目槽位。')
    targets = {slot['slot_id']: slot['difficulty'] for slot in plan}
    failures = []
    for item in assessment.items:
        reasons = []
        if not allow_difficulty_mismatch and item.assessed_difficulty != targets[item.slot_id]:
            reasons.append('difficulty_mismatch')
        if item.confidence == 'low':
            reasons.append('low_confidence')
        if not item.answerable_from_sources:
            reasons.append('not_answerable_from_sources')
        if not item.ambiguity_free:
            reasons.append('ambiguous_question')
        if reasons:
            failures.append({'slot_id': item.slot_id, 'target_difficulty': targets[item.slot_id],
                             'assessed_difficulty': item.assessed_difficulty,
                             'confidence': item.confidence, 'reasons': reasons})
    if failures:
        raise AssetRuleError('题目未通过独立难度复核；请调整题目设计或补充资料后重试。',
                             'difficulty_assessment_failed', items=failures)
    return assessment


DIFFICULTY_POLICY = {
    'revision': 'difficulty-calibration-v1',
    'strict_rounds': 3,
    'relaxation_tiers': ['adjacent_level', 'any_level'],
    'correctness_relaxed': False,
    'note': 'After three bounded rounds, reuse the closest candidate whose other quality gates passed. '
            'Requested labels remain authoring targets; assessed levels are reported separately. '
            'Combined low-confidence quality judgments are never relaxed.',
}


def difficulty_acceptance(items, plan, *, rounds, accepted_attempt):
    """Describe the actual model judgment without rewriting the requested plan."""
    targets = {slot['slot_id']: slot['difficulty'] for slot in plan}
    levels = {'easy': 0, 'medium': 1, 'hard': 2}
    result = []
    for raw in items:
        item = raw.model_dump() if hasattr(raw, 'model_dump') else dict(raw)
        target = targets[item['slot_id']]
        distance = abs(levels[target] - levels[item['assessed_difficulty']])
        result.append({k: item[k] for k in ('slot_id', 'assessed_difficulty', 'confidence')} | {
            'target_difficulty': target, 'strict_passed': distance == 0,
            'level_distance': distance})
    distance = max((item['level_distance'] for item in result), default=0)
    reason = 'target_matched' if not distance else 'adjacent_level' if distance == 1 else 'any_level'
    return {'policy_revision': DIFFICULTY_POLICY['revision'],
            'status': 'strict' if not distance else 'adjusted', 'strict_passed': distance == 0,
            'rounds': rounds, 'accepted_attempt': accepted_attempt,
            'acceptance_reason': reason, 'correctness_relaxed': False, 'items': result}


def acceptance_rank(acceptance):
    """Prefer the nearest assessed levels, then stronger joint quality confidence."""
    items = acceptance['items']
    return (max((item['level_distance'] for item in items), default=0),
            sum(item['level_distance'] for item in items),
            -sum({'low': 0, 'medium': 1, 'high': 2}[item['confidence']] for item in items),
            -acceptance['accepted_attempt'])


def combine_difficulty_acceptances(acceptances):
    """Summarize per-question decisions while retaining their own round counts."""
    adjusted = [item for item in acceptances if not item['strict_passed']]
    return {'policy_revision': DIFFICULTY_POLICY['revision'],
            'status': 'adjusted' if adjusted else 'strict', 'strict_passed': not adjusted,
            'rounds': max((item['rounds'] for item in acceptances), default=0),
            'acceptance_reason': ('any_level' if any(item['acceptance_reason'] == 'any_level'
                for item in adjusted) else 'adjacent_level' if adjusted else 'target_matched'),
            'correctness_relaxed': False,
            'items': [entry | {'rounds': item['rounds'], 'accepted_attempt': item['accepted_attempt'],
                'acceptance_reason': item['acceptance_reason']} for item in acceptances for entry in item['items']]}
