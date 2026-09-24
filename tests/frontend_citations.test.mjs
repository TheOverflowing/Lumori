import test from 'node:test';
import assert from 'node:assert/strict';
import { createCitationRenderer, renderCitationPreview, mountCitations } from '../app/static/citations.js';

globalThis.document = {documentElement:{lang:'zh-CN'}};
const first = 'context_487e8909cb9087ab351d6ca46d06db51';
const second = 'context_b30230851bad1603662d680ad1441f0f';
const unknown = 'context_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const sources = [
  {id:first,document_name:'Introduction to machine learning.pdf',page:3,text:'A model learns patterns from data.'},
  {id:second,document_name:'Lecture 2.pdf',page:9,text:'Training and evaluation use separate examples.'},
];
const asset = {sections:[{text:`Learn from examples [${first}].`,citation_ids:[first,second]}],questions:[]};

test('citation numbers use source order and remain stable across authored fields and duplicates', () => {
  const renderer = createCitationRenderer([...sources,sources[0]],asset);
  assert.equal(renderer.number(first),1);
  assert.equal(renderer.number(second),2);
  assert.match(renderer.text(`Evaluate [${second}]. Then train [${first}].`),/data-citation-id="context_b302[^>]+.*?aria-hidden="true">2/s);
  assert.match(renderer.text(`[${first}]`),/aria-hidden="true">1/);
  assert.equal((renderer.text(`[${first}, ${first}; ${second}]`).match(/data-citation-id=/g)||[]).length,2);
});

test('only genuine citation groups change; prose, array notation and code retain their text', () => {
  const renderer = createCitationRenderer(sources,asset);
  for (const text of ['array[0]', '[0, 1]', '[important note]', '[context_window]', '(context_window)', `Use [${first}, a sentence]`, `\`[${first}]\``, `\`\`[${first}]\`\``, `\`\`\`js\n[${first}]\n\`\`\``, `\`\`\`js\n[${first}]`]) {
    assert.equal(renderer.text(text),text);
  }
  assert.match(renderer.text(`\`[${first}]\` and [${second}]`),/data-citation-id=/);
  assert.equal((renderer.text(`\`[${first}]\` and [${second}]`).match(/data-citation-id=/g)||[]).length,1);
});

test('declared non-context IDs are recognized without treating arbitrary brackets as sources', () => {
  const renderer = createCitationRenderer([{id:'chunk-12',document_name:'Notes'}],{});
  assert.match(renderer.text('Example [chunk-12].'),/data-citation-id="chunk-12"/);
  assert.equal(renderer.text('Example [chunk-99].'),'Example [chunk-99].');
});

test('real generation parentheses and Chinese citation groups use the same numbering and deduplication', () => {
  const renderer = createCitationRenderer(sources,asset);
  for (const citation of [`(${first}, ${second})`,`（${first}；${second}）`]) {
    const html = renderer.text(`Examples ${citation}.`);
    assert.equal((html.match(/data-citation-id=/g)||[]).length,2);
    assert.equal(renderer.references([first,second],[citation]),'');
    assert.equal(createCitationRenderer(sources,asset,{enabled:false}).text(citation),'');
  }
  for (const prose of ['step (1)', 'value (2)', '（普通注释）', `(${first}, ordinary prose)`, `\`(${first})\``])
    assert.equal(renderer.text(prose),prose);
});

test('footer references are unique and omit sources already cited anywhere in the relevant text', () => {
  const renderer = createCitationRenderer(sources,asset);
  assert.equal(renderer.references([first,second],[`Title [${first}]`,`Body [${second}]`]),'');
  const remaining = renderer.references([first,second,second],[`Title [${first}]`]);
  assert.equal((remaining.match(/data-citation-id=/g)||[]).length,1);
  assert.match(remaining,/data-citation-id="context_b302/);
  assert.equal(renderer.references([],[]),'');
});

test('missing declared sources receive stable unavailable markers; unknown context IDs never look verified', () => {
  const renderer = createCitationRenderer(sources,{sections:[{citation_ids:['context_missing']}]});
  assert.equal(renderer.number('context_missing'),3);
  assert.match(renderer.text('[context_missing]'),/citation-ref-unavailable/);
  assert.match(renderer.text('[context_missing]'),/aria-hidden="true">3/);
  assert.match(renderer.text(`[${unknown}]`),/aria-hidden="true">\?/);
  assert.match(renderer.text(`[${unknown}]`),/引用来源不可用/);
  const preview = renderCitationPreview('context_missing',sources,asset);
  assert.match(preview,/来源暂不可用/);
  assert.doesNotMatch(preview,/Introduction|href=|context_missing/);
});

test('the reading view removes references while preserving literal code and ordinary brackets', () => {
  const renderer = createCitationRenderer(sources,asset,{enabled:false});
  assert.equal(renderer.text(`Hello [${first}] world [${unknown}].`),'Hello  world .');
  assert.equal(renderer.text('Use [context_window] tokens.'),'Use [context_window] tokens.');
  assert.equal(renderer.text(`\`[${first}]\` and array[0]`),`\`[${first}]\` and array[0]`);
  assert.equal(renderer.references([first]),'');
});

test('authored markup and source metadata stay escaped, including malicious source IDs', () => {
  const id = 'chunk-"onclick="alert(1)';
  const renderer = createCitationRenderer([{id}],{});
  const html = renderer.text(`<img src=x onerror=alert(1)> [${id}]`);
  assert.doesNotMatch(html,/<img| onclick=/);
  assert.match(html,/&lt;img/);
  assert.match(html,/data-citation-id="chunk-&quot;onclick=&quot;alert\(1\)"/);
  const preview = renderCitationPreview(first,[{...sources[0],document_name:'<script>bad()</script>',text:'<img src=x onerror=bad()>'}],{});
  assert.doesNotMatch(preview,/<script>|<img src=x/);
  assert.match(preview,/&lt;script&gt;/);
});

test('source previews show human source details and prefer the safe reading URL', () => {
  const source = {...sources[0],external_source:{title:'A course chapter',reading_url:'https://example.edu/chapter.html',url:'https://raw.example.edu/chapter.md'}};
  const preview = renderCitationPreview(first,[source],{});
  assert.match(preview,/A course chapter/);
  assert.match(preview,/第 3 页/);
  assert.match(preview,/A model learns patterns/);
  assert.match(preview,/href="https:\/\/example.edu\/chapter.html"/);
  assert.match(preview,/rel="noopener noreferrer"/);
  assert.doesNotMatch(preview,/raw.example.edu|context_/);
});

test('unsafe URLs never become links and unsafe reading URLs fall back to a safe original URL', () => {
  for (const url of ['javascript:alert(1)','data:text/html,<script>bad()</script>','https://person:password@example.edu/chapter','file:///tmp/notes','//example.edu/notes']) {
    const preview = renderCitationPreview(first,[{...sources[0],external_source:{url}}],{});
    assert.doesNotMatch(preview,/href=/);
  }
  const preview = renderCitationPreview(first,[{...sources[0],metadata:{external_source:{reading_url:'javascript:bad()',url:'https://example.edu/notes'}}}],{});
  assert.match(preview,/href="https:\/\/example.edu\/notes"/);
});

test('figure sources use the authenticated document asset endpoint and encode identifiers', () => {
  const source = {...sources[0],document_id:'doc/"onerror=',metadata:{source_kind:'figure',source_asset_ids:['figure/1']}};
  const preview = renderCitationPreview(first,[source],{});
  assert.match(preview,/src="\/api\/documents\/doc%2F%22onerror%3D\/assets\/figure%2F1"/);
  assert.doesNotMatch(preview,/ onerror=/);
  assert.doesNotMatch(renderCitationPreview(first,[{...source,metadata:{source_kind:'figure'}}],{}),/<img/);
});

test('rendering does not mutate the saved asset, citations, or evidence objects', () => {
  const frozenSources = structuredClone(sources);
  const frozenAsset = structuredClone(asset);
  const snapshot = JSON.stringify({sources:frozenSources,asset:frozenAsset});
  const renderer = createCitationRenderer(frozenSources,frozenAsset);
  renderer.text(frozenAsset.sections[0].text);
  renderer.references([first,second]);
  renderCitationPreview(first,frozenSources,frozenAsset);
  assert.equal(JSON.stringify({sources:frozenSources,asset:frozenAsset}),snapshot);
});

test('numbered accessible labels and preview interface copy support language updates', () => {
  document.documentElement.lang = 'en';
  try {
    const html = createCitationRenderer(sources,asset).text(`[${first}]`);
    assert.match(html,/data-i18n="引用 \{n\}" data-i18n-values="\{&quot;n&quot;:1\}"/);
    assert.match(html,/Citation 1/);
    assert.match(renderCitationPreview(first,sources,asset),/Source 1/);
  } finally { document.documentElement.lang = 'zh-CN'; }
});

test('mount without a live root returns a harmless cleanup', () => {
  assert.doesNotThrow(mountCitations(null));
});

test('preview placement uses settled layout dimensions while the entry transform is scaled', () => {
  const listeners = new Map();
  const windowListeners = new Map(), frames = new Map();
  let frameId=0, anchorReads=0;
  const noop = () => {};
  const preview = {
    style:{}, dataset:{}, offsetWidth:380, offsetHeight:419,
    getBoundingClientRect:() => ({width:372.4,height:410.62}),
    setAttribute:noop, removeAttribute:noop, addEventListener:noop, removeEventListener:noop,
    remove:noop, contains:() => false,
  };
  const win = {innerWidth:573,innerHeight:734,
    addEventListener:(name,listener)=>windowListeners.set(name,listener),removeEventListener:noop,
    requestAnimationFrame:callback=>{frames.set(++frameId,callback);return frameId;},cancelAnimationFrame:id=>frames.delete(id)};
  const doc = {defaultView:win,createElement:() => preview,body:{append:noop},addEventListener:noop,removeEventListener:noop};
  const button = {
    dataset:{citationId:first}, isConnected:true,
    getBoundingClientRect:() => {anchorReads++;return {left:540,top:295,bottom:319,width:28};},
    setAttribute:noop,removeAttribute:noop,
  };
  const root = {ownerDocument:doc,contains:node => node === button,addEventListener:(name,listener) => listeners.set(name,listener),removeEventListener:noop};
  const cleanup = mountCitations(root,sources,asset);
  listeners.get('click')({target:{closest:() => button},preventDefault:noop,detail:1});
  assert.equal(preview.style.left,'181px');
  assert.equal(preview.style.top,'303px');
  assert.equal(573 - parseFloat(preview.style.left) - preview.offsetWidth,12);
  assert.equal(734 - parseFloat(preview.style.top) - preview.offsetHeight,12);
  for(let n=0;n<10;n++)windowListeners.get('scroll')();
  assert.equal(frames.size,1,'scroll bursts share one placement per frame');
  assert.equal(anchorReads,1);
  const callback=[...frames.values()][0];frames.clear();callback();
  assert.equal(anchorReads,2);
  windowListeners.get('scroll')();
  cleanup();
  assert.equal(frames.size,0,'disposing cancels queued layout work');
});
