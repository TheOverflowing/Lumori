import { m, a, setText } from './i18n.js';
import { watchResource } from './live-updates.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const validDuration = value => Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;
const terminal = new Set(['succeeded', 'failed', 'insufficient_evidence', 'cancelled']);
const stageLabels = {
  exploration:'探索资料',
  retrieval:'检索资料', planning:'规划内容', writing:'生成与检查', saving:'保存结果',
  audio:'生成语音', speech:'生成语音', image:'生成图片', video:'生成视频',
};
const activityLabels = {
  checking_evidence:'正在检查资料覆盖', searching_sources:'正在查找参考资料', reading_sources:'正在读取候选资料',
  screening_sources:'正在筛选参考资料', indexing_sources:'正在整理参考资料',
  retrieving:'正在检索资料', embedding:'正在检索资料', rewriting:'正在理解检索需求',
  reranking:'正在筛选资料', planning:'正在规划内容', writing:'正在生成内容',
  solving:'正在核对答案', reviewing:'正在检查内容', repairing:'正在修正内容',
  validating:'正在检查内容', saving:'正在保存结果',
  generating_audio:'正在生成语音', generating_image:'正在生成图片', generating_video:'正在生成视频',
};
const eventActivities = {...activityLabels, retrieving:'检查可用课程资料', embedding:'准备语义检索', rewriting:'生成检索改写', reranking:'重新筛选候选资料', validating:'校验格式与引用'};
const stageStatuses = {pending:'待处理', active:'进行中', completed:'已完成', failed:'未完成', blocked:'需要补充资料', cancelled:'已停止'};
const icons = {
  exploration:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 6h14M5 18h14"/>',
  retrieval:'<circle cx="10" cy="10" r="5.5"/><path d="m14 14 5 5"/>',
  planning:'<rect x="4" y="4" width="6" height="6" rx="1.5"/><path d="M14 5h6M14 9h4M5 15h15M5 19h11"/>',
  writing:'<path d="m5 15 10-10a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4ZM13 7l4 4"/>',
  saving:'<path d="M5 4h13l2 2v14H4V4h1ZM8 4v6h8V4M8 20v-6h8v6"/>',
  audio:'<path d="M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4"/>',
  image:'<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8" cy="9" r="1.2"/><path d="m4 17 5-5 4 4 3-3 4 4"/>',
  video:'<rect x="3" y="5" width="12" height="14" rx="3"/><path d="m15 9 6-3v12l-6-3"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  paused:'<path d="M8 5v14M16 5v14"/>',
  generic:'<path d="M8 5h8M5 9h14M5 13h14M8 17h8"/>',
};
const glyph = kind => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[kind === 'speech' ? 'audio' : kind] || icons.generic}</svg>`;

// Decorative layers stay mounted, so a new stage can retarget the current
// opacity instead of flashing a freshly mounted illustration into place.
function layerMarkup(kind, markup, active, running = false) {
  return `<span class="generation-motion-layer" data-motion-layer="${escape(kind)}" data-visible="${kind === active}" data-running="${kind === active && running}">${markup}</span>`;
}
function iconLayers(view) {
  return [...new Set([...view.stages.map(stage => stage.kind),view.icon,'check','paused'])]
    .map(kind => layerMarkup(kind,glyph(kind),view.icon)).join('');
}
function stageIcon(kind) {
  return `<span class="generation-stage-symbol">${glyph(kind)}</span><span class="generation-stage-check">${glyph('check')}</span>`;
}
function selectLayer(container, kind, markup, {running = false, instant = false} = {}) {
  if (!container?.querySelectorAll) return;
  container.dataset.transition = instant ? 'instant' : 'soft';
  let layers = [...container.querySelectorAll('[data-motion-layer]')];
  if (!layers.some(layer => layer.dataset.motionLayer === kind)) {
    container.insertAdjacentHTML('beforeend',layerMarkup(kind,markup,false));
    layers = [...container.querySelectorAll('[data-motion-layer]')];
    // A newly introduced modality needs an initial opacity before it is shown.
    if (!instant) container.getBoundingClientRect?.();
  }
  for (const layer of layers) {
    const visible = layer.dataset.motionLayer === kind;
    layer.dataset.visible = String(visible);
    layer.dataset.running = String(visible && running);
  }
}
function progressMotion(root) {
  const animations = new Map(), browser = root.ownerDocument?.defaultView || globalThis;
  const preference = browser.matchMedia?.('(prefers-reduced-motion: reduce)');
  const stop = () => { for (const animation of animations.values()) animation.cancel(); animations.clear(); };
  preference?.addEventListener?.('change',stop);
  return {
    soften(node, {instant = false} = {}) {
      if (!node) return;
      const previous = animations.get(node);
      // Preserve the presented opacity when a faster update interrupts this one.
      const opacity = previous ? Number.parseFloat(browser.getComputedStyle?.(node)?.opacity) : .72;
      previous?.cancel(); animations.delete(node);
      if (instant || preference?.matches || !node.animate) return;
      const animation = node.animate([{opacity:Number.isFinite(opacity) ? opacity : .72},{opacity:1}],
        {duration:180,easing:'cubic-bezier(.22, 1, .36, 1)'});
      animations.set(node,animation);
      animation.onfinish = () => { if (animations.get(node) === animation) animations.delete(node); };
    },
    text(node, key, options) { setText(node,key); this.soften(node,options); },
    destroy() { stop(); preference?.removeEventListener?.('change',stop); },
  };
}

// Only exact application-owned errors are classified. Historic jobs have no
// typed provider metadata; arbitrary authored/provider text must stay untouched.
const transientTextErrors = new Map([
  ...[[500,'模型服务暂时异常，请稍后重试'],[502,'模型服务网关暂时异常，请稍后重试'],
      [503,'模型服务暂不可用或繁忙，请稍后重试'],[504,'模型服务响应超时，请稍后重试']]
    .flatMap(([status,hint]) => [hint,'请核对平台、模型及参数'].map(text =>
      [`text API 返回 HTTP ${status}；${text}。本次未自动重试。`,'模型服务暂不可用'])),
  ['text API 响应超时，请稍后重试。本次未自动重试。','模型服务响应超时'],
  ['text API 网络连接异常，请稍后重试。本次未自动重试。','暂时无法连接模型服务'],
]);

// No elapsed-time estimates or fabricated percentages: only server-confirmed work.
export function generationProgressView(job) {
  const timeline = job.timeline || {};
  const stages = timeline.recorded && Array.isArray(timeline.stages) ? timeline.stages.map(stage => ({
    id:String(stage.id), kind:String(stage.kind || stage.id),
    status:Object.hasOwn(stageStatuses, stage.status) ? stage.status : 'pending',
    label:Object.hasOwn(stageLabels,stage.kind || stage.id) ? stageLabels[stage.kind || stage.id] : '处理内容',
    elapsed:validDuration(stage.elapsed_ms), timingComplete:stage.timing_complete === true,
  })) : [];
  const active = stages.find(stage => stage.id === timeline.active_stage);
  const providerFailure = job.status === 'failed' && ['generate','revise_question'].includes(job.kind) ? transientTextErrors.get(job.error) : null;
  const resumable = ['failed','cancelled'].includes(job.status) && job.progress?.resumable === true;
  const resumeKind = resumable && ['generate','revise_question'].includes(job.kind) && job.progress?.resume_kind === 'repair' ? 'repair' : 'continue';
  const cancelRequested = !terminal.has(job.status) && job.cancel_requested === true;
  const title = cancelRequested ? '正在停止任务' : job.status === 'queued' ? '等待开始'
    : job.status === 'succeeded' ? (job.kind === 'revise_question' ? '修改完成' : '生成完成')
    : job.status === 'insufficient_evidence' ? '需要补充资料'
    : job.status === 'failed' ? providerFailure || (resumeKind === 'repair' ? '题目还需完善' : '生成暂时中断')
    : job.status === 'cancelled' ? '任务已停止'
    : Object.hasOwn(activityLabels,timeline.activity) ? activityLabels[timeline.activity] : '正在生成';
  const hasCount = timeline.unit === 'questions' && Number.isInteger(timeline.completed) &&
    Number.isInteger(timeline.total) && timeline.total > 0 && timeline.completed >= 0 && timeline.completed <= timeline.total;
  return {
    id:String(job.id), status:job.status, title, providerFailure, stages, terminal:terminal.has(job.status),
    activeStage:active?.id || null, activity:timeline.activity,
    elapsed:validDuration(timeline.elapsed_ms), timingComplete:timeline.timing_complete === true,
    events:Array.isArray(timeline.events) ? timeline.events.filter(event => event && eventMessage(event)).map(event => ({id:event.id,stage:String(event.stage),code:event.code,at:event.at,message:eventMessage(event)})) : [],
    truncated:timeline.events_truncated === true,
    icon:job.status === 'succeeded' ? 'check' : terminal.has(job.status) ? 'paused' : active?.kind || 'writing',
    counter:hasCount ? {done:timeline.completed, total:timeline.total} : null,
    contentId:job.status === 'succeeded' ? job.content_id || job.result?.content_id || null : null,
    resumable, resumeKind,
    cancellable:!terminal.has(job.status) && job.cancellable === true, cancelRequested,
    partialAvailable:job.kind === 'generate' && job.status !== 'succeeded' && job.partial_available === true,
    canRestore:job.kind === 'generate' && ['failed','insufficient_evidence','cancelled'].includes(job.status),
    reason:['failed','insufficient_evidence'].includes(job.status) ? String(job.error || job.result?.message || '') : '',
    message:cancelRequested ? '正在等待当前步骤安全停止，已完成的题目会保留。'
      : job.status === 'cancelled' ? '任务已停止，已完成的题目已保留。'
      : providerFailure ? (job.progress?.resumable ? '任务进度已保留，服务恢复后可继续生成。' : '生成条件已保留，可稍后返回修改并重试。')
      : job.status === 'insufficient_evidence' ? '补充或启用相关资料后，可重新生成。'
      : resumeKind === 'repair' ? '已完成的题目已保留，仅继续修正未通过的题目。'
      : job.status === 'failed' ? (job.progress?.resumable ? '已完成的题目已保留。' : job.kind === 'generate' ? '生成条件已保留，可返回修改后重试。' : '请检查资料与模型连接后重新生成。') : '',
  };
}

// This measures completed workflow steps, not elapsed or remaining time. Each
// declared stage has equal weight; accepted questions subdivide the writing
// stage, including when parallel workers finish out of order. Only a persisted
// successful job fills the bar completely. Missing telemetry stays indeterminate.
export function generationProgressMeter(view) {
  const total = view.stages.length;
  const done = view.stages.filter(stage => stage.status === 'completed').length;
  let units = done;
  if (view.counter && view.stages.some(stage => stage.kind === 'writing' && stage.status !== 'completed')) {
    units += view.counter.done / view.counter.total;
  }
  return {
    value:view.status === 'succeeded' ? 100 : total ? Math.min(99, Math.floor(units / total * 100)) : null,
    summary:view.status === 'succeeded' ? {key:'任务已完成'}
      : total ? {key:'已完成 {done} / {total} 个步骤', values:{done,total}}
      : {key:view.title},
  };
}
function meterMarkup(view) {
  const meter = generationProgressMeter(view);
  return `<div class="generation-meter" data-progress-meter data-determinate="${meter.value !== null}"><div class="generation-meter-caption"><span>${m('生成进度')}</span><span id="generation-meter-summary" data-progress-meter-summary>${m(meter.summary)}</span></div><div class="generation-meter-track" data-progress-meter-track role="progressbar" ${a('生成进度','aria-label')} aria-describedby="generation-meter-summary" aria-valuemin="0" aria-valuemax="100" ${meter.value === null ? '' : `aria-valuenow="${meter.value}"`}><span class="generation-meter-fill" data-progress-meter-fill style="transform:scaleX(${(meter.value || 0) / 100})"></span><span class="generation-meter-pending" aria-hidden="true"></span></div></div>`;
}
function updateMeter(root, previous, view) {
  const before = generationProgressMeter(previous), meter = generationProgressMeter(view);
  if (JSON.stringify(before) === JSON.stringify(meter)) return;
  const container = root.querySelector('[data-progress-meter]');
  container.dataset.determinate = String(meter.value !== null);
  const track = root.querySelector('[data-progress-meter-track]');
  if (meter.value === null) track.removeAttribute('aria-valuenow');
  else track.setAttribute('aria-valuenow', String(meter.value));
  root.querySelector('[data-progress-meter-fill]').style.transform = `scaleX(${(meter.value || 0) / 100})`;
  if (JSON.stringify(before.summary) !== JSON.stringify(meter.summary)) {
    setText(root.querySelector('[data-progress-meter-summary]'), meter.summary);
  }
}

function stageMarkup(stages) {
  return stages.map(stage => `<li class="generation-stage" data-stage="${escape(stage.id)}" data-state="${stage.status}" ${stage.status === 'active' ? 'aria-current="step"' : ''}><span class="generation-stage-icon">${stageIcon(stage.kind)}</span><span class="generation-stage-label">${m(stage.label)}</span><span class="generation-stage-meta"><span class="generation-stage-state">${m(stageStatuses[stage.status])}</span><span class="generation-stage-time">${durationMarkup(stage.elapsed, stage.timingComplete)}</span></span></li>`).join('');
}
function reasonMarkup(view) {
  return view.reason ? `<summary>${m('查看原因')}</summary><p>${m(view.reason)}</p>` : '';
}
function actionMarkup(view) {
  const primary = view.contentId ? `<button data-action="open-content" data-id="${escape(view.contentId)}">${m('查看结果')}</button>`
    : view.resumable ? `<button data-action="resume-generation" data-id="${escape(view.id)}">${m(view.resumeKind === 'repair' ? '继续修正' : '继续生成')}</button>`
    : view.status === 'insufficient_evidence' ? `<button data-action="documents">${m('前往课程资料')}</button>`
    : view.canRestore ? `<button data-action="restore-generation" data-id="${escape(view.id)}">${m('返回修改')}</button>`
    : view.terminal ? `<button data-action="generate">${m('返回创作')}</button>` : '';
  const restore = view.canRestore && (view.resumable || view.status === 'insufficient_evidence') ? `<button class="secondary" data-action="restore-generation" data-id="${escape(view.id)}">${m('返回修改')}</button>` : '';
  const partial = view.partialAvailable ? `<button class="secondary" data-action="open-partial" data-id="${escape(view.id)}">${m('查看已完成题目')}</button>` : '';
  const cancel = view.cancellable || view.cancelRequested ? `<button class="secondary" data-action="cancel-generation" data-id="${escape(view.id)}" ${view.cancelRequested ? 'disabled aria-busy="true"' : ''}>${m(view.cancelRequested ? '正在停止' : '停止任务')}</button>` : '';
  return `${primary}${partial}${restore}${cancel}<a class="button secondary" href="#${view.terminal ? 'jobs' : 'generate'}">${m(view.terminal ? '任务记录' : '返回创作')}</a>`;
}

function renderSimpleProgress(job) {
  const view = generationProgressView(job);
  return `<section class="generation-progress" data-generation-progress data-state="${escape(view.status)}" aria-labelledby="generation-progress-title"><a class="generation-back" href="#jobs"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m12 5-5 5 5 5"/></svg>${m('任务记录')}</a><div class="generation-progress-body"><div class="generation-orbit" aria-hidden="true"><span class="generation-orbit-track"></span><span class="generation-orbit-arc"></span><span class="generation-orbit-glyph" data-progress-icon>${iconLayers(view)}</span></div><div class="generation-progress-status" role="status" aria-live="polite" aria-atomic="true"><h1 id="generation-progress-title">${m(view.title)}</h1><p class="generation-counter" data-progress-count ${view.counter ? '' : 'hidden'}>${view.counter ? m('已检查 {done} / {total} 题', view.counter) : ''}</p></div>${meterMarkup(view)}<p class="generation-progress-message" data-progress-message ${view.message ? '' : 'hidden'}>${view.message ? m(view.message) : ''}</p><ol class="generation-stages" data-progress-stages ${a('生成步骤', 'aria-label')} ${view.stages.length ? '' : 'hidden'}>${stageMarkup(view.stages)}</ol><details class="generation-failure-detail" data-progress-reason ${view.reason ? '' : 'hidden'}>${reasonMarkup(view)}</details><div class="generation-progress-actions" data-progress-actions>${actionMarkup(view)}</div><div class="generation-connection" data-progress-connection hidden><p role="status">${m('连接暂时中断，正在重试')}</p><button class="link" data-action="retry">${m('刷新状态')}</button></div></div></section>`;
}

