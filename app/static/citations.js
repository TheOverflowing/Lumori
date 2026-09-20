import { a, m, t } from './i18n.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const contextId = /^context_[a-f0-9]{32}$/i;
const uniqueIds = values => [...new Set((Array.isArray(values) ? values : []).filter(value => typeof value === 'string' && value.trim()))];

function citationIndex(sources, asset) {
  const records = new Map();
  for (const source of Array.isArray(sources) ? sources : []) {
    if (typeof source?.id === 'string' && source.id && !records.has(source.id))
      records.set(source.id, {source, number:records.size + 1});
  }
  // Declared references without a source remain visible, but never masquerade as evidence.
  const visit = value => {
    if (!value || typeof value !== 'object') return;
    for (const id of uniqueIds(value.citation_ids)) {
      if (!records.has(id)) records.set(id, {source:null, number:records.size + 1});
    }
    for (const [key, child] of Object.entries(value)) {
      if (key !== 'citation_ids' && child && typeof child === 'object') {
        if (Array.isArray(child)) child.forEach(visit);
        else visit(child);
      }
    }
  };
  visit(asset);
  return records;
}

// Authored code and ordinary bracket notation are text, not citation controls.
function fragments(value, records) {
  const text = String(value ?? '');
  const pattern = /(`{3,})[\s\S]*?(?:\1|$)|(`{1,2})[^\n]*?\2|\[([^\[\]\n]+)\]|\(([^()\n]+)\)|（([^（）\n]+)）/g;
  const result = [];
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    const ids = (match[3] ?? match[4] ?? match[5])?.trim().split(/[\s,;，；、]+/u).filter(Boolean);
    const citation = ids?.length && ids.every(id => records.has(id) || contextId.test(id));
    if (!citation) continue;
    result.push({text:text.slice(start, match.index)}, {ids:uniqueIds(ids)});
    start = match.index + match[0].length;
  }
  result.push({text:text.slice(start)});
  return result;
}

function referenceButton(id, records) {
  const record = records.get(id);
  const available = Boolean(record?.source);
  const number = record?.number ?? '?';
  return `<button type="button" class="citation-ref${available ? '' : ' citation-ref-unavailable'}" data-citation-id="${escape(id)}" aria-haspopup="dialog" aria-expanded="false"><span class="citation-ref-face" aria-hidden="true">${number}</span><span class="sr-only">${available ? m('引用 {n}',{n:number}) : record ? `${m('引用 {n}',{n:number})} · ${m('引用来源不可用')}` : m('引用来源不可用')}</span></button>`;
}

export function createCitationRenderer(sources = [], asset = {}, {enabled = true} = {}) {
  const records = citationIndex(sources, asset);
  const controls = ids => ids.map(id => referenceButton(id,records)).join('');
  return {
    number: id => records.get(id)?.number ?? null,
    text(value) {
      return fragments(value,records).map(part => part.ids
        ? enabled ? `<span class="citation-group">${controls(part.ids)}</span>` : ''
        : escape(part.text)).join('');
    },
    references(ids, texts = []) {
      if (!enabled) return '';
      const inline = new Set((Array.isArray(texts) ? texts : [texts]).flatMap(value => fragments(value,records).flatMap(part => part.ids || [])));
      const remaining = uniqueIds(ids).filter(id => !inline.has(id));
      if (!remaining.length) return '';
      return `<div class="citation-references"><span class="citation-references-label">${m('来源')}</span>${controls(remaining)}</div>`;
    }
  };
}

function webLink(source) {
  const external = source?.external_source || source?.metadata?.external_source;
  for (const value of [external?.reading_url, external?.url]) {
    try {
      const url = new URL(value);
      if (['https:','http:'].includes(url.protocol) && url.hostname && !url.username && !url.password)
        return url;
    } catch { /* A missing or unsupported URL does not become an actionable link. */ }
  }
  return null;
}

export function renderCitationPreview(id, sources = [], asset = {}, titleId = 'citation-preview-title') {
  const record = citationIndex(sources,asset).get(id);
  const source = record?.source;
  const close = `<button type="button" class="citation-preview-close" data-citation-close ${a('关闭引用来源','aria-label')}><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg></button>`;
  if (!source) return `<div class="citation-preview-heading"><h2 id="${escape(titleId)}">${m('来源暂不可用')}</h2>${close}</div><p class="citation-preview-empty">${m('这条引用没有附带原始资料。')}</p>`;
  const link = webLink(source);
  const external = source.external_source || source.metadata?.external_source;
  const title = external?.title || source.document_name || t('未命名资料');
  const page = Number.isInteger(source.page) && source.page > 0 ? source.page : null;
  const assetId = source.metadata?.source_asset_ids?.[0];
  const figure = source.metadata?.source_kind === 'figure' && typeof source.document_id === 'string' && typeof assetId === 'string' && assetId
    ? `<img class="citation-preview-figure" loading="lazy" src="/api/documents/${encodeURIComponent(source.document_id)}/assets/${encodeURIComponent(assetId)}" ${a('原文图像','alt')}>` : '';
  return `<div class="citation-preview-heading"><div><div class="citation-preview-kicker">${m('来源 {n}',{n:record.number})}${page ? `<span aria-hidden="true"> · </span>${m('第 {n} 页',{n:page})}` : ''}</div><h2 id="${escape(titleId)}">${escape(title)}</h2></div>${close}</div>${figure}${source.text ? `<blockquote class="citation-preview-excerpt" tabindex="0">${escape(source.text)}</blockquote>` : `<p class="citation-preview-empty">${m('这条引用没有附带原始资料。')}</p>`}${link ? `<a class="citation-preview-link" href="${escape(link.href)}" target="_blank" rel="noopener noreferrer"><span>${m('打开原文')}</span><span aria-hidden="true">↗</span><span class="citation-preview-domain">${escape(link.hostname)}</span></a>` : ''}`;
}

