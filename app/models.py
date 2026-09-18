from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

DifficultyLevel = Literal['easy', 'medium', 'hard']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class CourseCreate(Model):
    name: str = Field(min_length=1)

class DifficultyDistribution(Model):
    easy: int = Field(ge=0, le=10, strict=True)
    medium: int = Field(ge=0, le=10, strict=True)
    hard: int = Field(ge=0, le=10, strict=True)

class GenerateRequest(Model):
    course_id: str
    topic: str = Field(min_length=2)
    material: Literal['lesson', 'quiz', 'assignment'] = 'quiz'
    difficulty: DifficultyLevel = 'medium'
    difficulty_distribution: DifficultyDistribution | None = None
    learner_profile: str = Field(default='已学习所选课程资料的本科生', min_length=1)
    question_type: Literal['mcq', 'short_answer', 'mixed'] = 'mixed'
    count: int = Field(default=3, ge=1, le=10, strict=True)
    language: Literal['zh', 'en'] = 'zh'
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    request_key: str = Field(min_length=8, max_length=100)
    @model_validator(mode='after')
    def difficulty_allocation(self):
        if self.difficulty_distribution is not None:
            if self.material == 'lesson':
                raise ValueError('学习讲解只设置讲解深度，不分配题目难度。')
            if sum(self.difficulty_distribution.model_dump().values()) != self.count:
                raise ValueError('简单、中等、困难的题数之和必须等于题目总数；每档可以为 0。')
        return self

class Section(Model):
    heading: str = Field(min_length=1)
    text: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=1, max_length=10)

class DifficultyDesign(Model):
    cognitive_process: Literal['remember', 'understand', 'apply', 'analyze', 'evaluate', 'create']
    concepts: list[str] = Field(min_length=1, max_length=6)
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
    options: list[str] = Field(default_factory=list, max_length=6)
    answer: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    difficulty_reason: str = Field(min_length=1)
    citation_ids: list[str] = Field(min_length=1, max_length=10)
    @model_validator(mode='after')
    def check_options(self):
        if self.kind == 'mcq':
            if len(self.options) != 4 or len(set(self.options)) != 4:
                raise ValueError('选择题需要四个不同选项')
            if self.answer not in ('A', 'B', 'C', 'D'):
                raise ValueError('选择题答案只能为 A/B/C/D 中的一个')
        elif self.options:
            raise ValueError('简答题不得包含选项')
        return self

class LearningAsset(Model):
    title: str = Field(min_length=1)
    evidence_sufficient: bool
    evidence_note: str = ''
    learning_objectives: list[str] = Field(default_factory=list, max_length=10)
    sections: list[Section] = Field(default_factory=list, max_length=10)
    questions: list[Question] = Field(default_factory=list, max_length=10)
    visual_prompt: str = ''
    @model_validator(mode='after')
    def evidence_contract(self):
        if self.evidence_sufficient and not self.sections:
            raise ValueError('材料必须包含学习讲解')
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
    question_difficulties: list[QuestionDifficultyRating] = Field(default_factory=list, max_length=10)
