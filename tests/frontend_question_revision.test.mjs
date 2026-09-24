import test from 'node:test';
import assert from 'node:assert/strict';
import { renderQuestionRevisionDialog, bindQuestionRevision, revisionPayload, questionChanges, renderQuestionComparison } from '../app/static/question-revision.js';
import { renderAsset } from '../app/static/content-renderer.js';
globalThis.document={documentElement:{lang:'zh-CN'}};
const question={slot_id:'q2',kind:'mcq',stem:'Which?',options:['a','b','c','d'],answer:'A',explanation:'Because.',difficulty:'easy'};
const content={id:'c1',version:4,asset:{title:'Quiz',questions:[question]},config:{}};

test('revision dialog shows current number/version and localizes all controls without authored translation',()=>{
  for(const language of ['zh-CN','en']) {
    document.documentElement.lang=language;
    const html=renderQuestionRevisionDialog(content,'q2');
    assert.match(html,/value="rewrite" checked/);
    assert.match(html,/value="explanation"/);
    assert.match(html,/name="instruction"[^>]*maxlength="2000" required/);
    assert.match(html,language==='en'?/Version 4/:/版本 4/);
    assert.match(html,language==='en'?/Keep current difficulty/:/保持当前难度/);
    assert.doesNotMatch(html,/<option[^>]*>\s*<span/);
  }
  document.documentElement.lang='zh-CN';
});

test('only review explicitly offers question editing; read-only and learner views never offer it',()=>{
  assert.doesNotMatch(renderAsset(content.asset),/revise-question/);
  assert.doesNotMatch(renderAsset(content.asset,false),/revise-question/);
  assert.match(renderAsset(content.asset,true,null,[],{editableQuestions:true}),/data-slot-id="q2"/);
  assert.match(renderAsset({...content.asset,questions:[{...question,slot_id:null}]},true,null,[],{editableQuestions:true}),/data-slot-id="q1"/);
  assert.match(renderAsset(content.asset,true,null,[],{originalQuestionNumbers:true}),/aria-hidden="true">2\./);
});

test('comparison exposes only changed fields, with answers additionally collapsed and safe text',()=>{
  const before=structuredClone(question);
  const after={...question,explanation:'<script>Example</script>',difficulty:'hard'};
  const changed={...content,asset:{...content.asset,questions:[after]},config:{question_revision:{slot_id:'q2',base_version:3,version:4,previous_question:before,instruction:'<img onerror=bad>'}}};
  const copy=structuredClone(changed), html=renderQuestionComparison(changed);
  assert.deepEqual(questionChanges(before,after).map(item=>item.field),['explanation','difficulty']);
  assert.match(html,/class="question-comparison"><summary>/);
  assert.match(html,/class="question-change-answers"><summary>/);
  assert.doesNotMatch(html,/Which\?|<script>|<img|\bopen(?:=|>)/);
  assert.match(html,/&lt;script&gt;Example/);
  assert.deepEqual(changed,copy);
  assert.equal(renderQuestionComparison(content),'');
});

function control(values={}) {return {dataset:{},disabled:false,hidden:false,value:'',...values};}
function harness(options={}) {
  let active=true;
  const mode=control({value:'rewrite'}), instruction=control({value:'Make the scenario clearer.'}), difficulty=control({value:'hard'}), label=control(), button=control(), error=control({hidden:true});
  const listeners={};
  const form={isConnected:true,valid:true,
    querySelector(selector){return {'[name="mode"]:checked':mode,'[name="instruction"]':instruction,'[name="difficulty"]':difficulty,'[data-revision-difficulty]':label,'button[type="submit"]':button,'[data-revision-error]':error}[selector];},
    querySelectorAll(){return [mode,instruction,difficulty,button];},
    reportValidity(){return this.valid;},setAttribute(){},removeAttribute(){},
    addEventListener(name,fn){listeners[name]=fn;},removeEventListener(name,fn){if(listeners[name]===fn)delete listeners[name];},
  };
  let keys=0;
  const dispose=bindQuestionRevision(form,{version:4,submit:async()=>({job_id:'new-job'}),onCreated(){},isActive:()=>active,makeKey:()=>`key${++keys}`,...options});
  return {form,mode,instruction,difficulty,label,button,error,dispose,deactivate(){active=false;},change(){listeners.change();},send(){return listeners.submit?.({preventDefault(){}});}};
}

test('explanation-only mode hides and omits difficulty, preserving unsent selection when switching back',()=>{
  const view=harness();
  view.mode.value='explanation';view.change();
  assert.equal(view.label.hidden,true);assert.equal(view.difficulty.disabled,true);
  assert.deepEqual(revisionPayload(view.form,4),{version:4,instruction:'Make the scenario clearer.',mode:'explanation'});
  view.mode.value='rewrite';view.change();
  assert.equal(view.difficulty.value,'hard');assert.equal(view.difficulty.disabled,false);
  assert.equal(revisionPayload(view.form,4).difficulty,'hard');view.dispose();
});

test('retries after uncertain replies reuse request key and only edited instructions get a new key',async()=>{
  const payloads=[];
  const view=harness({submit:async payload=>{payloads.push(payload);throw new Error('Connection interrupted');}});
  await view.send();await view.send();
  assert.equal(payloads[0].request_key,payloads[1].request_key);
  assert.equal(view.error.hidden,false);assert.equal(view.button.disabled,false);
  view.instruction.value='Use a new scenario.';await view.send();
  assert.notEqual(payloads[1].request_key,payloads[2].request_key);view.dispose();
});

test('pending revision blocks duplicate submissions and session/route changes ignore late replies',async()=>{
  let resolve, calls=0, opened=0;
  const view=harness({submit:()=>{calls++;return new Promise(done=>{resolve=done;});},onCreated(){opened++;}});
  const pending=view.send();await view.send();assert.equal(calls,1);assert.equal(view.button.disabled,true);
  view.deactivate();resolve({job_id:'new-job'});await pending;assert.equal(opened,0);view.dispose();
});

test('invalid instructions stay in the form and never submit',async()=>{
  let calls=0;
  const view=harness({submit:async()=>{calls++;}});view.instruction.value='   ';
  await view.send();assert.equal(calls,0);assert.equal(view.error.hidden,false);view.dispose();
});


test('revision comparison belongs only to its exact saved version, not later manual saves',()=>{
  const revised={...content,asset:{...content.asset,questions:[{...question,stem:'Changed stem'}]},config:{question_revision:{slot_id:'q2',base_version:3,version:4,previous_question:question}}};
  assert.match(renderQuestionComparison(revised),/question-comparison/);
  assert.equal(renderQuestionComparison({...revised,version:5}),'');
  for(const version of [undefined,null,'4']) assert.equal(renderQuestionComparison({...revised,config:{question_revision:{...revised.config.question_revision,version}}}),'');
});

test('revision instruction lengths match the API minimum and Unicode character counting',()=>{
  const view=harness();
  for(const value of ['改','x','😀','  a  ']) {
    view.instruction.value=value;
    assert.throws(()=>revisionPayload(view.form,4),/2–2000/);
  }
  view.instruction.value='改写';assert.equal(revisionPayload(view.form,4).instruction,'改写');
  view.instruction.value='😀'.repeat(2000);assert.equal([...revisionPayload(view.form,4).instruction].length,2000);
  view.instruction.value='改'.repeat(2001);assert.throws(()=>revisionPayload(view.form,4),/2–2000/);
  assert.match(renderQuestionRevisionDialog(content,'q2'),/minlength="2"/);
  view.dispose();
});
