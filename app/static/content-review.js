import { a, locale, m, setText, t } from './i18n.js';
import { bindDifficultyEvaluation, readQuestionDifficulties, renderQuestionDifficultyEvaluation } from './difficulty.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const criteria = [['correctness','内容正确性'],['groundedness','资料支持度'],['difficulty_match','难度匹配度']];
const formats = new Set(['pdf','docx','md']);
const validScore = value => Number.isInteger(Number(value)) && Number(value) >= 1 && Number(value) <= 5;
const evaluationFor = content => content.evaluation?.version === content.version ? content.evaluation : null;

export function renderReviewBar(content) {
  const statuses = {approved:'已确认',draft:'待审核',failed:'处理失败',insufficient_evidence:'资料依据不足'};
  const status = Object.hasOwn(statuses,content.status) ? content.status : '';
  const id = escape(encodeURIComponent(content.id));
  return `<div class="review-bar content-review-bar">
    <div class="content-review-meta"><span class="badge ${status}">${m(statuses[status] || '未记录')}</span><small>${m('版本 {n}',{n:content.version})}</small></div>
    <div class="content-review-actions"><button type="button" class="secondary" data-action="export-content">${m('导出')}</button>${status==='approved'?`<a class="button secondary" href="/?learn=${id}" target="_blank" rel="noopener">${m('打开学习页')}</a>`:''}</div>
  </div>`;
}

function summary(evaluation) {
  if (!evaluation) return {key:'未评价'};
  return {key:'已评价 · 版本 {n}',values:{n:evaluation.version}};
}

export function renderRatingPanel(content) {
  const evaluation = evaluationFor(content), metrics = evaluation?.metrics || {};
  return `<details class="inspector-section content-rating" data-rating-panel>
    <summary class="content-rating-summary"><span class="content-rating-title">${m('内容评价')}</span><span class="content-rating-status" data-rating-summary>${m(summary(evaluation))}</span></summary>
    <div class="content-rating-body">
      <p class="content-rating-purpose">${m('可选评价，用于记录内容质量，不影响内容确认或下载。')}</p>
      <p class="content-rating-version">${m('版本 {n}',{n:content.version})} <span aria-hidden="true">·</span> ${m('1 分最低，5 分最高。')}</p>
      <form id="evaluation">
        ${criteria.map(([name,label])=>`<label>${m(label)}<select name="${name}" required><option value="" ${!validScore(metrics[name])?'selected':''} data-i18n="选择评分">${escape(t('选择评分'))}</option>${[1,2,3,4,5].map(value=>`<option value="${value}" ${Number(metrics[name])===value?'selected':''} data-i18n="{n} 分" data-i18n-values="${escape(JSON.stringify({n:value}))}">${escape(t('{n} 分',{n:value}))}</option>`).join('')}</select></label>`).join('')}
        ${renderQuestionDifficultyEvaluation(content,metrics.question_difficulties || [])}
        <label>${m('观察记录')}<textarea name="notes" rows="3" ${a('记录值得保留或需要改进的地方','placeholder')}>${escape(metrics.notes || '')}</textarea></label>
        <p class="error-message content-rating-error" data-rating-error role="alert" hidden></p>
        <button type="submit" class="secondary">${m(evaluation?'更新评价':'保存评价')}</button>
      </form>
      <details class="content-rating-data"><summary>${m('评价数据')}</summary><div><a class="link" href="/api/evaluations/export">${m('导出评价 CSV')}</a><a class="link" href="/api/difficulty/evaluations/export">${m('导出逐题难度评价 CSV')}</a></div></details>
    </div>
  </details>`;
}

