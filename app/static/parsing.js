import { m, a } from './i18n.js';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const parsingLabel = status => ({good:'已提取文字',needs_review:'解析需核对',unreadable:'未识别到文字',legacy_unverified:'暂无解析报告'}[status] || '解析需核对');
const warningLabels = {
  no_text_extracted:'本页未识别到文字，请对照原图。',
  sparse_or_suspicious_text_requires_review:'本页文字较少或包含异常字符，请核对是否遗漏。',
  low_ocr_confidence_requires_review:'本页 OCR 置信度偏低，请核对识别结果。',
  ocr_page_limit:'已达到本次 OCR 页数上限，其余页面请拆分后处理。',
  preview_page_limit_use_original_pdf:'已达到预览页数上限，请下载原文件查看。',
  document_time_limit:'解析时间已达到上限，请拆分文件后重试。',
  text_output_truncated:'文字输出超过本次限制，结果有截断。',
  visual_asset_storage_limit:'预览图超过本次保存限制，请查看原文件。',
  native_text_extraction_failed:'原生文字层提取失败，已尝试可用的识别方法。',
};
export function parsingDialog(report, documentId) {
  const id = esc(documentId);
  const actions = `${parserControls(report.options)}<div class="parsing-actions"><a class="button secondary" href="/api/documents/${id}/source">${m('下载原文件')}</a><button class="secondary" data-action="reparse" data-id="${id}">${m('重新解析（需重建索引）')}</button>${report.options?.images?`<button class="secondary" data-action="retry-figure" data-id="${id}">${m('重试或更新图片索引')}</button>`:''}</div>`;
  if (!report.available) return `<p>${m('暂无解析报告')}</p>${actions}`;
  const metrics = report.metrics;
  return `<p class="parsing-summary">${m('有文字的页面：{readable} / {total}',{readable:metrics.pages_with_text,total:metrics.page_count})} · ${m(parsingLabel(report.status))}</p>${actions}<label>${m('选择页面')}<select id="parsing-page">${report.pages.map(p=>`<option value="${p.number}">${p.number} · ${esc(p.method)}</option>`).join('')}</select></label><div id="parsing-detail"></div>`;
}
export function parsingPage(report, number) {
  const page = report.pages.find(p=>p.number === Number(number)) || report.pages[0];
  if (!page) return '';
  const assets = report.assets.filter(asset=>asset.page === page.number);
  const warnings = [...new Set([...report.warnings,...page.warnings])].filter(w=>warningLabels[w] || w.startsWith('ocr_failed') || w.includes('dependency_missing'));
  const warningText = code => warningLabels[code] || (code.includes('dependency_missing')?'本机缺少所需解析组件，请检查配置。':'本页识别失败，请核对原文件或重试。');
  return `<div class="parsing-page-info"><strong>${m('第 {n} 页',{n:page.number})}</strong><span>${m(parsingLabel(page.status))} · ${esc(page.method)}</span></div>${warnings.length?`<ul class="parsing-warnings">${warnings.map(w=>`<li>${m(warningText(w))}</li>`).join('')}</ul>`:''}<div class="parsing-comparison"><section><h3>${m('提取的文字')}</h3>${page.text?`<pre class="parsed-text">${esc(page.text)}</pre>`:`<p>${m('本页未识别到文字，请对照原图。')}</p>`}</section><section><h3>${m('原页与配图')}</h3>${assets.length?assets.map(asset=>`<figure class="parsed-visual"><a href="${esc(asset.url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(asset.url)}" ${a('原文图像，点击放大','alt')}></a><figcaption>${m(asset.kind==='figure'?'Docling 裁切配图':asset.kind==='embedded_image'?'PDF 内嵌图像（可能只是图表的一部分）':'原页面（保留配图与版面）')}</figcaption>${asset.original_caption?`<p class="muted">${m('提取的原文图注')}：${esc(asset.original_caption)}</p>`:''}${report.figures?.find(f=>f.asset_id===asset.id)?figureCard(report.figures.find(f=>f.asset_id===asset.id),report.document_id):''}</figure>`).join(''):`<p>${m('本页没有保存预览图，可下载原文件核对。')}</p>`}</section></div>`;
}

export function parserControls(options={}) {
  return `<fieldset class="parser-options"><legend>${m('解析选项')}</legend><label>${m('MinerU 解析档位')}<select name="tier"><option value="standard" ${options.tier!=='advanced'?'selected':''}>Standard</option><option value="advanced" ${options.tier==='advanced'?'selected':''}>Advanced</option></select></label><label class="figure-toggle"><input type="checkbox" name="images" value="true" ${options.images?'checked':''}>${m('理解配图并加入检索')}</label></fieldset>`;
}

export function figureCard(figure, documentId) {
  const annotation=figure.annotation;
  return `<div class="figure-description"><p>${m('图片处理状态')}：${m(({pending:'待处理',running:'处理中',ready:'可检索',failed:'处理失败'})[figure.status]||'待处理')}</p>${annotation?`<p><strong>${m(annotation.origin==='user'?'人工编辑的图片说明':'AI 图片说明（需核对）')}</strong></p><p>${esc(annotation.data.description)}</p><p class="muted">${esc([...annotation.data.keywords_zh,...annotation.data.keywords_en].join(' · '))}</p>${annotation.data.uncertainties?.length?`<p>${m('不确定信息')}：${esc(annotation.data.uncertainties.join('；'))}</p>`:''}`:''}${figure.error?`<p>${m(figure.error)}</p>`:''}<button class="secondary" data-action="edit-figure" data-id="${esc(documentId)}" data-asset="${esc(figure.asset_id)}">${m('编辑图片说明')}</button> <button class="secondary" data-action="retry-figure" data-id="${esc(documentId)}" data-asset="${esc(figure.asset_id)}">${m('重试或更新图片索引')}</button></div>`;
}