let nextPreview = 0;

export function mountCitations(root, sources = [], asset = {}) {
  if (!root?.addEventListener) return () => {};
  const doc = root.ownerDocument || document;
  const win = doc.defaultView || window;
  const preview = doc.createElement('div');
  const id = `citation-preview-${++nextPreview}`;
  preview.id = id;
  preview.className = 'citation-preview';
  preview.hidden = true;
  preview.setAttribute('role','dialog');
  preview.setAttribute('aria-modal','false');
  preview.setAttribute('aria-labelledby',`${id}-title`);
  preview.tabIndex = -1;
  doc.body.append(preview);
  let active = null;
  let disposed = false;
  const close = (restore = false) => {
    if (!active) return;
    const previous = active;
    previous.setAttribute('aria-expanded','false');
    previous.removeAttribute('aria-controls');
    preview.hidden = true;
    active = null;
    if (restore && previous.isConnected) previous.focus({preventScroll:true});
  };
  const position = () => {
    if (!active) return;
    if (!active.isConnected) { close(); return; }
    const anchor = active.getBoundingClientRect();
    const viewport = win.visualViewport;
    const leftEdge = viewport?.offsetLeft || 0;
    const topEdge = viewport?.offsetTop || 0;
    const width = viewport?.width || win.innerWidth;
    const height = viewport?.height || win.innerHeight;
    const edge = 12;
    if (anchor.bottom < topEdge || anchor.top > topEdge + height) { close(); return; }
    preview.style.maxHeight = `${Math.max(120,height - edge * 2)}px`;
    preview.style.width = `${Math.min(380,width - edge * 2)}px`;
    // The entry transform scales the presentation rect. Placement must use the
    // settled layout size so the preview keeps its edge inset after animation.
    const box = {width:preview.offsetWidth, height:preview.offsetHeight};
    const x = Math.max(leftEdge + edge,Math.min(anchor.left - 10,leftEdge + width - box.width - edge));
    const below = anchor.bottom + 8;
    const above = anchor.top - box.height - 8;
    const openAbove = below + box.height > topEdge + height - edge && above >= topEdge + edge;
    const y = Math.max(topEdge + edge,Math.min(openAbove ? above : below,topEdge + height - box.height - edge));
    preview.style.left = `${x}px`;
    preview.style.top = `${y}px`;
    preview.style.transformOrigin = `${Math.max(16,Math.min(box.width - 16,anchor.left + anchor.width / 2 - x))}px ${openAbove ? '100%' : '0%'}`;
  };
  const click = event => {
    const button = event.target?.closest?.('[data-citation-id]');
    if (!button || !root.contains(button)) return;
    event.preventDefault();
    if (button === active) { close(); return; }
    const switching = Boolean(active);
    close();
    active = button;
    button.setAttribute('aria-expanded','true');
    button.setAttribute('aria-controls',id);
    preview.innerHTML = renderCitationPreview(button.dataset.citationId,sources,asset,`${id}-title`);
    preview.dataset.instant = String(event.detail === 0 || switching);
    preview.hidden = false;
    position();
    if (event.detail === 0) preview.focus({preventScroll:true});
  };
  const outside = event => {
    const reference = event.target?.closest?.('[data-citation-id]');
    if (reference && root.contains(reference)) return;
    if (active && !preview.contains(event.target) && !active.contains(event.target)) close();
  };
  const focus = event => {
    if (active && !preview.contains(event.target) && !active.contains(event.target)) close();
  };
  const keydown = event => {
    if (active && event.key === 'Escape') { event.preventDefault(); close(true); }
  };
  const previewClick = event => {
    if (event.target?.closest?.('[data-citation-close]')) close(true);
  };
  const language = () => {
    if (active) {
      preview.innerHTML = renderCitationPreview(active.dataset.citationId,sources,asset,`${id}-title`);
      position();
    }
  };
  root.addEventListener('click',click);
  preview.addEventListener('click',previewClick);
  doc.addEventListener('pointerdown',outside);
  doc.addEventListener('focusin',focus);
  doc.addEventListener('keydown',keydown);
  win.addEventListener('resize',position);
  win.addEventListener('scroll',position,true);
  win.addEventListener('appearancechange',language);
  win.visualViewport?.addEventListener('resize',position);
  const resize = typeof win.ResizeObserver === 'function' ? new win.ResizeObserver(position) : null;
  resize?.observe(preview);
  return () => {
    if (disposed) return;
    disposed = true;
    close();
    root.removeEventListener('click',click);
    preview.removeEventListener('click',previewClick);
    doc.removeEventListener('pointerdown',outside);
    doc.removeEventListener('focusin',focus);
    doc.removeEventListener('keydown',keydown);
    win.removeEventListener('resize',position);
    win.removeEventListener('scroll',position,true);
    win.removeEventListener('appearancechange',language);
    win.visualViewport?.removeEventListener('resize',position);
    resize?.disconnect();
    preview.remove();
  };
}
