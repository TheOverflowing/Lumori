import test from 'node:test';
import assert from 'node:assert/strict';
import { restoreGenerationDraft } from '../app/static/generation-retry.js';
import { difficultyValidation, ensureDifficultyDraft, generationPayload } from '../app/static/difficulty.js';

const request = (overrides = {}) => ({
  material:'quiz',topic:'Explain how a cache works. / 解释缓存如何工作。',
  difficulty:'hard',language:'en',question_type:'short_answer',count:7,
  learner_profile:'Undergraduates with basic Python knowledge / 具备 Python 基础的本科生',
  document_ids:['course-notes','lecture-slides'],query_fusion:true,auto_explore:true,
  ...overrides,
});

test('uniform retry restores all user inputs and the matching difficulty count draft', () => {
  const saved=request();
  const draft=restoreGenerationDraft(saved);
  assert.deepEqual(draft,{
    material:'quiz',topic:saved.topic,difficulty:'hard',language:'en',question_type:'short_answer',count:7,
    difficulty_mode:'uniform',difficulty_counts:{easy:0,medium:0,hard:7},lesson_difficulty:'hard',
    learner_profile_is_default:false,learner_profile:saved.learner_profile,
    document_ids:['course-notes','lecture-slides'],query_fusion:true,auto_explore:true,include_explanations:false,
  });
  assert.equal(difficultyValidation(draft),null);
  assert.equal(generationPayload(draft).difficulty,'hard');
});

test('zero-count levels and an all-hard custom allocation round trip without redistribution', () => {
  const distribution={easy:0,medium:0,hard:50};
  const draft=restoreGenerationDraft(request({count:50,difficulty_distribution:distribution}));
  assert.equal(draft.difficulty_mode,'distribution');
  assert.deepEqual(draft.difficulty_counts,distribution);
  assert.equal(difficultyValidation(draft),null);
  assert.deepEqual(generationPayload(draft).difficulty_distribution,distribution);
});

test('a mixed saved distribution becomes the existing editable allocation controls', () => {
  const draft=restoreGenerationDraft(request({material:'assignment',count:6,difficulty_distribution:{easy:1,medium:2,hard:3}}));
  assert.equal(draft.material,'assignment');
  assert.equal(draft.difficulty_mode,'distribution');
  assert.deepEqual(draft.difficulty_counts,{easy:1,medium:2,hard:3});
  assert.equal(difficultyValidation(draft),null);
});

test('lesson depth restores independently and a legacy question distribution cannot affect lesson export', () => {
  const draft=restoreGenerationDraft(request({material:'lesson',difficulty:'easy',count:3,difficulty_distribution:{easy:0,medium:0,hard:3}}));
  assert.equal(draft.lesson_difficulty,'easy');
  assert.equal(draft.difficulty_mode,'uniform');
  assert.deepEqual(draft.difficulty_counts,{easy:3,medium:0,hard:0});
  const payload=generationPayload(draft);
  assert.equal(payload.difficulty,'easy');
  assert.equal(payload.difficulty_distribution,null);
});

test('explicit saved learner profile stays authored text after changing interface language', () => {
  const original=globalThis.document;
  try {
    globalThis.document={documentElement:{lang:'en'}};
    const profile='已学习所选课程资料的本科生';
    const draft=restoreGenerationDraft(request({learner_profile:profile}));
    assert.equal(draft.learner_profile_is_default,false);
    assert.equal(ensureDifficultyDraft(draft).learner_profile,profile);
    document.documentElement.lang='zh-CN';
    assert.equal(ensureDifficultyDraft(draft).learner_profile,profile);
  } finally {
    if(original===undefined)delete globalThis.document;else globalThis.document=original;
  }
});

test('missing legacy profile lets the existing form supply the current interface default', () => {
  const original=globalThis.document;
  try {
    delete globalThis.document;
    const draft=restoreGenerationDraft(request({learner_profile:undefined}));
    assert.equal(draft.learner_profile_is_default,true);
    assert.equal(Object.hasOwn(draft,'learner_profile'),false);
    globalThis.document={documentElement:{lang:'en'}};
    ensureDifficultyDraft(draft);
    assert.equal(typeof draft.learner_profile,'string');
    assert.notEqual(draft.learner_profile,'已学习所选课程资料的本科生');
  } finally {
    if(original===undefined)delete globalThis.document;else globalThis.document=original;
  }
});

