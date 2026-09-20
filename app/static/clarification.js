import { m, a } from './i18n.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const preparationErrors = new Set(['preparation_expired', 'preparation_changed', 'preparation_pending', 'preparation_incomplete', 'preparation_used', 'preparation_missing']);
const restartMessage = '需求检查已失效，请重新点击“开始生成”。';
const clone = value => structuredClone(value);
const failureMessages = {
  upstream_unavailable:'文本模型服务暂不可用，生成也可能失败。请稍后再试，本页输入会保留。',
  authentication:'文本模型连接未通过身份或权限验证。请检查模型连接后再试；直接生成也可能失败。',
  payment_required:'文本模型账户余额不足。请处理账户余额后再试；直接生成也可能失败。',
  configuration:'文本模型接口或参数未被接受。请检查模型连接后再试；直接生成也可能失败。',
  rate_limited:'文本模型服务限流或额度已用尽，生成也可能失败。请稍后或调整额度后再试。',
  timeout:'需求检查等待超时，生成也可能受到影响。可稍后再试，或按原输入尝试生成。',
  network:'暂时无法连接文本模型服务，生成也可能失败。请检查连接后再试。',
  invalid_output:'需求检查返回的内容无法使用，可跳过检查，按原输入继续生成。',
  unknown:'需求检查未完成，生成也可能受到影响。可稍后再试，或按原输入尝试生成。',
};

function validatePreparation(result) {
  if (!result || !/^[a-f0-9]{32}$/.test(result.preparation_id || '') ||
      !['ready', 'clarification_required', 'unavailable'].includes(result.status) ||
      !Number.isFinite(result.expires_at) || !Array.isArray(result.options) || result.options.length > 3 ||
      result.options.some(option => typeof option !== 'string') ||
      (result.status === 'clarification_required' && (typeof result.question !== 'string' || !result.question.trim()))) {
    throw new Error('需求检查返回异常，请重试或取消勾选后使用原输入。');
  }
  return clone(result);
}

// Keep the submitted generation snapshot independent of editable page drafts.
// Cancellation invalidates callbacks; it does not pretend to stop server work.
export function createGenerationFlow({ request, isCurrent = () => true, onState = () => {}, onCreated = () => {} }) {
  let revision = 0, payload = null, preparation = null, lastAdditions = null;
  let state = { phase:'idle', originalTopic:'', answer:'' };
  const current = token => token === revision && isCurrent();
  function update(phase, extra = {}) {
    state = {...state, phase, ...extra};
    onState(clone(state));
  }
  function restart(error) {
    revision++;
    payload = null; preparation = null;
    update('expired');
    const next = new Error(restartMessage);
    next.code = error.code;
    next.uiMessage = {key:restartMessage};
    return next;
  }
  async function generate(token, additions, returnPhase) {
    if (!current(token)) return null;
    lastAdditions = clone(additions);
    update('submitting');
    try {
      const result = await request('/generations', 'POST', {...clone(payload), ...additions});
      if (!current(token)) return null;
      update('complete');
      onCreated(result, clone(payload));
      return result;
    } catch (error) {
      if (!current(token)) return null;
      if (preparationErrors.has(error.code)) throw restart(error);
      update(returnPhase);
      throw error;
    }
  }
  async function respond(action, answer = '') {
    const expected = action === 'skip' ? 'unavailable' : 'clarification_required';
    if (state.phase !== expected || !preparation || !payload) return null;
    if (action === 'answer' && (typeof answer !== 'string' || !answer.trim() || answer.length > 2000)) {
      throw new Error('请填写补充信息，最多 2000 个字符。');
    }
    const token = revision;
    state.answer = action === 'answer' ? answer : state.answer;
    return generate(token, {
      preparation_id:preparation.preparation_id,
      clarification_action:action,
      clarification_answer:action === 'answer' ? answer : '',
    }, expected);
  }
  return {
    get state() { return clone(state); },
    async start(snapshot, {check = true} = {}) {
      if (['checking', 'submitting'].includes(state.phase)) return null;
      const token = ++revision;
      payload = clone(snapshot); preparation = null; lastAdditions = null;
      state = {phase:'idle', originalTopic:payload.topic || '', language:payload.language, answer:''};
      if (!check) return generate(token, {}, 'retry_generation');
      update('checking');
      try {
        const result = await request('/generations/prepare', 'POST', clone(payload));
        if (!current(token)) return null;
        preparation = validatePreparation(result);
        if (preparation.status === 'ready') {
          return await generate(token, {preparation_id:preparation.preparation_id}, 'retry_generation');
        }
        update(preparation.status, {question:preparation.question || '', options:preparation.options,
          failureCode:Object.hasOwn(failureMessages,preparation.failure_code) ? preparation.failure_code : 'unknown'});
        return null;
      } catch (error) {
        if (!current(token)) {
          // A rejected preparation can explicitly expire this same flow.
          if (state.phase === 'expired' && error.message === restartMessage) throw error;
          return null;
        }
        if(state.phase !== 'retry_generation')update('idle');
        throw error;
      }
    },
    answer: answer => respond('answer', answer),
    unknown: () => respond('unknown'),
    skip: () => respond('skip'),
    retry: () => state.phase === 'retry_generation' && payload && lastAdditions
      ? generate(revision, lastAdditions, 'retry_generation') : Promise.resolve(null),
    cancel() {
      revision++; payload = null; preparation = null; lastAdditions = null;
      update('cancelled');
    },
  };
}

export function renderPreparationDialog(state) {
  const clarify = state.phase === 'clarification_required';
  const failureMessage = failureMessages[Object.hasOwn(failureMessages,state.failureCode) ? state.failureCode : 'unknown'];
  const original = `<details class="clarification-original"><summary>${m('原始学习目标')}</summary><p>${escape(state.originalTopic)}</p></details>`;
  const question = clarify ? `<p class="clarification-question" lang="${state.language === 'en' ? 'en' : 'zh-CN'}">${escape(state.question)}</p>` : '';
  const options = clarify && state.options?.length ? `<fieldset class="clarification-options"><legend class="sr-only">${m('选择回答')}</legend><div>${state.options.map((option, index) => `<button type="button" class="secondary" data-clarification-choice="${index}" aria-pressed="${state.answer===option}">${escape(option)}</button>`).join('')}</div></fieldset>` : '';
  const input = clarify ? `<label for="clarification-answer">${m('补充信息')}<textarea id="clarification-answer" name="answer" required maxlength="2000" rows="3" ${a('输入补充信息','placeholder')}>${escape(state.answer)}</textarea></label>` : `<p class="clarification-unavailable" role="status">${m(failureMessage)}</p>`;
  return `<form id="clarification-form" data-clarification-dialog>${question}${options}${input}${original}<div class="modal-actions clarification-actions"><button type="button" class="secondary" data-clarification-cancel>${m('取消')}</button>${clarify ? `<button type="button" class="secondary" data-clarification-unknown ${a('不确定，按原输入继续','aria-label')}>${m('不确定')}</button><button type="submit">${m('继续生成')}</button>` : `<button type="submit">${m(state.failureCode === 'invalid_output' ? '按原输入继续' : '仍按原输入尝试')}</button>`}</div></form>`;
}