// A mounted panel owns one content version. Route/session disposal makes a late reply inert.
export function bindRatingPanel(root, content, {save, onSaved = () => {}, onError = () => {}} = {}) {
  const panel = root.matches?.('[data-rating-panel]') ? root : root.querySelector('[data-rating-panel]');
  const form = panel?.querySelector('#evaluation');
  if (!form) return () => {};
  const version = content.version, submit = form.querySelector('button[type="submit"]');
  const errorNode = form.querySelector('[data-rating-error]'), status = panel.querySelector('[data-rating-summary]');
  const summaryNode = panel.querySelector('summary'), difficultyControls = [...form.querySelectorAll('[data-teacher-difficulty]')];
  let evaluation = evaluationFor(content), disposed = false, pending = false;
  const active = () => !disposed && root.isConnected !== false;
  const fill = metrics => {
    for (const [name] of criteria) form.querySelector(`[name="${name}"]`).value = validScore(metrics[name]) ? String(metrics[name]) : '';
    form.querySelector('[name="notes"]').value = metrics.notes || '';
    const ratings = new Map((metrics.question_difficulties || []).map(item=>[item.slot_id,item.assessed_difficulty]));
    for (const control of difficultyControls) control.value = ratings.get(control.dataset.teacherDifficulty) || '';
  };
  fill(evaluation?.metrics || {});
  bindDifficultyEvaluation(form);
  const difficultyHandlers = difficultyControls.map(control=>[control,control.onchange]);
  const showError = error => {
    setText(errorNode,error.uiMessage || error.message || '评价未保存，请重试。');
    errorNode.hidden = false;
  };
  const submitRating = async event => {
    event.preventDefault();
    if (pending || !active() || !form.reportValidity()) return;
    errorNode.hidden = true;
    let payload;
    try {
      const metrics = Object.fromEntries(criteria.map(([name])=>[name,Number(form.querySelector(`[name="${name}"]`).value)]));
      if (!Object.values(metrics).every(validScore)) throw new Error('选择评分');
      payload = {version,...metrics,notes:form.querySelector('[name="notes"]').value,question_difficulties:readQuestionDifficulties(form)};
    } catch (error) { showError(error); return; }
    pending = true;
    const disabled = [...form.querySelectorAll('input, select, textarea, button')].map(control=>[control,control.disabled]);
    for (const [control] of disabled) control.disabled = true;
    form.setAttribute('aria-busy','true');
    setText(submit,'正在保存评价…');
    try {
      const result = await save(payload);
      if (!active()) return;
      const snapshot = result?.evaluation;
      if (snapshot?.version !== version || !snapshot.metrics) throw new Error('评价未保存，请重试。');
      evaluation = snapshot;
      fill(snapshot.metrics);
      setText(status,summary(snapshot));
      const focusWasInside = form.contains?.(document.activeElement);
      panel.open = false;
      if (focusWasInside) summaryNode.focus({preventScroll:true});
      onSaved(snapshot);
    } catch (error) {
      if (active() && error.name !== 'SessionChangedError') { showError(error); onError(error); }
    } finally {
      pending = false;
      if (active()) {
        for (const [control,wasDisabled] of disabled) control.disabled = wasDisabled;
        form.removeAttribute('aria-busy');
        setText(submit,evaluation?'更新评价':'保存评价');
      }
    }
  };
  form.addEventListener('submit',submitRating);
  return () => {
    disposed = true;
    form.removeEventListener('submit',submitRating);
    for (const [control,handler] of difficultyHandlers) if (control.onchange === handler) control.onchange = null;
  };
}

export function renderExportDialog(content) {
  const questions = Boolean(content.asset?.questions?.length);
  return `<form id="content-export-form" class="content-export-form">
    <p class="content-export-context">${escape(content.asset?.title || '')}<span>${m('版本 {n}',{n:content.version})}</span></p>
    <fieldset><legend>${m('文件格式')}</legend><div class="content-export-formats">${[['pdf','PDF'],['docx','Word'],['md','Markdown']].map(([value,label])=>`<label class="content-export-format"><input type="radio" name="format" value="${value}" ${value==='pdf'?'checked':''} required><span>${label}</span></label>`).join('')}</div></fieldset>
    ${questions?`<label class="content-export-answers"><input type="checkbox" name="include_answers" checked>${m('包含答案与解析')}</label>`:''}
    <details class="content-export-more"><summary>${m('更多导出')}</summary><a class="content-export-evidence" href="/api/contents/${escape(encodeURIComponent(content.id))}/evidence" download>${m('导出生成依据')}</a></details>
    <p class="error-message" data-export-error role="alert" hidden></p>
    <div class="modal-actions"><button type="button" class="secondary" data-action="close-modal">${m('取消')}</button><button type="submit">${m('下载文件')}</button></div>
  </form>`;
}

export function readExportOptions(form) {
  const format = form.querySelector('[name="format"]:checked')?.value;
  if (!formats.has(format)) throw new Error('请选择文件格式。');
  return {format,include_answers:Boolean(form.querySelector('[name="include_answers"]')?.checked),language:locale()==='en-US'?'en':'zh'};
}
