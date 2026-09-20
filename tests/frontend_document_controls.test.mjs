import test from 'node:test';
import assert from 'node:assert/strict';
import {documentControls} from '../app/static/document-controls.js';
function language(lang,fn){const prev=globalThis.document;globalThis.document={documentElement:{lang}};try{fn();}finally{globalThis.document=prev;}}
test('switch exposes native keyboard semantics and saved checked state',()=>language('en',()=>{
  const enabled=documentControls({id:'d',name:'Lecture',enabled:true});
  assert.match(enabled,/role="switch"/);assert.match(enabled,/checked/);assert.match(enabled,/aria-label="Use for generation"/);
  const disabled=documentControls({id:'d',enabled:false});assert.doesNotMatch(disabled,/checked/);
}));
test('deleted document exposes restore, with escaped attributes and translated copy',()=>language('en',()=>{
  const deleted=documentControls({id:'d" onload="bad',deleted_at:'today'});
  assert.match(deleted,/Restore file/);assert.doesNotMatch(deleted,/role="switch"/);assert.doesNotMatch(deleted,/data-id="d" onload=/);
}));
