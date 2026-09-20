import { m } from './i18n.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

// Search cannot repair a missing course, model connection, or invalid explicit scope.
export function generationEligibility({course, documents = [], status = {}, autoExplore = false, documentIds = []} = {}) {
  if (!course) return {allowed:false, reason:'请先选择课程'};
  if (!status.capabilities?.text?.configured || !status.capabilities?.embedding?.configured)
    return {allowed:false, reason:'请先配置文本与嵌入模型'};
  if (documentIds.length && documentIds.some(id => !documents.some(doc => doc.id === id && doc.index_current && doc.enabled !== false && !doc.deleted_at)))
    return {allowed:false, reason:'选定资料尚未就绪，请检查资料与索引'};
  if (autoExplore === true && status.auto_exploration?.available !== true)
    return {allowed:false, reason:'自动探索暂不可用，请检查搜索配置或关闭自动探索'};
  const ready = documents.some(doc => doc.index_current && doc.enabled !== false && !doc.deleted_at && (!documentIds.length || documentIds.includes(doc.id)));
  const exploration = autoExplore === true && status.auto_exploration?.available === true;
  if (ready || exploration) return {allowed:true, reason:'', exploration};
  return {allowed:false, reason:autoExplore ? '自动探索暂不可用，请先准备课程资料' : '请先准备课程资料，或在设置中开启自动探索'};
}

export function renderExternalSource(document) {
  const source = document?.external_source || document?.metadata?.external_source;
  if (!source || typeof source !== 'object') return '';
  let url;
  try {
    url = new URL(source.url);
    if (!['http:','https:'].includes(url.protocol) || !url.hostname || url.username || url.password) url = null;
  } catch { url = null; }
  const link = url ? `<a class="external-source-link" href="${escape(url.href)}" target="_blank" rel="noopener noreferrer" title="${escape(source.title || url.hostname)}">${escape(url.hostname)}<span aria-hidden="true">↗</span><span class="sr-only">${m('打开原始来源')}</span></a>` : '';
  return `<div class="external-source"><span class="badge external-source-badge">${m('自动发现')}</span>${link}</div>`;
}
