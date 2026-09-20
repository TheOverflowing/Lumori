import { setLocalizedValidity } from './control-language.js';
import { m, t, setText } from './i18n.js';
import { renderSwitch } from './settings.js';

const levels = ['easy', 'medium', 'hard'];
const names = {easy:'简单', medium:'中等', hard:'困难', uncertain:'无法判断'};
const processes = {remember:'记忆',understand:'理解',apply:'应用',analyze:'分析',evaluate:'评价',create:'创造'};
const allNames = {easy:'全部 {n} 题为简单', medium:'全部 {n} 题为中等', hard:'全部 {n} 题为困难'};
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const integer = value => value !== '' && value !== null && Number.isInteger(Number(value)) && Number(value) >= 0;
const option = (value, label, selected) => `<option value="${value}" ${selected === value ? 'selected' : ''} data-i18n="${escape(label)}">${escape(t(label))}</option>`;

export function ensureDifficultyDraft(draft) {
  draft.difficulty_mode ??= 'uniform';
  draft.difficulty_counts ??= Object.fromEntries(levels.map(level => [level, level === draft.difficulty ? draft.count : 0]));
  draft.lesson_difficulty ??= draft.difficulty;
  draft.include_explanations ??= false;
  draft.learner_profile_is_default ??= draft.learner_profile == null;
  if (draft.learner_profile_is_default) draft.learner_profile = t('已学习所选课程资料的本科生');
  return draft;
}

export function renderLessonDepth(draft) {
  return `<div id="lesson-difficulty-options"><label class="select-field">${m('讲解深度')}<select id="lesson-difficulty" name="lesson_difficulty">${[['easy','简单 · 概念讲解'],['medium','中等 · 应用讲解'],['hard','困难 · 综合分析']].map(([value,label])=>option(value,label,draft.lesson_difficulty)).join('')}</select></label></div>`;
}

export function renderExplanationOption(draft) {
  const lesson = draft.material === 'lesson';
  return `<fieldset id="explanation-option" class="explanation-option" ${lesson?'hidden disabled':''}>${renderSwitch({id:'include-explanations',label:'添加题前讲解',checked:draft.include_explanations===true})}</fieldset>`;
}

export function updateExplanationOption(form, draft) {
  const group = form.querySelector('#explanation-option');
  const control = form.querySelector('#include-explanations');
  // Keep the assessment choice when switching to a lesson and back. Reading
  // the same native input also preserves it across interface language changes.
  draft.include_explanations = control.checked === true;
  group.hidden = draft.material === 'lesson';
  group.disabled = group.hidden;
  control.disabled = group.hidden;
}

export function renderDifficultyControls(draft) {
  ensureDifficultyDraft(draft);
  return `<section class="difficulty-controls">
    <fieldset id="question-difficulty-options"><legend>${m('题目难度')}</legend>
      <div class="difficulty-mode">${[['uniform','统一难度'],['distribution','按题数分配']].map(([value,label])=>`<label><input type="radio" name="difficulty_mode" value="${value}" ${draft.difficulty_mode===value?'checked':''}>${m(label)}</label>`).join('')}</div>
      <div id="uniform-difficulty-options"><label>${m('所有题目的目标难度')}<select name="difficulty" id="difficulty">${levels.map(level=>option(level,names[level],draft.difficulty)).join('')}</select></label></div>
      <div id="distribution-difficulty-options"><div class="difficulty-counts">${levels.map(level=>`<div class="number-field"><label for="difficulty-${level}">${m(names[level])}</label><input id="difficulty-${level}" type="number" name="difficulty_${level}" data-difficulty-count="${level}" min="0" max="50" step="1" required value="${escape(draft.difficulty_counts[level])}" aria-describedby="difficulty-preview difficulty-validation"></div>`).join('')}</div><div class="difficulty-presets">${levels.map(level=>`<button type="button" class="secondary" data-difficulty-preset="${level}">${m({easy:'全部简单',medium:'全部中等',hard:'全部困难'}[level])}</button>`).join('')}</div></div>
      <div id="difficulty-preview" class="difficulty-preview" role="status" aria-live="polite"></div><p id="difficulty-validation" class="difficulty-validation" role="status" aria-live="polite"></p>
    </fieldset>
    <label class="learner-profile">${m('目标学生与已学基础')}<textarea name="learner_profile" id="learner-profile" required minlength="2" rows="2">${escape(draft.learner_profile)}</textarea></label>

  </section>`;
}

