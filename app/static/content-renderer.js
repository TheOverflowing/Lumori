import { m } from './i18n.js';
import { createCitationRenderer } from './citations.js';
import { renderQuestionDifficulty } from './difficulty.js';
import { renderExternalSource } from './exploration.js';
import { renderReadableText } from './readable-text.js';
import { questionSlot } from './question-revision.js';
import { contentAnchor, displayedQuestionNumber } from './content-navigation.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

// Friendly citation labels are a view of the saved evidence. Never rewrite the
// asset: editing, exports and version-bound review retain their original IDs.
export function renderAsset(asset, showCitations = true, difficultyAssessment = null, sources = [], {editableQuestions = false, originalQuestionNumbers = false} = {}) {
  const citations = createCitationRenderer(sources, asset, {enabled:showCitations});
  const text = value => citations.text(value);
  const prose = value => renderReadableText(value,text);
  const number = (question,index) => displayedQuestionNumber(question,index,{originalQuestionNumbers});
  return `<h2>${text(asset.title)}</h2>${asset.learning_objectives?.length ? `<p class="objective">${m('学习目标：')}${asset.learning_objectives.map(text).join('；')}</p>` : ''}${(asset.sections || []).map((section,index) => `<h3 data-content-anchor="${contentAnchor('section',index)}" tabindex="-1">${text(section.heading)}</h3><div class="prose readable-text">${prose(section.text)}</div>${citations.references(section.citation_ids, [section.heading, section.text])}`).join('')}${(asset.questions || []).map((question, index) => `<section class="question" data-content-anchor="${contentAnchor('question',index)}" tabindex="-1"><div class="question-prompt"><h3 class="question-number"><span aria-hidden="true">${number(question,index)}.</span><span class="sr-only">${m('第 {n} 题',{n:number(question,index)})}</span></h3><div class="question-stem readable-text">${prose(question.stem)}</div></div>${editableQuestions ? `<div class="question-edit-action"><button type="button" class="link" data-action="revise-question" data-slot-id="${esc(questionSlot(question,index))}">${m('修改此题')}<span class="sr-only"> · ${m('第 {n} 题',{n:number(question,index)})}</span></button></div>` : ''}${showCitations ? renderQuestionDifficulty(question, index, difficultyAssessment) : ''}${question.options?.length ? `<ol class="question-options" type="A">${question.options.map(option => `<li class="readable-text">${prose(option)}</li>`).join('')}</ol>` : ''}${citations.references(question.citation_ids, [question.stem, ...(question.options || [])])}${question.answer !== undefined ? `<details><summary>${m('参考答案与解析')}</summary><div class="answer readable-text"><div class="answer-key">${prose(question.answer)}</div>${prose(question.explanation)}</div></details>` : ''}</section>`).join('')}`;
}

export function renderContentSources(sources = [], asset = {}) {
  const citations = createCitationRenderer(sources, asset);
  return sources.map(source => {
    const figure = source.metadata?.source_kind === 'figure' && source.metadata?.source_asset_ids?.[0];
    return `<details class="source-reference"><summary><span class="source-reference-number" aria-hidden="true">${citations.number(source.id)}</span><span class="source-reference-heading"><span class="sr-only">${m('来源 {n}', {n:citations.number(source.id)})} · </span><span class="source-reference-name">${esc(source.document_name || '') || m('未命名资料')}</span>${source.page ? `<span class="source-reference-page">${m('第 {n} 页', {n:source.page})}</span>` : ''}</span></summary><div class="source-chunk">${renderExternalSource(source)}${figure ? `<figure class="retrieved-figure"><img loading="lazy" src="/api/documents/${encodeURIComponent(source.document_id)}/assets/${encodeURIComponent(figure)}" alt="${esc(source.document_name || '')}"><figcaption>${m(source.metadata.annotation?.origin === 'user' ? '人工编辑的图片说明' : 'AI 图片说明（需核对）')}</figcaption></figure>` : ''}<p>${esc(source.text)}</p>${source.document_id ? `<button class="link" data-action="parsing" data-id="${esc(source.document_id)}" data-page="${esc(source.page)}">${m('查看原页')}</button>` : ''}</div></details>`;
  }).join('');
}
