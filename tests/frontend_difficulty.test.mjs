import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ensureDifficultyDraft, difficultyValidation, generationPayload,
  difficultySummary, readQuestionDifficulties, renderQuestionDifficulty,
  renderDifficultyAssessment, renderQuestionDifficultyEvaluation,
} from '../app/static/difficulty.js';

globalThis.document = {documentElement:{lang:'zh-CN'}};
const draft = overrides => ensureDifficultyDraft({
  material:'quiz', count:5, difficulty:'hard', topic:'Preserve this topic',
  language:'en', question_type:'short_answer', ...overrides,
});
const evaluationForm = values => ({
  querySelectorAll: () => values.map((value,index)=>({value,dataset:{teacherDifficulty:`q${index+1}`}})),
});

test('uniform difficulty means every question, with no imposed distribution', () => {
  const request = draft();
  assert.equal(difficultyValidation(request),null);
  assert.match(difficultySummary(request),/全部 5 题为困难/);
  const payload = generationPayload(request);
  assert.equal(payload.difficulty,'hard');
  assert.equal(payload.difficulty_distribution,null);
  assert.equal(payload.count,5);
});

test('explicit allocation permits zero levels and an entirely hard set', () => {
  const request = draft({difficulty_mode:'distribution',difficulty_counts:{easy:'0',medium:'0',hard:'5'}});
  assert.equal(difficultyValidation(request),null);
  assert.deepEqual(generationPayload(request).difficulty_distribution,{easy:0,medium:0,hard:5});
});

test('invalid or mismatched allocations are blocked without redistribution', () => {
  const invalid = [
    {easy:'',medium:'0',hard:'5'},
    {easy:'-1',medium:'0',hard:'6'},
    {easy:'0.5',medium:'0',hard:'4.5'},
    {easy:'0',medium:'0',hard:'4'},
    {easy:'0',medium:'0',hard:'6'},
  ];
  for (const counts of invalid) {
    const request = draft({difficulty_mode:'distribution',difficulty_counts:counts});
    const before = structuredClone(counts);
    assert.ok(difficultyValidation(request));
    assert.deepEqual(request.difficulty_counts,before);
  }
});

test('changing the total preserves the teacher allocation and reports the difference', () => {
  const request = draft({difficulty_mode:'distribution',difficulty_counts:{easy:'1',medium:'1',hard:'3'}});
  request.count = 7;
  assert.deepEqual(request.difficulty_counts,{easy:'1',medium:'1',hard:'3'});
  assert.deepEqual(difficultyValidation(request),{key:'还需分配 {n} 题。',values:{n:2}});
  request.count = 4;
  assert.deepEqual(difficultyValidation(request),{key:'分配超出 {n} 题，请减少。',values:{n:1}});
});

test('API payload excludes UI state and keeps authored inputs unchanged', () => {
  const request = draft({difficulty_mode:'distribution',difficulty_counts:{easy:'1',medium:'1',hard:'3'}});
  const before = structuredClone(request);
  const payload = generationPayload(request);
  for (const field of ['difficulty_mode','difficulty_counts','lesson_difficulty']) assert.equal(field in payload,false);
  assert.equal(payload.topic,'Preserve this topic');
  assert.equal(payload.language,'en');
  assert.equal(payload.learner_profile,'已学习所选课程资料的本科生');
  assert.deepEqual(request,before);
});

test('lesson depth, uniform choice, and custom counts retain independent drafts', () => {
  const request = draft({difficulty_mode:'distribution',difficulty_counts:{easy:'1',medium:'1',hard:'3'}});
  request.material = 'lesson';
  request.lesson_difficulty = 'easy';
  assert.equal(generationPayload(request).difficulty,'easy');
  assert.equal(generationPayload(request).difficulty_distribution,null);
  assert.equal(difficultyValidation(request),null);
  request.material = 'assignment';
  assert.equal(generationPayload(request).difficulty,'hard');
  assert.deepEqual(generationPayload(request).difficulty_distribution,{easy:1,medium:1,hard:3});
  request.difficulty_mode = 'uniform';
  assert.equal(generationPayload(request).difficulty,'hard');
  assert.equal(generationPayload(request).difficulty_distribution,null);
  request.difficulty_mode = 'distribution';
  assert.deepEqual(generationPayload(request).difficulty_distribution,{easy:1,medium:1,hard:3});
  assert.equal(request.lesson_difficulty,'easy');
});

test('per-question teacher judgments can be omitted as an entire group', () => {
  assert.deepEqual(readQuestionDifficulties(evaluationForm(['','',''])),[]);
  assert.deepEqual(readQuestionDifficulties(evaluationForm([])),[]);
});

test('partial teacher judgments are rejected while uncertain is an explicit assessment', () => {
  assert.throws(()=>readQuestionDifficulties(evaluationForm(['hard','','easy'])),/全部填写/);
  assert.deepEqual(readQuestionDifficulties(evaluationForm(['hard','uncertain','easy'])),[
    {slot_id:'q1',assessed_difficulty:'hard'},
    {slot_id:'q2',assessed_difficulty:'uncertain'},
    {slot_id:'q3',assessed_difficulty:'easy'},
  ]);
});

test('legacy target levels are not invented and edited versions cannot claim a current model check', () => {
  assert.match(renderQuestionDifficulty({},0),/未记录/);
  const content = {version:2,asset:{questions:[{slot_id:'q1',stem:'Explain the method.'}]},difficulty_assessment:{status:'model_checked'}};
  assert.match(renderDifficultyAssessment(content),/当前版本需要重新判断难度/);
  assert.doesNotMatch(renderDifficultyAssessment(content),/模型已检查，待教师判断/);
  const form = renderQuestionDifficultyEvaluation(content);
  assert.match(form,/value="" selected/);
  assert.match(form,/value="uncertain"/);
  assert.doesNotMatch(form,/value="(?:easy|medium|hard|uncertain)" selected/);
});