export function difficultyValidation(draft) {
  if (draft.material === 'lesson') return null;
  if (!Number.isInteger(draft.count) || draft.count < 1 || draft.count > 50) return {key:'请将题目数量设为 1 至 50 的整数。'};
  if (draft.difficulty_mode === 'uniform') return null;
  if (levels.some(level => !integer(draft.difficulty_counts[level]) || Number(draft.difficulty_counts[level]) > 50)) return {key:'每档题数必须是 0 至 50 的整数。'};
  const total = levels.reduce((sum,level)=>sum+Number(draft.difficulty_counts[level]),0);
  if (total < draft.count) return {key:'还需分配 {n} 题。',values:{n:draft.count-total}};
  if (total > draft.count) return {key:'分配超出 {n} 题，请减少。',values:{n:total-draft.count}};
  return null;
}

export function difficultySummary(draft) {
  if (draft.material === 'lesson') return m(names[draft.lesson_difficulty]);
  if (draft.difficulty_mode === 'uniform') return m(allNames[draft.difficulty],{n:draft.count||'—'});
  return levels.map(level=>`${m(names[level])} ${escape(draft.difficulty_counts[level] === '' ? '—' : draft.difficulty_counts[level])}`).join(' · ');
}

export function updateDifficultyControls(form, draft) {
  const lesson = draft.material === 'lesson';
  draft.difficulty_mode = form.querySelector('[name=difficulty_mode]:checked').value;
  draft.difficulty = form.querySelector('#difficulty').value;
  draft.lesson_difficulty = form.querySelector('#lesson-difficulty').value;
  draft.learner_profile = form.querySelector('#learner-profile').value;
  for (const input of form.querySelectorAll('[data-difficulty-count]')) draft.difficulty_counts[input.dataset.difficultyCount] = input.value;
  const distribution = draft.difficulty_mode === 'distribution';
  form.querySelector('#lesson-difficulty-options').hidden = !lesson;
  form.querySelector('#lesson-difficulty').disabled = !lesson;
  const questionOptions = form.querySelector('#question-difficulty-options');
  questionOptions.hidden = lesson;
  questionOptions.disabled = lesson;
  form.querySelector('#uniform-difficulty-options').hidden = distribution;
  form.querySelector('#difficulty').disabled = lesson || distribution;
  form.querySelector('#distribution-difficulty-options').hidden = !distribution;
  for (const input of form.querySelectorAll('[data-difficulty-count]')) input.disabled = lesson || !distribution;
  form.querySelector('#difficulty-preview').innerHTML = difficultySummary(draft);
  const error = difficultyValidation(draft);
  const validation = form.querySelector('#difficulty-validation');
  validation.hidden = lesson || !distribution;
  validation.classList.toggle('invalid',Boolean(error));
  setText(validation,error || {key:'已分配 {n} / {n} 题。',values:{n:draft.count}});
  // Keep native constraint validation and the visible allocation message in sync.
  const firstCount = form.querySelector('[data-difficulty-count]');
  setLocalizedValidity(firstCount, error);
}

export function bindDifficultyPresets(form, draft, update) {
  for (const button of form.querySelectorAll('[data-difficulty-preset]')) button.onclick = () => {
    if (!Number.isInteger(draft.count) || draft.count < 1 || draft.count > 50) { form.querySelector('#count').reportValidity(); return; }
    for (const input of form.querySelectorAll('[data-difficulty-count]')) input.value = input.dataset.difficultyCount === button.dataset.difficultyPreset ? draft.count : 0;
    update();
  };
}

export function generationPayload(draft) {
  const {difficulty_mode, difficulty_counts, lesson_difficulty, learner_profile_is_default, ...payload} = draft;
  payload.difficulty = draft.material === 'lesson' ? lesson_difficulty : draft.difficulty;
  payload.difficulty_distribution = draft.material !== 'lesson' && difficulty_mode === 'distribution'
    ? Object.fromEntries(levels.map(level=>[level,Number(difficulty_counts[level])])) : null;
  payload.include_explanations = draft.material !== 'lesson' && draft.include_explanations === true;
  return payload;
}

