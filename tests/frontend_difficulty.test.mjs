import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ensureDifficultyDraft, difficultyValidation, generationPayload,
  difficultySummary, readQuestionDifficulties, renderQuestionDifficulty,
  renderDifficultyAssessment, renderQuestionDifficultyEvaluation,
  renderDifficultyControls, bindDifficultyPresets,
  renderExplanationOption, updateExplanationOption,
} from '../app/static/difficulty.js';
import { t } from '../app/static/i18n.js';

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

test('assessments default to questions only and explicit introductions require a real boolean', () => {
  for (const material of ['quiz','assignment']) {
    const request=draft({material});
    assert.equal(request.include_explanations,false);
    assert.equal(generationPayload(request).include_explanations,false);
    for(const value of [true,false,undefined,null,'true',1]) {
      request.include_explanations=value;
      assert.equal(generationPayload(request).include_explanations,value===true);
    }
  }
});

test('the introduction switch stays native and lessons hide and disable it before first update', () => {
  const hidden=renderExplanationOption(draft({material:'lesson',include_explanations:true}));
  assert.match(hidden,/<fieldset[^>]*hidden disabled/);
  assert.match(hidden,/<label[^>]*for="include-explanations"/);
  assert.match(hidden,/<input type="checkbox" role="switch" id="include-explanations" checked/);
  const visible=renderExplanationOption(draft({material:'quiz'}));
  assert.doesNotMatch(visible,/<fieldset[^>]*(?:hidden|disabled)/);
  assert.doesNotMatch(visible,/<input[^>]*checked/);
  assert.match(visible,/data-i18n="添加题前讲解"/);
});

test('switching assessment types, lessons and interface languages preserves the introduction choice and authored inputs', () => {
  const request=draft({include_explanations:false});
  const group={hidden:false,disabled:false}, control={checked:false,disabled:false};
  const form={querySelector:selector=>selector==='#explanation-option'?group:control};
  const topic=request.topic, profile=request.learner_profile;
  const beforeLanguage=document.documentElement.lang;
  try {
    control.checked=true;
    updateExplanationOption(form,request);
    assert.equal(generationPayload(request).include_explanations,true);
    request.material='lesson';
    updateExplanationOption(form,request);
    assert.equal(group.hidden,true);
    assert.equal(group.disabled,true);
    assert.equal(control.disabled,true);
    assert.equal(control.checked,true);
    assert.equal(request.include_explanations,true);
    assert.equal(generationPayload(request).include_explanations,false);
    request.material='assignment';
    document.documentElement.lang='en';
    updateExplanationOption(form,request);
    assert.equal(group.hidden,false);
    assert.equal(group.disabled,false);
    assert.equal(control.disabled,false);
    assert.equal(generationPayload(request).include_explanations,true);
    assert.equal(request.topic,topic);
    assert.equal(request.learner_profile,profile);
    control.checked=false;
    updateExplanationOption(form,request);
    assert.equal(generationPayload(request).include_explanations,false);
  } finally { document.documentElement.lang=beforeLanguage; }
});

test('explicit allocation permits zero levels and an entirely hard set', () => {
  const request = draft({difficulty_mode:'distribution',difficulty_counts:{easy:'0',medium:'0',hard:'5'}});
  assert.equal(difficultyValidation(request),null);
  assert.deepEqual(generationPayload(request).difficulty_distribution,{easy:0,medium:0,hard:5});
});

test('50 questions can use one level or a mixed allocation without changing the request', () => {
  for (const counts of [{easy:'0',medium:'0',hard:'50'},{easy:'20',medium:'15',hard:'15'}]) {
    const request = draft({count:50,difficulty_mode:'distribution',difficulty_counts:counts});
    const before = structuredClone(request);
    assert.equal(difficultyValidation(request),null);
    assert.equal(generationPayload(request).count,50);
    assert.deepEqual(generationPayload(request).difficulty_distribution,
      Object.fromEntries(Object.entries(counts).map(([level,value])=>[level,Number(value)])));
    assert.deepEqual(request,before);
  }
  assert.equal(difficultyValidation(draft({count:50})),null);
});

