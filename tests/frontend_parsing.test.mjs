import test from 'node:test';
import assert from 'node:assert/strict';
import {parsingDialog, parsingPage} from '../app/static/parsing.js';

const visibleText = html => html.replace(/<[^>]*>/g, '');
const report = () => ({
  available:true, status:'needs_review', warnings:[],
  metrics:{pages_with_text:1,page_count:2,recognition_accuracy:null},
  pages:[
    {number:1,text:'二分查找 Binary search',method:'rapidocr_v1',status:'good',warnings:[],
      metrics:{ocr_mean_word_confidence:99.2,ocr_confidence_is_accuracy:false}},
    {number:2,text:'',method:'tesseract_v1',status:'unreadable',warnings:['no_text_extracted','ocr_page_limit']},
  ],
  assets:[
    {page:1,kind:'page_preview',url:'/api/documents/doc-a/visuals/page-a'},
    {page:1,kind:'embedded_image',url:'/api/documents/doc-a/visuals/figure-a'},
    {page:2,kind:'source_image',url:'/api/documents/doc-a/visuals/page-b'},
  ],
});

function inLanguage(language, callback) {
  const previous = globalThis.document;
  globalThis.document = {documentElement:{lang:language}};
  try { return callback(); }
  finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
}

test('Chinese and English reports show text coverage without claiming recognition accuracy', () => {
  inLanguage('zh-CN', () => {
    const html = parsingDialog(report(), 'doc-a');
    assert.match(visibleText(html), /有文字的页面：1 \/ 2/);
    assert.match(visibleText(html), /重新解析（需重建索引）/);
  });
  inLanguage('en', () => {
    const html = parsingDialog(report(), 'doc-a');
    assert.match(visibleText(html), /Pages with text: 1 \/ 2/);
    assert.doesNotMatch(visibleText(html), /页面|重新解析|选择页面/);
    assert.doesNotMatch(visibleText(html + parsingPage(report(), 1)), /99\.2|100%|accuracy:\s*\d/i);
  });
});

test('switching interface language preserves authored bilingual source text', () => {
  for (const language of ['zh-CN', 'en']) inLanguage(language, () => {
    const html = parsingPage(report(), 1);
    assert.match(html, /<pre class="parsed-text">二分查找 Binary search<\/pre>/);
    if (language === 'en') {
      assert.match(visibleText(html), /Extracted text/);
      assert.match(visibleText(html), /Original page, including figures and layout/);
    } else assert.match(visibleText(html), /提取的文字/);
  });
});

test('OCR text and parser method cannot inject executable HTML', () => inLanguage('en', () => {
  const fixture = report();
  fixture.pages[0].text = '<img src=x onerror="alert(1)"> & <script>throw 1</script>';
  fixture.pages[0].method = '<svg onload="alert(2)">';
  const html = parsingDialog(fixture, 'doc-a') + parsingPage(fixture, 1);
  assert.doesNotMatch(html, /<script>|<svg|<img src=x/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(html, /&lt;svg onload=&quot;alert\(2\)&quot;&gt;/);
  assert.match(html, /&amp; &lt;script&gt;throw 1&lt;\/script&gt;/);
}));

test('document and visual identifiers remain inside quoted attributes', () => inLanguage('en', () => {
  const fixture = report();
  fixture.assets[0].url += '?label=" onerror="bad()';
  const html = parsingDialog(fixture, 'doc" onmouseover="bad()') + parsingPage(fixture, 1);
  assert.doesNotMatch(html, /href="[^"]*" onmouseover=|src="[^"]*" onerror=/);
  assert.match(html, /doc&amp;quot;|doc&quot;/);
  assert.match(html, /label=&quot; onerror=&quot;bad\(\)/);
}));

test('older documents keep the download and explicit reparse entry without pretending a report exists', () => {
  inLanguage('en', () => {
    const html = parsingDialog({available:false}, 'legacy-doc');
    assert.match(visibleText(html), /No parsing report yet/);
    assert.match(visibleText(html), /reindex required/);
    assert.match(html, /href="\/api\/documents\/legacy-doc\/source"/);
    assert.match(html, /data-action="reparse" data-id="legacy-doc"/);
    assert.doesNotMatch(html, /id="parsing-page"|Pages with text:/);
  });
  inLanguage('zh-CN', () => assert.match(visibleText(parsingDialog({available:false}, 'legacy-doc')), /暂无解析报告/));
});

test('changing the selected page shows only that page text and visual assets', () => inLanguage('en', () => {
  const fixture = report();
  const first = parsingPage(fixture, '1');
  assert.match(first, /\/visuals\/page-a/);
  assert.match(first, /\/visuals\/figure-a/);
  assert.doesNotMatch(first, /\/visuals\/page-b/);
  assert.match(first, /target="_blank" rel="noopener"/);
  const second = parsingPage(fixture, 2);
  assert.match(second, /\/visuals\/page-b/);
  assert.doesNotMatch(second, /\/visuals\/page-a|\/visuals\/figure-a|二分查找/);
  assert.match(visibleText(second), /No text was recognized on this page/);
  assert.match(visibleText(second), /OCR page limit/);
}));

test('missing components and truncated extraction get readable localized warnings', () => {
  const fixture = report();
  fixture.warnings = ['ocr_dependency_missing:rapidocr', 'text_output_truncated'];
  fixture.pages[0].warnings = ['text_output_truncated', 'low_ocr_confidence_requires_review'];
  inLanguage('en', () => {
    const text = visibleText(parsingPage(fixture, 1));
    assert.match(text, /local parsing component is missing/);
    assert.match(text, /truncated/);
    assert.match(text, /OCR confidence is low/);
    assert.doesNotMatch(text, /ocr_dependency_missing:|text_output_truncated/);
  });
  inLanguage('zh-CN', () => assert.match(visibleText(parsingPage(fixture, 1)), /本机缺少所需解析组件/));
});

test('unavailable visual previews retain a useful original-file fallback', () => inLanguage('en', () => {
  const fixture = report();
  fixture.assets = [];
  const html = parsingPage(fixture, 1);
  assert.match(visibleText(html), /No preview was saved for this page/);
  assert.match(visibleText(html), /original file/);
  assert.doesNotMatch(html, /<img/);
  assert.equal(parsingPage({...fixture,pages:[]}, 1), '');
}));

test('new parser options are opt-in and localized',async()=>{
  const {parserControls}=await import('../app/static/parsing.js');
  inLanguage('en',()=>{
    const html=parserControls();
    assert.match(html,/Standard/);assert.match(html,/Advanced/);
    assert.match(html,/type="checkbox" name="images" value="true" >/);
    assert.doesNotMatch(visibleText(html),/[\u4e00-\u9fff]/);
    assert.match(visibleText(html),/Understand figures and include them in retrieval/);
    assert.match(parserControls({tier:'advanced',images:true}),/value="advanced" selected/);
  });
});
test('figure annotations escape model text and distinguish edited provenance',async()=>{
  const {figureCard}=await import('../app/static/parsing.js');
  inLanguage('en',()=>{
    const html=figureCard({asset_id:'a',status:'ready',annotation:{origin:'user',data:{description:'<script>bad()</script>',keywords_zh:[],keywords_en:['tree'],uncertainties:[]}}},'d');
    assert.match(html,/Edited figure description/);assert.match(html,/&lt;script&gt;/);
    assert.doesNotMatch(html,/<script>/);
  });
});
