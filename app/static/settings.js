import { m } from './i18n.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const labels = {vision:'视觉理解模型',text:'文本模型',embedding:'嵌入模型',speech:'语音模型',image:'图像模型'};

export function renderSwitch({id, label, checked = false, className = ''}) {
  return `<label class="document-switch preference-switch ${className}" for="${id}"><span>${m(label)}</span><input type="checkbox" role="switch" id="${id}" ${checked?'checked':''}><span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span></label>`;
}

export function renderSettings(queryFusion, progressMode = 'detailed', autoExplore = false) {
  const progressChoices = [['simple','简洁'],['detailed','详细']].map(([value,label])=>`<label class="settings-choice"><input type="radio" name="progress-mode" value="${value}" ${progressMode===value?'checked':''}><span>${m(label)}</span></label>`).join('');
  return `<div class="settings-panel" data-settings-panel><section class="settings-group" aria-labelledby="generation-settings-title"><h3 id="generation-settings-title">${m('生成')}</h3>${renderSwitch({id:'query-fusion',label:'融合模式',checked:queryFusion})}${renderSwitch({id:'auto-explore',label:'自动探索',checked:autoExplore})}<fieldset class="settings-progress-mode"><legend>${m('进度显示')}</legend><div class="settings-segments">${progressChoices}</div></fieldset></section><section class="settings-group" aria-labelledby="model-settings-title"><h3 id="model-settings-title">${m('模型连接')}</h3><div data-model-connections aria-live="polite">${m('正在载入…')}</div></section></div>`;
}

export function renderModelConnections(capabilities, exploration) {
  const models=Object.entries(capabilities).map(([key,cap]) => `<div class="settings-connection"><div class="settings-model"><span class="settings-model-label">${m(labels[key] || key)}</span><span class="settings-model-name">${cap.model?escape(cap.model):m('尚未指定模型')}</span></div><span class="badge ${cap.configured?'ready':''}">${m(cap.configured?'已填写配置':'未配置')}</span></div>`).join('');
  if(!exploration)return models;
  const source=exploration.provider==='curated'?'公开课程目录':exploration.provider==='brave'?'Brave Search':'尚未配置来源';
  return models+`<div class="settings-connection"><div class="settings-model"><span class="settings-model-label">${m('资料搜索')}</span><span class="settings-model-name">${m(source)}</span></div><span class="badge ${exploration.available===true?'ready':''}">${m(exploration.available===true?'可用':'未配置')}</span></div>`;
}
