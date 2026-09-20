import { a, m } from './i18n.js';

// Add a format here when its generation and review flow becomes available.
// The initial request produces text; supported media follows text approval.
export const outputFormats = Object.freeze([
  { kind: 'text', label: '文字', icon: 'file-text', stage: 'initial' },
  { kind: 'audio', label: '语音', icon: 'audio-lines', stage: 'reviewed', action: '生成语音' },
  { kind: 'image', label: '配图', icon: 'image', stage: 'reviewed', action: '生成配图' },
  { kind: 'video', label: '视频', icon: 'video', stage: 'upcoming', action: '生成视频' },
]);

const icon = name => `<img class="icon" src="/static/icons/${name}.svg" alt="">`;

export function renderOutputFormats() {
  return `<fieldset class="output-format-picker"><legend>${m('输出形式')}</legend><div class="output-formats">${outputFormats.map(format => {
    const selected = format.stage === 'initial';
    const reason = format.stage === 'upcoming' ? '即将支持' : '文字审核后可用';
    return `<label class="output-format" ${selected ? '' : a(reason, 'title')}><input type="radio" name="output_format" value="${format.kind}" ${selected ? 'checked' : `disabled aria-describedby="format-${format.kind}-availability"`}>${icon(format.icon)}<span>${m(format.label)}</span>${selected ? `<span class="format-check">${icon('check')}</span>` : `<small id="format-${format.kind}-availability" class="${format.stage === 'upcoming' ? 'format-availability' : 'sr-only'}">${m(reason)}</small>`}</label>`;
  }).join('')}</div></fieldset>`;
}

export function renderMultimodal(content, renderMedia) {
  const approved = content.status === 'approved';
  return `<section class="inspector-section multimodal-section"><h2>${m('多模态')}</h2>${approved ? '' : `<p class="multimodal-prerequisite">${m('文字审核后可用')}</p>`}<div class="multimodal-slots">${outputFormats.filter(format => format.stage !== 'initial').map(format => {
    const upcoming = format.stage === 'upcoming';
    const disabled = upcoming || !approved;
    const reason = upcoming ? '即将支持' : '文字审核后可用';
    const media = (content.media || []).filter(item => item.kind === format.kind);
    return `<div class="multimodal-slot" data-format="${format.kind}"><div class="multimodal-slot-heading"><h3>${icon(format.icon)}${m(format.label)}</h3><button type="button" class="secondary media-generate" ${upcoming ? '' : `data-action="make-media" data-kind="${format.kind}"`} ${a(format.action, 'aria-label')} ${disabled ? `disabled ${a(reason, 'title')}` : ''}>${m(upcoming ? '即将支持' : '生成')}</button></div>${renderMedia(media, true, content.version)}</div>`;
  }).join('')}</div></section>`;
}