test('total and per-level limits report the same 50-question boundary in both languages', () => {
  try {
    for (const [language,totalMessage,levelMessage] of [
      ['zh-CN','请将题目数量设为 1 至 50 的整数。','每档题数必须是 0 至 50 的整数。'],
      ['en','Set the total question count to an integer from 1 to 50.','Each difficulty count must be an integer from 0 to 50.'],
    ]) {
      document.documentElement.lang = language;
      for (const count of [0,51,1.5]) {
        for (const difficulty_mode of ['uniform','distribution']) {
          assert.equal(t(difficultyValidation(draft({count,difficulty_mode}))),totalMessage);
        }
      }
      for (const level of ['easy','medium','hard']) {
        const counts = {easy:'0',medium:'0',hard:'0',[level]:'51'};
        assert.equal(t(difficultyValidation(draft({count:50,difficulty_mode:'distribution',difficulty_counts:counts}))),levelMessage);
      }
      const html = renderDifficultyControls(draft({count:50}));
      const fields = [...html.matchAll(/<input[^>]+data-difficulty-count="[^"]+"[^>]*>/g)];
      assert.equal(fields.length,3);
      for (const [field] of fields) assert.match(field,/min="0" max="50" step="1"/);
    }
  } finally {
    document.documentElement.lang = 'zh-CN';
  }
});

test('50-question allocations still require the exact total', () => {
  assert.deepEqual(difficultyValidation(draft({count:50,difficulty_mode:'distribution',difficulty_counts:{easy:'20',medium:'15',hard:'14'}})),
    {key:'还需分配 {n} 题。',values:{n:1}});
  assert.deepEqual(difficultyValidation(draft({count:50,difficulty_mode:'distribution',difficulty_counts:{easy:'20',medium:'15',hard:'16'}})),
    {key:'分配超出 {n} 题，请减少。',values:{n:1}});
});

test('all-at-one-level presets allocate 50 and reject 51 without replacing draft values', () => {
  for (const level of ['easy','medium','hard']) {
    const request = draft({count:50});
    const button = {dataset:{difficultyPreset:level}};
    const fields = ['easy','medium','hard'].map(level=>({value:'1',dataset:{difficultyCount:level}}));
    let updates = 0, invalidReports = 0;
    const form = {
      querySelectorAll:selector=>selector==='[data-difficulty-preset]'?[button]:fields,
      querySelector:()=>({reportValidity:()=>invalidReports++}),
    };
    bindDifficultyPresets(form,request,()=>updates++);
    button.onclick();
    assert.equal(updates,1);
    assert.deepEqual(fields.map(field=>field.value),fields.map(field=>field.dataset.difficultyCount===level?50:0));
    const before = fields.map(field=>field.value);
    request.count = 51;
    button.onclick();
    assert.equal(updates,1);
    assert.equal(invalidReports,1);
    assert.deepEqual(fields.map(field=>field.value),before);
  }
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

test('an adjusted question keeps requested and assessed difficulty distinct in both interface languages', () => {
  const question={slot_id:'q2',difficulty:'hard',stem:'Compare the methods.',difficulty_reason:'Combine two concepts.'};
  const assessment={status:'model_adjusted',items:[
    {slot_id:'q1',assessed_difficulty:'easy',confidence:'high'},
    {slot_id:'q2',assessed_difficulty:'medium',confidence:'high',rationale:'Synthetic judgment for UI testing.'},
  ]};
  const before=structuredClone({question,assessment});
  try {
    for(const [lang,target,assessed] of [['zh-CN','困难','中等'],['en','Hard','Medium']]) {
      document.documentElement.lang=lang;
      const html=renderQuestionDifficulty(question,1,assessment);
      const visible=html.slice(0,html.indexOf('<details'));
      assert.match(visible,new RegExp(`<strong>[\\s\\S]*?>${target}</span></strong>`));
      assert.match(visible,new RegExp(`class="difficulty-adjusted-label"[\\s\\S]*?>${assessed}</span>`));
      assert.match(html,/Synthetic judgment for UI testing\./);
    }
  } finally {document.documentElement.lang='zh-CN';}
  assert.deepEqual({question,assessment},before);
});

test('only questions whose assessed level differs receive the adjusted inline label', () => {
  const question={slot_id:'q1',difficulty:'hard',stem:'Compare the methods.'};
  for(const assessment of [
    {status:'model_adjusted',items:[{slot_id:'q1',assessed_difficulty:'hard',confidence:'high'}]},
    {status:'model_adjusted',items:[{slot_id:'q2',assessed_difficulty:'easy',confidence:'high'}]},
    {status:'needs_review',items:[{slot_id:'q1',assessed_difficulty:'easy',confidence:'high'}]},
  ]) {
    assert.doesNotMatch(renderQuestionDifficulty(question,0,assessment),/class="difficulty-adjusted-label"/);
  }
});

test('adjusted material discloses the difference without claiming exact difficulty matching or human review', () => {
  const content={version:1,asset:{questions:[{slot_id:'q1',stem:'Question'}]},difficulty_assessment:{status:'model_adjusted'}};
  const html=renderDifficultyAssessment(content);
  assert.match(html,/已保留结果，难度有偏差/);
  assert.match(html,/<details class="difficulty-adjustment-detail"><summary>/);
  assert.doesNotMatch(html,/<details class="difficulty-adjustment-detail"[^>]*\bopen\b/);
  assert.match(html,/这份结果未严格匹配目标难度，仍需人工核对。/);
  assert.doesNotMatch(html,/模型已检查，待教师判断/);
});

test('editing invalidates both strict and adjusted assessment banners until the new version is reviewed', () => {
  for(const version of [2,7])for(const status of ['model_checked','model_adjusted']) {
    const content={version,asset:{questions:[{slot_id:'q1',stem:'Updated question'}]},difficulty_assessment:{status,version:1}};
    const html=renderDifficultyAssessment(content);
    assert.match(html,/当前版本需要重新判断难度/);
    assert.doesNotMatch(html,/已保留结果，难度有偏差|模型已检查，待教师判断|class="difficulty-adjustment-detail"/);
  }
  const invalidated=renderQuestionDifficulty({slot_id:'q1',difficulty:'hard'},0,
    {status:'needs_review',version:1,items:[{slot_id:'q1',assessed_difficulty:'easy',rationale:'Prior version only.'}]});
  assert.doesNotMatch(invalidated,/模型判断|Prior version only\./);
});

test('shadow disagreement appears for current content but stale evidence does not grade the edited question', () => {
  const content={version:1,asset:{questions:[{slot_id:'q1',stem:'Compare windows.',difficulty:'hard'}]},
    difficulty_assessment:{status:'model_adjusted'},
    difficulty_shadow:{matches_current_content:true,questions:[{slot_id:'q1',candidates:['easy','medium'],
      manual_review_recommended:true,review_flags:['published_grade_outside_candidates','difficulty_boundary']}]}};
  const current=renderDifficultyAssessment(content);
  assert.match(current,/旁路评审建议人工复核/);
  assert.match(current,/当前标注的难度未进入旁路评审候选/);
  assert.match(current,/旁路意见不会自动改题或更改原有判定/);
  assert.doesNotMatch(current,/&lt;span/);
  assert.match(current,/简单 \/ 中等/);
  try {
    document.documentElement.lang='en';
    const english=renderDifficultyAssessment(content);
    assert.match(english,/the displayed difficulty is outside the shadow review candidates/);
    assert.match(english,/Easy \/ Medium/);
  } finally { document.documentElement.lang='zh-CN'; }
  const stale=renderDifficultyAssessment({...content,version:2,difficulty_shadow:{...content.difficulty_shadow,matches_current_content:false}});
  assert.match(stale,/旁路评审对应旧版本/);
  assert.doesNotMatch(stale,/当前标注的难度未进入旁路评审候选/);
  assert.doesNotMatch(renderDifficultyAssessment({...content,difficulty_shadow:null}),/旁路评审建议人工复核/);
});

test('new reviewer shows content failure before difficulty candidates and discloses incomplete coverage', () => {
  const content={version:1,asset:{questions:[{slot_id:'q1',stem:'One'},{slot_id:'q2',stem:'Two'}]},
    difficulty_shadow:{matches_current_content:true,
      coverage:{total_questions:2,sampled_questions:1,assessed_questions:0},
      questions:[{slot_id:'q1',candidates:['easy'],decision:{status:'content_issue'},
        manual_review_recommended:true,review_flags:['published_grade_outside_candidates']}]}};
  const html=renderDifficultyAssessment(content);
  assert.match(html,/请先检查内容/);
  assert.match(html,/0 \/ 2/);
  assert.match(html,/未判级不表示通过/);
  assert.doesNotMatch(html,/当前标注的难度未进入旁路评审候选/);
  const stale=renderDifficultyAssessment({...content,difficulty_shadow:{...content.difficulty_shadow,matches_current_content:false}});
  assert.doesNotMatch(stale,/请先检查内容|0 \/ 2/);
});

test('new reviewer distinguishes unavailable evidence from a completed assessment', () => {
  const content={version:1,asset:{questions:[{slot_id:'q1',stem:'One'}]},
    difficulty_shadow:{matches_current_content:true,
      coverage:{total_questions:1,sampled_questions:1,assessed_questions:0},
      questions:[{slot_id:'q1',decision:{status:'unavailable'},manual_review_recommended:true}]}};
  assert.match(renderDifficultyAssessment(content),/评审未完整完成/);
  try {
    document.documentElement.lang='en';
    assert.match(renderDifficultyAssessment(content),/Unrated questions have not passed review/);
  } finally {document.documentElement.lang='zh-CN';}
});