export function renderQuestionDifficulty(question, index, assessment) {
  const design = question.difficulty_design;
  const checked = ['model_checked','model_adjusted'].includes(assessment?.status) && assessment.items?.find(item=>item.slot_id===(question.slot_id || `q${index+1}`));
  return `<div class="question-difficulty">${m('目标难度')} · <strong>${m(names[question.difficulty] || '未记录')}</strong>${assessment?.status === 'model_adjusted' && checked && checked.assessed_difficulty !== question.difficulty ? `<span class="difficulty-adjusted-label">${m('模型判断')} · ${m(names[checked.assessed_difficulty] || '未记录')}</span>` : ''}</div>${design || question.difficulty_reason || checked ? `<details class="question-difficulty-detail"><summary>${m('查看难度设计与检查')}</summary>${question.difficulty_reason?`<p>${escape(question.difficulty_reason)}</p>`:''}${design?`<dl><dt>${m('认知过程')}</dt><dd>${m(processes[design.cognitive_process] || design.cognitive_process)}</dd><dt>${m('涉及概念')}</dt><dd>${(design.concepts || []).map(escape).join(' · ')}</dd><dt>${m('预期评分要点')}</dt><dd><ol>${(design.expected_steps || []).map(step=>`<li>${escape(step)}</li>`).join('')}</ol></dd></dl>`:''}${checked?`<p>${m('模型判断')} · ${m(names[checked.assessed_difficulty] || '未记录')}${checked.confidence !== undefined ? ` · ${m('置信度')} ${m({low:'低',medium:'中',high:'高'}[checked.confidence] || checked.confidence)}` : ''}</p><p>${escape(checked.rationale)}</p>`:''}</details>`:''}`;
}

export function renderDifficultyAssessment(content) {
  if (!content.asset.questions.length) return `<section class="inspector-section difficulty-assessment"><h2>${m('讲解深度')}</h2><p>${m(names[content.config?.difficulty] || '未记录')}</p></section>`;
  const assessment = content.difficulty_assessment;
  const status = content.version > 1 && ['model_checked','model_adjusted'].includes(assessment?.status) ? 'needs_review' : assessment?.status || 'legacy_unverified';
  return `<section class="inspector-section difficulty-assessment"><h2>${m('难度控制')}</h2><p class="difficulty-assessment-status">${m({model_checked:'模型已检查，待教师判断',model_adjusted:'已保留结果，难度有偏差',needs_review:'当前版本需要重新判断难度',legacy_unverified:'旧材料尚无逐题难度检查'}[status] || '当前版本需要重新判断难度')}</p>${status === 'model_adjusted' ? `<details class="difficulty-adjustment-detail"><summary>${m('校准记录')}</summary><p>${m('已尝试按目标难度调整，保留了通过内容检查的结果；目标难度与模型判断分别显示。')}</p><p>${m('这份结果未严格匹配目标难度，仍需人工核对。')}</p></details>` : ''}${content.config?.learner_profile?`<p>${m('目标学生与已学基础')}<br>${escape(content.config.learner_profile)}</p>`:''}</section>`;
}

export function renderQuestionDifficultyEvaluation(content, saved = []) {
  const ratings = new Map(saved.map(item=>[item.slot_id,item.assessed_difficulty]));
  if (!content.asset.questions.length) return '';
  return `<fieldset class="difficulty-evaluation"><legend>${m('逐题教师判断难度（可选）')}</legend>${content.asset.questions.map((question,index)=>`<label><span>${m('第 {n} 题',{n:index+1})} · ${escape(question.stem.slice(0,90))}${question.stem.length>90?'…':''}</span><select data-teacher-difficulty="${escape(question.slot_id||`q${index+1}`)}">${option('','暂不评价',ratings.get(question.slot_id||`q${index+1}`)||'')}${[...levels,'uncertain'].map(level=>option(level,names[level],ratings.get(question.slot_id||`q${index+1}`))).join('')}</select></label>`).join('')}<p class="difficulty-evaluation-error invalid" role="status" hidden></p></fieldset>`;
}

export function readQuestionDifficulties(form) {
  const controls = [...form.querySelectorAll('[data-teacher-difficulty]')];
  const selected = controls.filter(control=>control.value);
  if (selected.length && selected.length !== controls.length) throw new Error('逐题难度评价请全部填写，或全部留空。');
  return selected.map(control=>({slot_id:control.dataset.teacherDifficulty,assessed_difficulty:control.value}));
}

export function bindDifficultyEvaluation(form) {
  const controls = [...form.querySelectorAll('[data-teacher-difficulty]')];
  const update = () => {
    const partial = controls.some(control=>control.value) && controls.some(control=>!control.value);
    for (const control of controls) setLocalizedValidity(control, partial && !control.value ? '逐题难度评价请全部填写，或全部留空。' : '');
    const error = form.querySelector('.difficulty-evaluation-error');
    if (error) { error.hidden = !partial; setText(error,'逐题难度评价请全部填写，或全部留空。'); }
  };
  for (const control of controls) control.onchange = update;
}