test('resolved topic is preserved exactly and execution identities or clarification tokens cannot leak into a retry', () => {
  const topic='  Context supplied by the teacher:\nUse a hash table, not a hardware cache.  ';
  const draft=restoreGenerationDraft(request({
    topic,original_topic:'ambiguous cache request',course_id:'server-owned-course',
    request_key:'old-idempotency-key',preparation_id:'old-preparation',clarification_action:'answer',
    clarification_answer:'old answer',clarification_question:'old question',clarification_options:['old'],
    model:'private-model',api_key:'fixture-not-a-real-key',source_evidence:[{secret:'not a form field'}],
    difficulty_plan:[{slot_id:'q1',difficulty:'hard'}],prompt_version:'old-prompt',
  }));
  assert.equal(draft.topic,topic);
  const allowed=['material','topic','difficulty','language','question_type','count','learner_profile','document_ids',
    'query_fusion','auto_explore','include_explanations','difficulty_mode','difficulty_counts','lesson_difficulty','learner_profile_is_default'];
  assert.deepEqual(Object.keys(draft).sort(),allowed.sort());
  for(const key of ['request_key','preparation_id','clarification_action','clarification_answer','original_topic','model','api_key','course_id']) {
    assert.equal(Object.hasOwn(generationPayload(draft),key),false,key);
  }
});

test('restored flags use strict booleans and old omitted flags remain explicitly off', () => {
  for(const value of [undefined,null,false,'true',1,{}]) {
    const draft=restoreGenerationDraft(request({query_fusion:value,auto_explore:value}));
    assert.equal(draft.query_fusion,false);
    assert.equal(draft.auto_explore,false);
  }
});

test('retry preserves the saved introduction choice while omitted or malformed flags remain off', () => {
  for(const value of [true,false,undefined,null,'true',1,{}]) {
    const draft=restoreGenerationDraft(request({include_explanations:value}));
    assert.equal(draft.include_explanations,value===true);
    assert.equal(generationPayload(draft).include_explanations,value===true);
    assert.equal(generationPayload({...draft,material:'lesson'}).include_explanations,false);
  }
});

test('restoring does not mutate or share the saved request collections', () => {
  const saved=request({difficulty_distribution:{easy:1,medium:2,hard:4},document_ids:['a','b','a',null,'']});
  const before=structuredClone(saved);
  const draft=restoreGenerationDraft(saved);
  assert.deepEqual(draft.document_ids,['a','b']);
  draft.document_ids.push('new');
  draft.difficulty_counts.hard=0;
  assert.deepEqual(saved,before);
  assert.deepEqual(restoreGenerationDraft(saved).difficulty_counts,{easy:1,medium:2,hard:4});
});

test('inconsistent historical allocations remain visible for correction rather than silently becoming uniform', () => {
  const mismatch=restoreGenerationDraft(request({count:7,difficulty_distribution:{easy:1,medium:1,hard:1}}));
  assert.equal(mismatch.difficulty_mode,'distribution');
  assert.deepEqual(mismatch.difficulty_counts,{easy:1,medium:1,hard:1});
  assert.ok(difficultyValidation(mismatch));
  const malformed=restoreGenerationDraft(request({difficulty_distribution:{easy:0,medium:-1,hard:'many'}}));
  assert.deepEqual(malformed.difficulty_counts,{easy:0,medium:'',hard:''});
  assert.ok(difficultyValidation(malformed));
});

test('legacy missing or malformed scalar fields produce a usable fresh form without DOM or storage', () => {
  const original=globalThis.document;
  try {
    delete globalThis.document;
    for(const input of [null,undefined,[],{},'invalid']) {
      const draft=restoreGenerationDraft(input);
      assert.equal(draft.material,'lesson');
      assert.equal(draft.topic,'');
      assert.equal(draft.count,3);
      assert.equal(draft.language,'zh');
      assert.equal(draft.difficulty,'medium');
      assert.deepEqual(draft.document_ids,[]);
    }
    const invalid=restoreGenerationDraft(request({material:'other',difficulty:'expert',language:'de',question_type:'essay',count:99}));
    assert.equal(invalid.material,'lesson');
    assert.equal(invalid.question_type,'mixed');
    assert.equal(invalid.count,3);
  } finally {
    if(original===undefined)delete globalThis.document;else globalThis.document=original;
  }
});