// Keep the orbit and interactive controls mounted between polls. A new status
// patches only the fields that changed, without resetting motion or focus.
function mountSimpleProgress(root, initial) {
  let previous = generationProgressView(initial);
  const find = selector => root.querySelector(selector);
  const motion = progressMotion(root);
  return {
    update(job) {
      const view = generationProgressView(job);
      root.dataset.state = view.status;
      updateMeter(root, previous, view);
      if (previous.title !== view.title) motion.text(find('#generation-progress-title'), view.title);
      if (previous.icon !== view.icon) selectLayer(find('[data-progress-icon]'),view.icon,glyph(view.icon));
      if (JSON.stringify(previous.counter) !== JSON.stringify(view.counter)) {
        const count = find('[data-progress-count]');
        count.hidden = !view.counter;
        if (view.counter) setText(count, '已检查 {done} / {total} 题', view.counter);
      }
      if (previous.message !== view.message) {
        const message = find('[data-progress-message]');
        message.hidden = !view.message;
        if (view.message) setText(message, view.message);
      }
      if (previous.reason !== view.reason) {
        const reason = find('[data-progress-reason]');
        reason.hidden = !view.reason;
        reason.innerHTML = reasonMarkup(view);
      }
      if (JSON.stringify(previous.stages) !== JSON.stringify(view.stages)) {
        const stages = find('[data-progress-stages]');
        stages.hidden = !view.stages.length;
        if (JSON.stringify(previous.stages.map(stage=>[stage.id,stage.kind])) !== JSON.stringify(view.stages.map(stage=>[stage.id,stage.kind]))) {
          stages.innerHTML = stageMarkup(view.stages);
        } else {
          const rows = [...stages.querySelectorAll('[data-stage]')];
          for (const stage of view.stages) {
            const row = rows.find(row => row.dataset.stage === stage.id), old = previous.stages.find(item => item.id === stage.id);
            if (!row) continue;
            if (old.status !== stage.status) {
              row.dataset.state = stage.status;
              if (stage.status === 'active') row.setAttribute('aria-current','step'); else row.removeAttribute('aria-current');
              motion.text(row.querySelector('.generation-stage-state'),stageStatuses[stage.status]);
            }
            if (old.elapsed !== stage.elapsed || old.timingComplete !== stage.timingComplete) row.querySelector('.generation-stage-time').innerHTML = durationMarkup(stage.elapsed,stage.timingComplete);
          }
        }
      }
      if (actionMarkup(previous) !== actionMarkup(view)) {
        find('[data-progress-actions]').innerHTML = actionMarkup(view);
        motion.soften(find('[data-progress-actions]'));
      }
      previous = view;
    },
    connection(error) {
      find('[data-progress-connection]').hidden = !error;
      root.dataset.connection = error ? 'interrupted' : 'connected';
    },
    destroy() { motion.destroy(); },
  };
}

