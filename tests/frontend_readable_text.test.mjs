import test from 'node:test';
import assert from 'node:assert/strict';
import { readableBlocks } from '../app/static/readable-text.js';
import { renderAsset } from '../app/static/content-renderer.js';

globalThis.document = {documentElement:{lang:'en'}};

test('inline numbered premises become distinct items without losing their text', () => {
  const value = 'Which statement best describes this situation? (1) The agent observes its current room. (2) It can infer location from earlier observations. (3) It balances exploration and exploitation.';
  const blocks = readableBlocks(value);
  assert.equal(blocks.length,2);
  assert.equal(blocks[0].text,'Which statement best describes this situation?');
  assert.deepEqual(blocks[1].items.map(item => item.marker),['(1)','(2)','(3)']);
  assert.equal(blocks.flatMap(block => block.items ? block.items.map(item => `${item.marker} ${item.text}`) : block.text).join(' '),value);
});

test('claims and a final question have separate blocks', () => {
  const value = 'The engineer makes these claims. Claim 1: The agent receives sensor readings. Claim 2: The agent always knows the optimal action. Which claim is correct?';
  const blocks = readableBlocks(value);
  assert.equal(blocks.length,3);
  assert.deepEqual(blocks[1].items,[{marker:'Claim 1:',text:'The agent receives sensor readings.'},{marker:'Claim 2:',text:'The agent always knows the optimal action.'}]);
  assert.equal(blocks[2].text,'Which claim is correct?');
});

test('Chinese numbered conditions and the final question remain readable', () => {
  const blocks = readableBlocks('机器人满足以下条件：（1）只能观察当前房间的状态。（2）可以参考之前获得的观察。请判断哪种说法正确？');
  assert.equal(blocks[1].items.length,2);
  assert.equal(blocks[2].text,'请判断哪种说法正确？');
  assert.equal(readableBlocks('给定以下条件：条件1：只能观察当前房间。 条件2：每次行动后收到奖励。')[1].items.length,2);
});

test('author newlines, blank paragraphs and indented code are retained', () => {
  const value = 'Background.\r\nA second line.\r\n\r\nQuestion?';
  assert.deepEqual(readableBlocks(value),[{type:'paragraph',text:'Background.\nA second line.'},{type:'paragraph',text:'Question?'}]);
  const code = '```python\nfor i in range(2):\n    print(i)\n\n    print(i + 1)\n```';
  assert.deepEqual(readableBlocks(code),[{type:'paragraph',text:code}]);
  assert.deepEqual(readableBlocks('if ready:\n    send()\n\n    finish()'),[
    {type:'paragraph',text:'if ready:\n    send()'},
    {type:'paragraph',text:'    finish()'},
  ]);
});

test('equation references, inline code, math, URLs, decimals and discontinuous numbering are not inferred as lists', () => {
  for (const value of [
    'Use equation (1) to derive the solution and equation (2) to check the result.',
    'Review equations: (1) is the conservation law and (2) is the update rule.',
    'Given: `Claim 1: the first code string. Claim 2: the second code string.`',
    'Given: $ (1) first mathematical term (2) second mathematical term $',
    'Given: \\( (1) first mathematical term (2) second mathematical term \\)',
    'Given: https://example.org/Claim%201:first/path/Claim%202:second/path',
    'Values: 1.25 and 2.75 are decimal values.',
    'References: (1) and (2).',
    'Conditions: (1) first substantial condition here. (3) another substantial condition here.',
  ]) assert.deepEqual(readableBlocks(value),[{type:'paragraph',text:value}],value);
});

test('layout keeps citation controls safe and saved content unchanged', () => {
  const id = 'context_487e8909cb9087ab351d6ca46d06db51';
  const asset = {title:'Practice',questions:[{
    stem:`Consider these claims. Claim 1: A robot uses observations [${id}]. Claim 2: <img src=x onerror=alert(1)> is plain text. Which claim is supported?`,
    options:['First line\nSecond line','Another choice'],citation_ids:[id],answer:'A',explanation:'Reason one.\n\nReason two.',
  }]};
  const before = JSON.stringify(asset);
  const html = renderAsset(asset,true,null,[{id,document_name:'Notes',text:'Evidence.'}]);
  assert.match(html,/<ol class="material-statements"/);
  assert.match(html,/data-citation-id=/);
  assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html,/<img src=x/);
  assert.match(html,/<p>Reason one\.<\/p><p>Reason two\.<\/p>/);
  assert.equal(JSON.stringify(asset),before);
  const learning = structuredClone(asset);
  delete learning.questions[0].answer;
  delete learning.questions[0].explanation;
  const learnerHtml = renderAsset(learning,false);
  assert.doesNotMatch(learnerHtml,/data-citation-id=|Reason one|answer-key/);
});
