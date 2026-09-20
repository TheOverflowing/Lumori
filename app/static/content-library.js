import { formatDate, m } from './i18n.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const materials = {
  lesson:{kind:'lesson',icon:'book-open',label:'学习讲解'},
  quiz:{kind:'quiz',icon:'layers',label:'测验练习'},
  assignment:{kind:'assignment',icon:'file-text',label:'课后作业'},
};
const genericMaterial = {kind:'unknown',icon:'files',label:'学习材料'};

export function materialIdentity(content = {}) {
  // Library responses carry material directly; a detail response keeps it in config.
  const material = content.material ?? content.config?.material;
  return Object.hasOwn(materials, material) ? {...materials[material]} : {...genericMaterial};
}

export function courseLibraryRows(rows, courseId) {
  if (!courseId || !Array.isArray(rows)) return [];
  return rows.filter(row => row?.course_id === courseId);
}

export function renderLibraryRow(content, {showCourse = false} = {}) {
  const material = materialIdentity(content);
  const statuses = {draft:'待审核',approved:'已确认'};
  const status = Object.hasOwn(statuses, content.status) ? content.status : '';
  const date = `<time data-date="${escape(content.created_at)}">${escape(formatDate(content.created_at))}</time>`;
  const metadata = [
    `<span class="material-type">${m(material.label)}</span>`,
    ...(showCourse && content.course_name ? [`<span>${escape(content.course_name)}</span>`] : []),
    `<span>${m('版本 {n}',{n:content.version})}</span>`,
    date,
  ];
  return `<div class="collection-row material-row" data-material="${material.kind}"><span class="file-icon" aria-hidden="true"><img class="icon" src="/static/icons/${material.icon}.svg" alt=""></span><div class="row-main"><h3>${escape(content.title)}</h3><p class="material-row-meta">${metadata.join('<span class="material-meta-divider" aria-hidden="true">·</span>')}</p></div><span class="badge ${status}">${m(statuses[status] || '未记录')}</span><div class="row-actions"><button type="button" class="secondary" data-action="open-content" data-id="${escape(content.id)}">${m('打开')}</button></div></div>`;
}
