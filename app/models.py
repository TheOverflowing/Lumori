from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from pydantic_core import PydanticCustomError

DifficultyLevel = Literal['easy', 'medium', 'hard']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class CourseCreate(Model):
    name: str = Field(min_length=1)

class DifficultyDistribution(Model):
    easy: int = Field(ge=0, le=50, strict=True)
    medium: int = Field(ge=0, le=50, strict=True)
    hard: int = Field(ge=0, le=50, strict=True)

class GenerateRequest(Model):
    course_id: str
    topic: str = Field(min_length=2)
    material: Literal['lesson', 'quiz', 'assignment'] = 'quiz'
    difficulty: DifficultyLevel = 'medium'
    difficulty_distribution: DifficultyDistribution | None = None
    learner_profile: str = Field(default='已学习所选课程资料的本科生', min_length=1)
    question_type: Literal['mcq', 'short_answer', 'mixed'] = 'mixed'
    count: int = Field(default=3, ge=1, le=50, strict=True)
    language: Literal['zh', 'en'] = 'zh'
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    request_key: str = Field(min_length=8, max_length=100)
    query_fusion: bool = Field(default=False, strict=True)
    auto_explore: bool = Field(default=False, strict=True)
    # None keeps historical/server-configured jobs on their original route.
    # New web requests explicitly choose True or False for assessments.
    use_subagents: bool | None = Field(default=None, strict=True)
    # Teaching material before an assessment; answer explanations are unchanged.
    include_explanations: bool = Field(default=False, strict=True)
    preparation_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    clarification_action: Literal['answer', 'unknown', 'skip'] | None = None
    clarification_answer: str = Field(default='', max_length=2000)
    @model_validator(mode='after')
    def difficulty_allocation(self):
        if not self.preparation_id and (self.clarification_action or self.clarification_answer):
            raise ValueError('补充回答必须关联生成前的理解检查。')
        if self.clarification_action == 'answer' and not self.clarification_answer:
            raise ValueError('请填写补充信息，或选择不确定。')
        if self.clarification_action != 'answer' and self.clarification_answer:
            raise ValueError('只有补充回答操作可以包含回答文字。')
        if self.difficulty_distribution is not None:
            if self.material == 'lesson':
                raise ValueError('学习讲解只设置讲解深度，不分配题目难度。')
            if sum(self.difficulty_distribution.model_dump().values()) != self.count:
                raise ValueError('简单、中等、困难的题数之和必须等于题目总数；每档可以为 0。')
        if self.material == 'lesson' and self.use_subagents is True:
            raise ValueError('逐题子代理只适用于测验和作业。')
        return self

    @model_serializer(mode='wrap')
    def preserve_unprepared_request_shape(self, handler):
        values = handler(self)
        # Existing jobs compare their saved configuration exactly on resume.
        # Absent clarification must not add new null/default keys to old payloads.
        if self.preparation_id is None:
            for key in ('preparation_id', 'clarification_action', 'clarification_answer'):
                values.pop(key, None)
        if not self.query_fusion:
            values.pop('query_fusion', None)
        if not self.auto_explore:
            values.pop('auto_explore', None)
        if self.use_subagents is None:
            values.pop('use_subagents', None)
        # Historical preparation fingerprints must keep their original shape.
        # New generation configurations freeze the resolved value explicitly.
        if 'include_explanations' not in self.model_fields_set:
            values.pop('include_explanations', None)
        return values

class IntentGenerateRequest(GenerateRequest):
    """Server-only execution view; the HTTP request remains GenerateRequest."""
    original_topic: str
    clarification_question: str = ''
    clarification_options: list[str] = Field(default_factory=list)
    clarification_note: str = ''

class Section(Model):
    heading: str = Field(min_length=1)
    text: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=1, max_length=10)

