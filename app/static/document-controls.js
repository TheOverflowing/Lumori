import {m,a} from './i18n.js';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function documentControls(doc){
  const id=esc(doc.id);
  if(doc.deleted_at)return `<button class="secondary" data-action="restore-document" data-id="${id}">${m('恢复文件')}</button>`;
  return `<label class="document-switch" ${a('用于生成','title')}><input type="checkbox" role="switch" class="document-enabled" data-document="${id}" ${doc.enabled!==false?'checked':''} ${a('用于生成','aria-label')} aria-describedby="document-name-${id}"><span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span></label><button class="plain document-trash" data-action="delete-document" data-id="${id}" ${a('移到最近删除','aria-label')} aria-describedby="document-name-${id}">${m('删除')}</button>`;
}
