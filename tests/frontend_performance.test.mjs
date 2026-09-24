import test from 'node:test';
import assert from 'node:assert/strict';
import { beginViewLoad, patchCollection } from '../app/static/view-updates.js';
import { mountInputControls } from '../app/static/input-controls.js';

test('fast navigation preserves the previous surface until data is ready, without a loading flash', context => {
  context.mock.timers.enable({apis:['setTimeout']});
  const root = {innerHTML:'old content', inert:false, setAttribute(){}, removeAttribute(){}};
  const loading = beginViewLoad(root, 'loading');
  assert.equal(root.inert, true);
  context.mock.timers.tick(80);
  assert.equal(root.innerHTML, 'old content');
  loading.finish();
  context.mock.timers.tick(200);
  assert.equal(root.innerHTML, 'old content');
  assert.equal(root.inert, false);
  assert.equal(loading.signal.aborted, false);
});

test('superseded navigation aborts its read and cannot install a late loading screen', context => {
  context.mock.timers.enable({apis:['setTimeout']});
  const root = {innerHTML:'old', inert:false, setAttribute(){}, removeAttribute(){}};
  const old = beginViewLoad(root, 'old loading');
  old.cancel();
  const latest = beginViewLoad(root, 'new loading');
  old.finish();
  assert.equal(root.inert, true);
  assert.equal(old.signal.aborted, true);
  context.mock.timers.tick(120);
  assert.equal(root.innerHTML, 'new loading');
  latest.finish();
  assert.equal(root.inert, false);
});

// Tiny structural DOM double: assertions concern node identity and ordering,
// not browser parsing, layout cost or real-device frame rate.
class Row {
  constructor(key = '', text = '', children = [], collection = false) {
    this.dataset = key ? {rowKey:key} : {};
    this.text = text; this.collection = collection; this.children = [];
    this.classList = {contains:name => name === 'collection' && this.collection};
    children.forEach(child => this.insertBefore(child, null));
  }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() { return this.parent?.children[this.parent.children.indexOf(this)+1] || null; }
  insertBefore(node, reference) {
    node.remove();
    const index = reference ? this.children.indexOf(reference) : this.children.length;
    assert.ok(index >= 0);
    this.children.splice(index,0,node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this),1); this.parent = null; } }
  replaceWith(node) { const parent=this.parent; parent.insertBefore(node,this); this.remove(); }
  replaceChildren(fragment) { this.children.slice().forEach(child=>child.remove()); fragment.children.slice().forEach(child=>this.insertBefore(child,null)); }
  json() { return {key:this.dataset.rowKey || '',text:this.text,collection:this.collection,children:this.children.map(child=>child.json())}; }
  isEqualNode(node) { return JSON.stringify(this.json()) === JSON.stringify(node.json()); }
  get innerHTML() { return JSON.stringify(this.children.map(child=>child.json())); }
}
const row = (key,text=key) => new Row(key,text);
const decode = item => new Row(item.key,item.text,item.children.map(decode),item.collection);
test('a single changed row preserves all unaffected row nodes; reorder and removal stay correct', context => {
  const previousDocument=globalThis.document;
  globalThis.document={createElement:()=>({set innerHTML(value){this.content=new Row('', '', JSON.parse(value).map(decode));}})};
  context.after(()=>{globalThis.document=previousDocument;});
  const original=[row('a'),row('b'),row('c')], wrapper=new Row('', '', original,true), root=new Row('', '',[wrapper]);
  const markup=rows=>new Row('', '',[new Row('', '',rows,true)]).innerHTML;
  patchCollection(root,markup([row('a'),row('b','updated'),row('c')]));
  assert.equal(root.firstElementChild,wrapper);
  assert.equal(wrapper.children[0],original[0]);
  assert.notEqual(wrapper.children[1],original[1]);
  assert.equal(wrapper.children[2],original[2]);
  patchCollection(root,markup([row('c'),row('d'),row('a')]));
  assert.deepEqual(wrapper.children.map(node=>node.dataset.rowKey),['c','d','a']);
  assert.equal(wrapper.children[0],original[2]);
  assert.equal(wrapper.children[2],original[0]);
  const kept=wrapper.children.slice();
  patchCollection(root,markup([row('c'),row('d'),row('a')]));
  assert.deepEqual(wrapper.children,kept);
});

test('editing one of 80 textareas measures only that field and batches layout reads', context => {
  let nextFrame=0, resize;
  const frames=new Map(), operations=[];
  const previous={requestAnimationFrame:globalThis.requestAnimationFrame,cancelAnimationFrame:globalThis.cancelAnimationFrame,
    ResizeObserver:globalThis.ResizeObserver,getComputedStyle:globalThis.getComputedStyle};
  Object.assign(globalThis, {
    requestAnimationFrame:callback=>{frames.set(++nextFrame,callback);return nextFrame;},
    cancelAnimationFrame:id=>frames.delete(id),
    ResizeObserver:class {constructor(callback){resize=callback;}observe(){}unobserve(){}disconnect(){}},
    getComputedStyle:()=>({borderTopWidth:'1px',borderBottomWidth:'1px',paddingTop:'5px',
      paddingBottom:'5px',boxSizing:'border-box',maxHeight:'400px',minHeight:'40px'}),
  });
  context.after(()=>Object.assign(globalThis,previous));
  const inputs=Array.from({length:80},(_,index)=>({value:`field ${index}`,dataset:{},
    style:{set height(value){operations.push(`write:${index}`);},overflowY:''},
    getClientRects:()=>[{}],get scrollHeight(){operations.push(`read:${index}`);return 80;}}));
  const root=new EventTarget();
  root.querySelectorAll=selector=>selector==='textarea'?inputs:[];
  root.contains=input=>inputs.includes(input);
  const controls=mountInputControls(root);
  assert.equal(operations.filter(x=>x.startsWith('read:')).length,80);
  assert.ok(operations.slice(0,80).every(x=>x.startsWith('write:')));
  assert.ok(operations.slice(80,160).every(x=>x.startsWith('read:')));
  operations.length=0;
  const flush=()=>{for(const [id,callback] of [...frames]){frames.delete(id);callback();}};
  inputs[0].value='changed';root.dispatchEvent(new Event('input'));flush();
  assert.deepEqual(operations.filter(x=>x.startsWith('read:')),['read:0']);
  operations.length=0;
  root.dispatchEvent(new Event('click'));flush();
  assert.equal(operations.length,0);
  resize([{target:inputs[0],contentRect:{width:400}}]);flush();
  assert.equal(operations.length,0,'initial ResizeObserver delivery does not repeat autosizing');
  resize([{target:inputs[0],contentRect:{width:200}}]);flush();
  assert.deepEqual(operations.filter(x=>x.startsWith('read:')),['read:0']);
  controls.destroy();
});