export function watchGenerationProgress({load, initial, update, error, interval = 2000, visibility}) {
  if (terminal.has(initial.status)) return () => {};
  let stop = () => {};
  stop = watchResource({load, initial, interval, visibility, error, update(job) {
    update(job);
    if (terminal.has(job.status)) { error?.(null); stop(); }
  }});
  return stop;
}

export function durationText(milliseconds) {
  const value = validDuration(milliseconds);
  if (value === null) return '—';
  const seconds = Math.floor(value / 1000);
  return seconds < 3600 ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
    : `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
function durationMarkup(milliseconds, complete) {
  const time = durationText(milliseconds);
  const key = time === '—' ? '耗时未记录' : complete ? '耗时 {time}' : '已记录至少 {time}';
  return `<span class="generation-duration"><span aria-hidden="true">${time === '—' || complete ? '' : '≥ '}${time}</span><span class="sr-only">${m(key, {time})}</span></span>`;
}

// This is an explicit projection of structured events, never provider text,
// model reasoning, prompts, or arbitrary event.data fields.
export function eventMessage(event) {
  const data = event?.data || {};
  const number = name => Number.isInteger(data[name]) && data[name] >= 0;
  const message = (key, values = {}) => ({key, values});
  switch (event?.code) {
    case 'exploration_started': return message('开始检查资料缺口');
    case 'exploration_search': return number('round') && number('queries') ? message('第 {round} 轮：检索 {n} 组资料线索', {round:data.round,n:data.queries}) : null;
    case 'exploration_source_accepted': return number('accepted') ? message('已接纳 {n} 份参考资料', {n:data.accepted}) : null;
    case 'exploration_complete': return number('accepted') ? message('探索完成，新增 {n} 份参考资料', {n:data.accepted}) : null;
    case 'exploration_stopped': return number('accepted') ? message('本次探索已结束，新增 {n} 份参考资料', {n:data.accepted}) : null;
    case 'stage_started': return message('步骤开始');
    case 'stage_completed': return message('步骤完成');
    case 'activity_changed': return Object.hasOwn(eventActivities,data.activity) ? message(eventActivities[data.activity]) : null;
    case 'retrieval_selected': return number('selected') ? message('已选取 {n} 个资料片段', {n:data.selected}) : null;
    case 'fusion_applied': return number('routes') ? message('已融合 {n} 路检索结果', {n:data.routes}) : null;
    case 'fusion_fallback': {
      const reasons = {
        ambiguous_input:'表达仍有歧义，保留原始检索', clarification_unresolved:'补充条件尚未确认，保留原始检索',
        unchanged_query:'改写与原输入一致，保留原始检索',
        language_changed:'改写改变了关键条件，保留原始检索', literal_changed:'改写改变了关键条件，保留原始检索', number_added:'改写改变了关键条件，保留原始检索',
        input_limit:'输入超过改写限制，保留原始检索', interrupted_rewrite:'改写过程已中断，保留原始检索',
        rewrite_timeout:'改写响应超时，保留原始检索', rewrite_failed:'改写暂不可用，保留原始检索', invalid_output:'改写未通过检查，保留原始检索',
        rewrite_retrieval_failed:'改写检索暂不可用，保留原始检索', unavailable:'融合暂不可用，保留原始检索',
      };
      return message(Object.hasOwn(reasons,data.reason) ? reasons[data.reason] : '已使用原始检索结果');
    }
    case 'plan_ready': return number('total') ? message('已规划 {n} 道题', {n:data.total}) : null;
    case 'question_started': return number('number') && number('total') ? message('开始处理第 {n} / {total} 题', {n:data.number,total:data.total}) : null;
    case 'question_passed': return number('number') ? message('第 {n} 题已通过检查', {n:data.number}) : null;
    case 'question_rejected': return number('number') && number('attempt') ? message('第 {n} 题未通过第 {attempt} 次检查', {n:data.number,attempt:data.attempt}) : null;
    case 'question_format_adapting': return number('number') ? message('正在调整第 {n} 题的格式要求',{n:data.number}) : null;
    case 'question_format_adapted': return number('number') && data.level === 'compatible' ? message('第 {n} 题已兼容格式差异',{n:data.number}) : null;
    case 'question_check_failed': {
      const reasons = {
        format:'第 {n} 题需调整格式 · 第 {attempt} 次检查',
        difficulty:'第 {n} 题需调整难度 · 第 {attempt} 次检查',
        quality:'第 {n} 题需修正内容 · 第 {attempt} 次检查',
        evidence:'第 {n} 题需补充依据 · 第 {attempt} 次检查',
        duplicate:'第 {n} 题与已有题目重复 · 第 {attempt} 次检查',
      };
      return number('number') && number('attempt') && typeof data.reason === 'string' && Object.hasOwn(reasons,data.reason)
        ? message(reasons[data.reason],{n:data.number,attempt:data.attempt}) : null;
    }
    case 'question_repair': return number('number') && number('attempt') ? message('正在修正第 {n} 题 · 第 {attempt} 轮', {n:data.number,attempt:data.attempt}) : null;
    case 'task_succeeded': return message('任务已完成');
    case 'task_failed': return message('任务未完成');
    case 'task_blocked': return message('资料依据不足，已停止');
    case 'task_interrupted': return data.cause === 'service_stop' ? message('服务已停止，任务中断') : data.cause === 'restart' ? message('服务重启后检测到中断') : null;
    case 'task_cancel_requested': return message('已申请停止任务');
    case 'task_cancelled': return message('任务已停止');
    case 'task_resumed': return message('任务已恢复');
    default: return null;
  }
}
function eventClock(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.valueOf()) ? '—' : [date.getHours(),date.getMinutes(),date.getSeconds()].map(part => String(part).padStart(2,'0')).join(':');
}
const eventsFor = (view, selected) => selected ? view.events.filter(event => event.stage === selected.id) : view.events;
function eventsMarkup(view, selected) {
  const events = eventsFor(view, selected);
  return events.length ? events.map(event => `<li class="generation-event" data-event-id="${escape(event.id)}"><time datetime="${escape(event.at || '')}">${eventClock(event.at)}</time><span>${m(event.message)}</span></li>`).join('')
    : `<li class="generation-events-empty">${m(view.stages.length ? '暂无处理记录' : '此任务未记录详细过程')}</li>`;
}
function currentStage(view) {
  return view.stages.find(stage => stage.id === view.activeStage)
    || view.stages.find(stage => ['failed','blocked','cancelled'].includes(stage.status))
    || [...view.stages].reverse().find(stage => stage.status === 'completed')
    || view.stages[0] || null;
}
export function progressSelection(view, selectedId = null, following = true) {
  return following ? currentStage(view) : view.stages.find(stage => stage.id === selectedId) || currentStage(view);
}
function selectedHeadline(view, selected) {
  if (!selected) return '任务概况';
  if (selected.status === 'failed' && view.providerFailure) return view.providerFailure;
  if (selected.status === 'failed' && view.resumeKind === 'repair') return '可继续修正此步骤';
  if (selected.status === 'active' && view.cancelRequested) return '正在停止任务';
  if (selected.status === 'active' && !view.terminal && view.status !== 'queued') return Object.hasOwn(activityLabels,view.activity) ? activityLabels[view.activity] : selected.label;
  return selected.status === 'completed' ? '此步骤已完成' : ['failed','blocked','cancelled'].includes(selected.status) ? '此步骤已停止' : '等待此步骤开始';
}

function visualMarkup(kind) {
  const lines = '<path d="M16 23h29M16 32h37M16 41h24"/>';
  let artwork;
  switch (kind) {
    case 'exploration': artwork = `<g class="scene-exploration-globe"><circle cx="104" cy="76" r="44" class="scene-glass"/><ellipse cx="104" cy="76" rx="21" ry="44"/><path d="M60 76h88M69 50h70M69 102h70"/></g><path class="scene-link" d="M152 76h77"/><g class="scene-particle"><circle cx="152" cy="76" r="3" class="scene-dot"/></g><g transform="translate(239 21) rotate(5 33 43)"><rect width="66" height="84" rx="9" class="scene-muted"/></g><g class="scene-exploration-source" transform="translate(218 42)"><rect width="72" height="91" rx="10" class="scene-paper"/>${lines}<path d="M16 53h38M16 64h29"/><circle cx="64" cy="76" r="14" class="scene-glass"/><path d="m58 76 4 4 8-9"/></g>`; break;
    case 'retrieval': artwork = `<g class="progress-scene-sources"><g transform="translate(40 26) rotate(-8 35 45)"><rect width="66" height="84" rx="9" class="scene-paper"/>${lines}<path d="M16 54h34M16 63h26"/></g><g transform="translate(64 33) rotate(5 35 45)"><rect width="66" height="84" rx="9" class="scene-paper"/>${lines}<path d="M16 54h34M16 63h26"/></g></g><path class="scene-link" d="M142 76h78"/><g class="scene-particle"><circle cx="152" cy="76" r="3" class="scene-dot"/></g><g class="scene-lens" transform="translate(157 42)"><circle cx="28" cy="28" r="26" class="scene-glass"/><circle cx="28" cy="28" r="15"/><path d="m39 40 15 15"/></g><g transform="translate(246 33)"><rect width="70" height="83" rx="10" class="scene-paper"/><path d="M15 20h38M15 31h27M15 51h37M15 62h31"/><rect x="10" y="42" width="50" height="29" rx="4" class="scene-highlight"/></g>`; break;
    case 'planning': artwork = `<path class="scene-link" d="M180 46v24M83 83V69h194v14M180 69v14"/><g transform="translate(144 15)"><rect width="72" height="32" rx="8" class="scene-paper"/><path d="M160 31h39" transform="translate(-144 -15)"/></g>${[48,145,242].map((x,i)=>`<g class="scene-plan-node scene-delay-${i}" transform="translate(${x} 83)"><rect width="70" height="49" rx="9" class="scene-paper"/><path d="M15 18h40M15 29h28"/></g>`).join('')}`; break;
    case 'writing': artwork = `<g transform="translate(99 14)"><rect width="126" height="123" rx="13" class="scene-paper"/><path d="M23 24h53"/><g class="scene-writing-lines"><path d="M23 44h81M23 56h68M23 68h77"/></g><g class="scene-writing-lines scene-delay-1"><path d="M23 88h74M23 100h47"/></g></g><g class="scene-review" transform="translate(217 76)"><circle cx="23" cy="23" r="27" class="scene-glass"/><path d="m11 23 8 8 16-18"/></g>`; break;
    case 'saving': artwork = `<path d="M97 46V34a9 9 0 0 1 9-9h51l14 14h82a10 10 0 0 1 10 10v69H97Z" class="scene-muted"/><g class="scene-saving-sheet"><rect x="133" y="17" width="79" height="92" rx="9" class="scene-paper"/><path d="M150 37h43M150 48h32M150 59h38"/></g><path d="M94 65a8 8 0 0 1 8-9h161a8 8 0 0 1 8 9l-9 58H103Z" class="scene-glass"/><path d="m165 88 11 11 20-23"/>`; break;
    case 'audio': case 'speech': artwork = `<rect x="46" y="34" width="268" height="86" rx="22" class="scene-glass"/><path class="scene-link" d="M63 77h234"/>${[20,35,52,31,61,42,70,46,55,29,40,20].map((height,i)=>`<rect class="scene-audio-bar scene-delay-${i%3}" x="${74+i*19}" y="${77-height/2}" width="4" height="${height}" rx="2" style="--bar-delay:${i*.12}s"/>`).join('')}`; break;
    case 'image': artwork = `<rect x="91" y="19" width="178" height="116" rx="14" class="scene-paper"/><g class="scene-image-layer"><path d="m109 116 49-51 34 32 26-22 34 41Z" class="scene-muted"/><circle cx="223" cy="50" r="12" class="scene-highlight"/></g><path d="M80 42V22a12 12 0 0 1 12-12h20M248 10h20a12 12 0 0 1 12 12v20M280 111v19a12 12 0 0 1-12 12h-20M112 142H92a12 12 0 0 1-12-12v-19" class="scene-crop"/>`; break;
    case 'video': artwork = `<g transform="translate(62 34) rotate(-6 95 45)"><rect width="188" height="98" rx="12" class="scene-muted"/></g><g class="scene-video-front" transform="translate(102 22)"><rect width="188" height="108" rx="12" class="scene-paper"/><path d="m78 31 37 24-37 24Z" class="scene-highlight"/><path d="M15 95h157"/></g>`; break;
    default: artwork = `<circle cx="180" cy="77" r="42" class="scene-glass"/><path d="M162 61h36M162 76h36M162 91h24"/>`;
  }
  return `<svg class="generation-scene" data-scene-kind="${escape(kind)}" viewBox="0 0 360 154" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${artwork}</svg>`;
}
function sceneLayers(view, selected) {
  const kind = selected?.kind || 'generic';
  return [...new Set([...view.stages.map(stage => stage.kind),kind])].map(value =>
    layerMarkup(value,visualMarkup(value),kind,selected?.status === 'active' && view.status === 'running')).join('');
}
function detailedStageMarkup(stages, selected) {
  return stages.map((stage,index) => `<li class="generation-stage-choice" data-stage="${escape(stage.id)}" data-state="${stage.status}"><button type="button" data-stage-select="${escape(stage.id)}" aria-pressed="${stage.id === selected?.id}" ${stage.status === 'active' ? 'aria-current="step"' : ''}><span class="generation-stage-icon">${stageIcon(stage.kind)}</span><span class="generation-stage-choice-label"><span class="generation-stage-label">${m(stage.label)}</span><span class="generation-stage-state">${m(stageStatuses[stage.status])}</span></span><span class="generation-stage-time">${durationMarkup(stage.elapsed,stage.timingComplete)}</span></button></li>`).join('');
}
function renderDetailedProgress(job) {
  const view = generationProgressView(job), selected = currentStage(view);
  return `<section class="generation-progress generation-progress-detailed" data-generation-progress data-mode="detailed" data-state="${escape(view.status)}" aria-labelledby="generation-progress-title"><a class="generation-back" href="#jobs"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m12 5-5 5 5 5"/></svg>${m('任务记录')}</a><header class="generation-workbench-heading"><div class="generation-workbench-status" role="status" aria-live="polite" aria-atomic="true"><h1 id="generation-progress-title">${m(view.title)}</h1><p class="generation-counter" data-progress-count ${view.counter ? '' : 'hidden'}>${view.counter ? m('已检查 {done} / {total} 题',view.counter) : ''}</p></div><div class="generation-total-time"><span>${m('总处理耗时')}</span><strong data-progress-total-time>${durationMarkup(view.elapsed,view.timingComplete)}</strong></div></header>${meterMarkup(view)}<div class="generation-workbench"><aside class="generation-stage-nav" ${a('生成步骤','aria-label')}><ol data-progress-stages>${detailedStageMarkup(view.stages,selected)}</ol><button class="generation-follow" data-progress-follow disabled>${m('正在跟随当前步骤')}</button></aside><section class="generation-stage-workspace" aria-labelledby="generation-stage-title"><div class="generation-workspace-top"><h2 id="generation-stage-title">${m(selected?.label || '任务概况')}</h2><span class="generation-workspace-time" data-progress-selected-time>${durationMarkup(selected?.elapsed,selected?.timingComplete)}</span></div><div class="generation-visual" data-progress-visual data-motion="${selected?.status === 'active' && view.status === 'running' ? 'active' : 'static'}">${sceneLayers(view,selected)}</div><p class="generation-stage-headline" data-progress-stage-headline>${m(selectedHeadline(view,selected))}</p><div class="generation-events-header"><h3>${m('处理记录')}</h3><span data-progress-truncated ${view.truncated ? '' : 'hidden'}>${m('仅显示最近记录')}</span></div><ol class="generation-events" data-progress-events tabindex="0" ${a('处理记录','aria-label')}>${eventsMarkup(view,selected)}</ol></section></div><div class="generation-workbench-footer"><div><p class="generation-progress-message" data-progress-message ${view.message ? '' : 'hidden'}>${view.message ? m(view.message) : ''}</p><details class="generation-failure-detail" data-progress-reason ${view.reason ? '' : 'hidden'}>${reasonMarkup(view)}</details></div><div class="generation-progress-actions" data-progress-actions>${actionMarkup(view)}</div></div><div class="generation-connection" data-progress-connection hidden><p role="status">${m('连接暂时中断，正在重试')}</p><button class="link" data-action="retry">${m('刷新状态')}</button></div></section>`;
}