class DifficultyDesign(Model):
    cognitive_process: Literal['remember', 'understand', 'apply', 'analyze', 'evaluate', 'create']
    concepts: list[str] = Field(min_length=1)
    expected_steps: list[str] = Field(min_length=1, max_length=8)
    @model_validator(mode='after')
    def meaningful_design(self):
        if any(not item.strip() for item in [*self.concepts, *self.expected_steps]):
            raise ValueError('知识点和评分要点不得为空。')
        return self

class Question(Model):
    # Optional for reading historical drafts. New generations require these fields.
    slot_id: str | None = None
    difficulty: DifficultyLevel | None = None
    difficulty_design: DifficultyDesign | None = None
    kind: Literal['mcq', 'short_answer']
    stem: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, max_length=26)
    answer: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    difficulty_reason: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=1, max_length=10)
    @model_validator(mode='after')
    def check_options(self):
        if self.kind == 'mcq':
            if not 2 <= len(self.options) <= 26:
                raise PydanticCustomError('mcq_option_count', '选择题需要 2 至 26 个不同选项',
                                          {'actual_length': len(self.options)})
            if len(set(self.options)) != len(self.options):
                raise PydanticCustomError('mcq_option_unique', '选择题需要不同选项')
            if any(not option.strip() for option in self.options):
                raise PydanticCustomError('mcq_option_blank', '选择题选项不得为空')
            if self.answer not in tuple(chr(65 + index) for index in range(len(self.options))):
                raise PydanticCustomError('mcq_answer_key', '选择题答案必须为现有选项对应的单个大写字母',
                                          {'option_count': len(self.options)})
        elif self.options:
            raise PydanticCustomError('short_answer_options', '简答题不得包含选项',
                                      {'actual_length': len(self.options)})
        return self

class LearningAsset(Model):
    title: str = Field(min_length=1)
    evidence_sufficient: bool
    evidence_note: str = ''
    learning_objectives: list[str] = Field(default_factory=list, max_length=10)
    section_scope: Literal['shared', 'per_question'] = 'shared'
    # A supervisor may preserve one scoped material block for each of 50 questions.
    # Individual author calls still request at most two teaching sections.
    sections: list[Section] = Field(default_factory=list, max_length=50)
    questions: list[Question] = Field(default_factory=list, max_length=50)
    visual_prompt: str = ''
    @model_validator(mode='after')
    def evidence_contract(self):
        if self.section_scope == 'shared' and len(self.sections) > 10:
            raise ValueError('共享学习讲解最多包含 10 个小节。')
        if self.section_scope == 'per_question' and (
                not self.questions or len(self.sections) != len(self.questions)
                or [q.slot_id for q in self.questions] != [f'q{i+1}' for i in range(len(self.questions))]):
            raise ValueError('逐题资料必须与完整有序的题目槽位一一对应。')
        if self.evidence_sufficient and not (self.sections or self.questions):
            raise ValueError('材料必须包含学习讲解或题目')
        if not self.evidence_sufficient and (self.sections or self.questions):
            raise ValueError('证据不足时不得返回教学结论或题目')
        return self

class ReviewRequest(Model):
    version: int = Field(ge=1)
    action: Literal['save', 'approve']
    asset: LearningAsset | None = None

class MediaRequest(Model):
    version: int = Field(ge=1)
    kind: Literal['audio', 'image']
    request_key: str = Field(min_length=8, max_length=100)

class MediaReview(Model):
    version: int = Field(ge=1)

class QuestionDifficultyRating(Model):
    slot_id: str = Field(min_length=1)
    assessed_difficulty: Literal['easy', 'medium', 'hard', 'uncertain']

class EvaluationRequest(Model):
    version: int = Field(ge=1)
    correctness: int = Field(ge=1, le=5)
    groundedness: int = Field(ge=1, le=5)
    difficulty_match: int = Field(ge=1, le=5)
    notes: str = ''
    question_difficulties: list[QuestionDifficultyRating] = Field(default_factory=list, max_length=50)
