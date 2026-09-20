import test from 'node:test';
import assert from 'node:assert/strict';
import { renderAsset, renderContentSources } from '../app/static/content-renderer.js';

globalThis.document = {documentElement:{lang:'en'}};
const sourceId = 'context_487e8909cb9087ab351d6ca46d06db51';
const sources = [{id:sourceId, document_id:'document1', document_name:'Introduction to machine learning', page:3, text:'A model learns from examples.'}];
const asset = {
  title:'Learning from data', learning_objectives:['Understand models'],
  sections:[{heading:'An introduction',text:`Models learn from examples [${sourceId}].`,citation_ids:[sourceId]}],
  questions:[{stem:`What does a model learn from? [${sourceId}]`,options:['Examples','Nothing'],citation_ids:[sourceId],answer:'Examples',explanation:`The source describes learning from examples [${sourceId}].`}],
};

test('content rendering replaces display references without changing saved review data', () => {
  const before = JSON.stringify(asset);
  const html = renderAsset(asset,true,null,sources);
  assert.doesNotMatch(html,/\[context_/);
  assert.match(html,/data-citation-id=/);
  assert.equal(JSON.stringify(asset),before);
  const sidebar = renderContentSources(sources,asset);
  assert.match(sidebar,/source-reference-number[^>]*>1</);
  assert.match(sidebar,/Introduction to machine learning/);
  assert.doesNotMatch(sidebar,/context_/);
});

test('learning rendering cleans citation markers while keeping ordinary brackets and unrevealed answers private', () => {
  const learning = structuredClone(asset);
  delete learning.questions[0].answer;
  delete learning.questions[0].explanation;
  learning.sections[0].text += ' Use array[index] to access an element.';
  const html = renderAsset(learning,false);
  assert.doesNotMatch(html,/context_|data-citation-id|class="answer"/);
  assert.match(html,/array\[index\]/);
  assert.match(html,/Models learn from examples/);
});

test('source inspector escapes untrusted snapshot text and handles incomplete image metadata', () => {
  const html = renderContentSources([{...sources[0],document_name:'<img onerror=alert(1)>',text:'<script>alert(1)</script>',metadata:{source_kind:'figure'}}],asset);
  assert.doesNotMatch(html,/<script>|<img /);
  assert.match(html,/&lt;script&gt;/);
});

test('questions-only assessments omit the introduction while preserving optional answer disclosure and citations', () => {
  const questionsOnly = {...structuredClone(asset),sections:[],learning_objectives:[]};
  const html = renderAsset(questionsOnly,true,null,sources);
  assert.doesNotMatch(html,/class="objective"|class="prose"|An introduction/);
  assert.match(html,/What does a model learn from/);
  assert.match(html,/Answers &amp; explanations|Answers & explanations/);
  assert.match(html,/data-citation-id=/);
  assert.match(html,/The source describes learning from examples/);
});