export function renderGenerationProgress(job, {mode = 'simple'} = {}) {
  return mode === 'detailed' ? renderDetailedProgress(job) : renderSimpleProgress(job);
}

function mountDetailedProgress(root, initial) {
  let view = generationProgressView(initial), selected = currentStage(view), following = true;
  const find = selector => root.querySelector(selector);
  const motion = progressMotion(root);
  const equal = (left,right) => JSON.stringify(left) === JSON.stringify(right);
  function ensureSelectedVisible() {
    const nav = find('[data-progress-stages]');
    if (!selected || nav.scrollWidth <= nav.clientWidth || !nav.getBoundingClientRect) return;
    const control = [...nav.querySelectorAll('[data-stage-select]')].find(control => control.dataset.stageSelect === selected.id);
    if (!control?.getBoundingClientRect) return;
    const bounds = nav.getBoundingClientRect(), item = control.getBoundingClientRect();
    // Scroll this horizontal navigation only; never pull the whole page or
    // reset a user's position on a duration-only polling update.
    if (item.left < bounds.left) nav.scrollLeft = Math.max(0, nav.scrollLeft + item.left - bounds.left - 8);
    else if (item.right > bounds.right) nav.scrollLeft += item.right - bounds.right + 8;
  }
  function paintSelected(previousSelected, previousView, options = {}) {
    root.dataset.progressInput = options.instant ? 'keyboard' : 'continuous';
    if (previousSelected?.id !== selected?.id) motion.text(find('#generation-stage-title'),selected?.label || '任务概况',options);
    const visual = find('[data-progress-visual]'), running = selected?.status === 'active' && view.status === 'running';
    if (previousSelected?.kind !== selected?.kind || previousSelected?.status !== selected?.status || previousView.status !== view.status) {
      selectLayer(visual,selected?.kind || 'generic',visualMarkup(selected?.kind || 'generic'),{running,...options});
    }
    visual.dataset.motion = running ? 'active' : 'static';
    if (previousSelected?.elapsed !== selected?.elapsed || previousSelected?.timingComplete !== selected?.timingComplete) find('[data-progress-selected-time]').innerHTML = durationMarkup(selected?.elapsed,selected?.timingComplete);
    const headline = selectedHeadline(view,selected);
    if (headline !== selectedHeadline(previousView,previousSelected)) motion.text(find('[data-progress-stage-headline]'),headline,options);
    if (previousSelected?.id !== selected?.id || !equal(eventsFor(view,selected),eventsFor(previousView,previousSelected))) {
      const list = find('[data-progress-events]'), atEnd = list.scrollHeight - list.clientHeight - list.scrollTop < 24, scroll = list.scrollTop;
      list.innerHTML = eventsMarkup(view,selected);
      list.scrollTop = previousSelected?.id !== selected?.id ? 0 : atEnd ? list.scrollHeight : scroll;
      if (previousSelected?.id !== selected?.id) motion.soften(list,options);
    }
    for (const control of find('[data-progress-stages]').querySelectorAll('[data-stage-select]')) control.setAttribute('aria-pressed', String(control.dataset.stageSelect === selected?.id));
    if (following && previousSelected?.id !== selected?.id) ensureSelectedVisible();
  }
  function paintNav(previousView) {
    const nav = find('[data-progress-stages]');
    if (!equal(view.stages.map(stage=>[stage.id,stage.kind]),previousView.stages.map(stage=>[stage.id,stage.kind]))) {
      const focused = root.ownerDocument?.activeElement;
      const restore = nav.contains?.(focused) ? focused?.dataset.stageSelect : null;
      nav.innerHTML = detailedStageMarkup(view.stages,selected);
      if (restore) [...nav.querySelectorAll('[data-stage-select]')].find(control=>control.dataset.stageSelect === restore)?.focus({preventScroll:true});
      return;
    }
    const rows = [...nav.querySelectorAll('[data-stage]')];
    for (const stage of view.stages) {
      const row = rows.find(row=>row.dataset.stage === stage.id), old = previousView.stages.find(item=>item.id === stage.id);
      if (!row || equal(old,stage)) continue;
      const control = row.querySelector('[data-stage-select]');
      if (old.status !== stage.status) {
        row.dataset.state = stage.status;
        if (stage.status === 'active') control.setAttribute('aria-current','step'); else control.removeAttribute('aria-current');
        motion.text(row.querySelector('.generation-stage-state'),stageStatuses[stage.status]);
      }
      if (old.elapsed !== stage.elapsed || old.timingComplete !== stage.timingComplete) row.querySelector('.generation-stage-time').innerHTML = durationMarkup(stage.elapsed,stage.timingComplete);
    }
  }
  function follow(options) {
    const previousSelected = selected;
    following = true; selected = currentStage(view);
    find('[data-progress-follow]').disabled = true;
    setText(find('[data-progress-follow]'),'正在跟随当前步骤');
    paintSelected(previousSelected,view,options);
  }
  const click = event => {
    // Browser-generated keyboard clicks have no pointer click count.
    const options = {instant:event.detail === 0};
    const control = event.target.closest('[data-stage-select]');
    if (control && root.contains(control)) {
      const previousSelected = selected;
      selected = view.stages.find(stage=>stage.id === control.dataset.stageSelect) || selected;
      following = false;
      find('[data-progress-follow]').disabled = false;
      setText(find('[data-progress-follow]'),'跟随当前步骤');
      paintSelected(previousSelected,view,options);
    } else if (event.target.closest('[data-progress-follow]')) follow(options);
  };
  root.addEventListener('click',click);
  ensureSelectedVisible();
  return {
    update(job) {
      const previousView = view, previousSelected = selected;
      view = generationProgressView(job);
      selected = progressSelection(view,selected?.id,following);
      root.dataset.state = view.status;
      updateMeter(root, previousView, view);
      if (previousView.title !== view.title) motion.text(find('#generation-progress-title'),view.title);
      if (!equal(previousView.counter,view.counter)) {
        const counter = find('[data-progress-count]'); counter.hidden = !view.counter;
        if (view.counter) setText(counter,'已检查 {done} / {total} 题',view.counter);
      }
      if (previousView.elapsed !== view.elapsed || previousView.timingComplete !== view.timingComplete) find('[data-progress-total-time]').innerHTML = durationMarkup(view.elapsed,view.timingComplete);
      if (previousView.message !== view.message) { const message=find('[data-progress-message]');message.hidden=!view.message;if(view.message)setText(message,view.message); }
      if (previousView.reason !== view.reason) { const reason=find('[data-progress-reason]');reason.hidden=!view.reason;reason.innerHTML=reasonMarkup(view); }
      if (actionMarkup(previousView) !== actionMarkup(view)) { find('[data-progress-actions]').innerHTML=actionMarkup(view); motion.soften(find('[data-progress-actions]')); }
      find('[data-progress-truncated]').hidden=!view.truncated;
      paintNav(previousView); paintSelected(previousSelected,previousView);
    },
    connection(error) { find('[data-progress-connection]').hidden=!error;root.dataset.connection=error?'interrupted':'connected'; },
    destroy() { root.removeEventListener('click',click); motion.destroy(); },
  };
}
export function mountGenerationProgress(root, initial, {mode = 'simple'} = {}) {
  return mode === 'detailed' ? mountDetailedProgress(root,initial) : mountSimpleProgress(root,initial);
}
