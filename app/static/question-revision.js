import { a, m, setText, t } from './i18n.js';
import { renderReadableText } from './readable-text.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const levels = {easy:'简单',medium:'中等',hard:'困难'};
const fields = [['stem','题干'],['options','选项'],['answer','参考答案'],['explanation','答案解析'],['difficulty','目标难度']];
export const questionSlot = (question,index) => question.slot_id || `q${index+1}`;

export function renderQuestionRevisionDialog(content, slotId) {
  const index = content.asset.questions.findIndex((question,index)=>questionSlot(question,index) === slotId);
  if (index < 0) throw new Error('找不到这道题目，请刷新后重试。');
  return `<form id="question-revision-form" class="question-revision-form">
    <p class="question-revision-context">${m('第 {n} 题',{n:index+1})}<span aria-hidden="true"> · </span>${m('版本 {n}',{n:content.version})}</p>
    <fieldset><legend class="sr-only">${m('修改范围')}</legend><div class="question-revision-modes">${[['rewrite','重写本题'],['explanation','仅修改解析']].map(([value,label])=>`<label><input type="radio" name="mode" value="${value}" ${value==='rewrite'?'checked':''}><span>${m(label)}</span></label>`).join('')}</div></fieldset>
    <label>${m('修改要求')}<textarea name="instruction" rows="4" minlength="2" maxlength="2000" required ${a('例如：保留知识点，换一个更贴近生活的情境','placeholder')}></textarea></label>
    <label data-revision-difficulty>${m('目标难度')}<select name="difficulty"><option value="" data-i18n="保持当前难度">${escape(t('保持当前难度'))}</option>${Object.entries(levels).map(([value,label])=>`<option value="${value}" data-i18n="${label}">${escape(t(label))}</option>`).join('')}</select></label>
    <p class="error-message" data-revision-error role="alert" hidden></p>
    <div class="modal-actions"><button type="button" class="secondary" data-action="close-modal">${m('取消')}</button><button type="submit">${m('开始修改')}</button></div>
  </form>`;
}

export function revisionPayload(form, version) {
  const mode = form.querySelector('[name="mode"]:checked')?.value;
  const instruction = form.querySelector('[name="instruction"]').value.trim();
  const difficulty = form.querySelector('[name="difficulty"]').value;
  if (!['rewrite','explanation'].includes(mode) || [...instruction].length < 2 || [...instruction].length > 2000) throw new Error('修改要求需为 2–2000 个字符。');
  if (mode === 'rewrite' && difficulty && !Object.hasOwn(levels,difficulty)) throw new Error('请选择目标难度。');
  return {version,instruction,mode,...(mode === 'rewrite' && difficulty ? {difficulty} : {})};
}

// Keep one idempotency key per form payload, including after uncertain replies.
export function bindQuestionRevision(form, {version, submit, onCreated, isActive=()=>true, pendingChanged=()=>{}, makeKey=()=>crypto.randomUUID()}) {
  const button = form.querySelector('button[type="submit"]'), error = form.querySelector('[data-revision-error]');
  const difficulty = form.querySelector('[data-revision-difficulty]');
  let pending = false, disposed = false, fingerprint = '', requestKey;
  const active = () => !disposed && form.isConnected !== false && isActive();
  const changed = () => {
    const explanation = form.querySelector('[name="mode"]:checked')?.value === 'explanation';
    difficulty.hidden = explanation;
    form.querySelector('[name="difficulty"]').disabled = explanation;
  };
  const send = async event => {
    event.preventDefault();
    if (pending || !active() || !form.reportValidity()) return;
    error.hidden = true;
    let payload;
    try { payload = revisionPayload(form,version); } catch (cause) { setText(error,cause.message); error.hidden=false; return; }
    const nextFingerprint = JSON.stringify(payload);
    if (fingerprint !== nextFingerprint) { fingerprint=nextFingerprint; requestKey=makeKey(); }
    pending = true;
    const controls = [...form.querySelectorAll('input, textarea, select, button')].map(control=>[control,control.disabled]);
    for (const [control] of controls) control.disabled=true;
    form.setAttribute('aria-busy','true'); setText(button,'正在提交…'); pendingChanged(true);
    try {
      const result = await submit({...payload,request_key:requestKey});
      if (active()) {
        if (!result?.job_id) throw new Error('修改任务未能创建，请重试。');
        onCreated(result);
      }
    } catch (cause) {
      if (active() && cause.name !== 'SessionChangedError') { setText(error,cause.uiMessage || cause.message); error.hidden=false; }
    } finally {
      pending=false; pendingChanged(false);
      if (active()) { for (const [control,disabled] of controls) control.disabled=disabled; form.removeAttribute('aria-busy'); setText(button,'开始修改'); changed(); }
    }
  };
  form.addEventListener('change',changed);
  form.addEventListener('submit',send); changed();
  return () => { disposed=true; form.removeEventListener('change',changed); form.removeEventListener('submit',send); };
}

export function questionChanges(previous, current) {
  if (!previous || !current) return [];
  return fields.filter(([name])=>JSON.stringify(previous[name] ?? null) !== JSON.stringify(current[name] ?? null))
    .map(([field,label])=>({field,label,before:previous[field],after:current[field]}));
}

export function renderQuestionComparison(content) {
  const revision = content.config?.question_revision;
  if (!revision?.previous_question || revision.version !== content.version) return '';
  const index = (content.asset.questions || []).findIndex((question,index)=>questionSlot(question,index) === revision.slot_id);
  const changes = questionChanges(revision.previous_question,content.asset.questions[index]);
  if (!changes.length) return '';
  const value = (field,item) => field === 'difficulty' ? m(levels[item] || '未记录') : field === 'options'
    ? `<ol type="A">${(item || []).map(option=>`<li>${renderReadableText(option,escape)}</li>`).join('')}</ol>`
    : renderReadableText(item || '',escape);
  const row = change => `<section class="question-change"><h4>${m(change.label)}</h4><div class="question-change-columns"><div><span class="question-change-label">${m('修改前')}</span><div class="readable-text">${value(change.field,change.before)}</div></div><div><span class="question-change-label">${m('修改后')}</span><div class="readable-text">${value(change.field,change.after)}</div></div></div></section>`;
  const answers = changes.filter(change=>['answer','explanation'].includes(change.field));
  return `<details class="question-comparison"><summary>${m('查看本次修改')}<span>${m('第 {n} 题',{n:index+1})}</span></summary><div class="question-comparison-body"><p class="question-revision-context">${m('版本 {before} → {after}',{before:revision.base_version,after:content.version})}</p>${revision.instruction?`<p class="question-change-request">${escape(revision.instruction)}</p>`:''}${changes.filter(change=>!answers.includes(change)).map(row).join('')}${answers.length?`<details class="question-change-answers"><summary>${m('答案与解析的变化')}</summary>${answers.map(row).join('')}</details>`:''}</div></details>`;
}
