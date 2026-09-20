import test from 'node:test';
import assert from 'node:assert/strict';
import { createGenerationFlow, renderPreparationDialog } from '../app/static/clarification.js';

globalThis.document = {documentElement:{lang:'zh-CN'}};
const id = 'a'.repeat(32);
const prep = (status='ready', extras={}) => ({preparation_id:id,status,question:'Which version do you mean?',options:[],expires_at:1900000000,...extras});
const payload = () => ({topic:'Explain the fast one',course_id:'course-a',count:7,material:'quiz',language:'en',
  question_type:'mixed',difficulty:'hard',difficulty_distribution:{easy:0,medium:2,hard:5},
  learner_profile:'CS undergraduates',document_ids:['doc-a','doc-b'],request_key:'stable-request-key'});
function deferred() { let resolve,reject; const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject}; }
function transport(responses) {
  const calls=[];
  return {calls,request:async(path,method,body)=>{
    calls.push({path,method,body:structuredClone(body)});
    const next=responses.shift();
    if(next instanceof Error)throw next;
    return next instanceof Function?next():await next;
  }};
}

test('ready uses an immutable full snapshot and keeps original topic, key, and nested controls',async()=>{
  const wait=deferred(), io=transport([wait.promise,{job_id:'job-a'}]), created=[];
  const input=payload(), saved=structuredClone(input);
  const flow=createGenerationFlow({request:io.request,onCreated:(job,snapshot)=>created.push({job,snapshot})});
  const pending=flow.start(input);
  input.topic='changed';input.document_ids.push('other');input.difficulty_distribution.hard=1;
  wait.resolve(prep());await pending;
  assert.deepEqual(io.calls[0],{path:'/generations/prepare',method:'POST',body:saved});
  assert.deepEqual(io.calls[1].body,{...saved,preparation_id:id});
  assert.equal(io.calls[1].path,'/generations');
  assert.deepEqual(created,[{job:{job_id:'job-a'},snapshot:saved}]);
});

test('clarification waits for actual edited answer and submits raw answer without rewriting original input',async()=>{
  const io=transport([prep('clarification_required',{options:['Ascending','Descending']}),{job_id:'job-a'}]);
  const flow=createGenerationFlow({request:io.request});await flow.start(payload());
  assert.equal(io.calls.length,1);assert.equal(flow.state.phase,'clarification_required');
  await flow.answer('  Descending, with duplicates.\n');
  assert.deepEqual(io.calls[1].body,{...payload(),preparation_id:id,clarification_action:'answer',clarification_answer:'  Descending, with duplicates.\n'});
  assert.equal(flow.state.phase,'complete');
});

test('unknown and unavailable require explicit distinct actions; neither creates a guessed answer',async()=>{
  for(const [status,action] of [['clarification_required','unknown'],['unavailable','skip']]){
    const io=transport([prep(status),{job_id:'job-a'}]),flow=createGenerationFlow({request:io.request});
    await flow.start(payload());assert.equal(io.calls.length,1);
    await flow[action]();
    assert.equal(io.calls[1].body.clarification_action,action);
    assert.equal(io.calls[1].body.clarification_answer,'');
    assert.equal(io.calls[1].body.topic,payload().topic);
  }
});

test('turning the check off uses existing direct generation contract',async()=>{
  const io=transport([{job_id:'direct'}]);const flow=createGenerationFlow({request:io.request});
  await flow.start(payload(),{check:false});
  assert.deepEqual(io.calls,[{path:'/generations',method:'POST',body:payload()}]);
});

test('cancelled pending preparation cannot create a generation task',async()=>{
  const wait=deferred(),io=transport([wait.promise]),created=[];
  const flow=createGenerationFlow({request:io.request,onCreated:job=>created.push(job)});
  const pending=flow.start(payload());flow.cancel();wait.resolve(prep());await pending;
  assert.equal(io.calls.length,1);assert.deepEqual(created,[]);assert.equal(flow.state.phase,'cancelled');
});

test('late cancelled check cannot replace a newer question',async()=>{
  const first=deferred(),second=deferred(),io=transport([first.promise,second.promise]);
  const flow=createGenerationFlow({request:io.request});
  const old=flow.start(payload());flow.cancel();
  const newer=flow.start({...payload(),topic:'another objective'});
  second.resolve(prep('clarification_required',{question:'New question?'}));await newer;
  first.resolve(prep());await old;
  assert.equal(flow.state.question,'New question?');assert.equal(io.calls.length,2);
});

test('route or account becoming stale suppresses further generation and callbacks',async()=>{
  let active=true;const wait=deferred(),io=transport([wait.promise]),created=[];
  const flow=createGenerationFlow({request:io.request,isCurrent:()=>active,onCreated:x=>created.push(x)});
  const pending=flow.start(payload());active=false;wait.resolve(prep());await pending;
  assert.equal(io.calls.length,1);assert.deepEqual(created,[]);
});

test('duplicate check and answer clicks cannot enqueue duplicate requests',async()=>{
  const checking=deferred(),generating=deferred(),io=transport([checking.promise,generating.promise]);
  const flow=createGenerationFlow({request:io.request});
  const first=flow.start(payload());await flow.start(payload());assert.equal(io.calls.length,1);
  checking.resolve(prep('clarification_required'));await first;
  const answer=flow.answer('Ascending');await flow.answer('Ascending');await flow.unknown();
  assert.equal(io.calls.length,2);generating.resolve({job_id:'one'});await answer;
});

test('ready and direct transport failures retry exact same request without another preparation',async()=>{
  for(const check of [true,false]){
    const responses=check?[prep(),new Error('network lost'),{job_id:'existing'}]:[new Error('network lost'),{job_id:'existing'}];
    const io=transport(responses),flow=createGenerationFlow({request:io.request});
    await assert.rejects(flow.start(payload(),{check}),/network lost/);
    assert.equal(flow.state.phase,'retry_generation');
    await flow.retry();
    const posted=io.calls.filter(x=>x.path==='/generations');
    assert.equal(posted.length,2);assert.deepEqual(posted[0],posted[1]);
    assert.equal(io.calls.filter(x=>x.path.endsWith('/prepare')).length,check?1:0);
  }
});

test('answer transport failure retains answer and key for an explicit retry',async()=>{
  const io=transport([prep('clarification_required'),new Error('network lost'),{job_id:'existing'}]);
  const flow=createGenerationFlow({request:io.request});await flow.start(payload());
  await assert.rejects(flow.answer('Ascending'),/network lost/);
  assert.equal(flow.state.phase,'clarification_required');assert.equal(flow.state.answer,'Ascending');
  await flow.answer(flow.state.answer);assert.deepEqual(io.calls[1],io.calls[2]);
});

test('expired, changed, used, or missing preparation requires explicit restart and never auto-retries',async()=>{
  for(const code of ['preparation_expired','preparation_changed','preparation_used','preparation_missing','preparation_pending','preparation_incomplete']){
    const error=Object.assign(new Error('conflict'),{code});
    const io=transport([prep(),error]),flow=createGenerationFlow({request:io.request});
    await assert.rejects(flow.start(payload()),e=>e.code===code&&e.message.includes('重新点击'));
    assert.equal(flow.state.phase,'expired');await flow.retry();assert.equal(io.calls.length,2);
  }
});

test('empty or oversized answers cannot submit; choice strings remain editable text',async()=>{
  const io=transport([prep('clarification_required')]),flow=createGenerationFlow({request:io.request});
  await flow.start(payload());
  for(const answer of ['',' \n ','x'.repeat(2001)])await assert.rejects(flow.answer(answer),/2000/);
  assert.equal(io.calls.length,1);assert.equal(flow.state.phase,'clarification_required');
});

test('invalid preparation responses cannot trigger expensive generation',async()=>{
  for(const result of [prep('ready',{preparation_id:'unsafe'}),prep('other'),prep('clarification_required',{question:''}),prep('ready',{options:['1','2','3','4']})]){
    const io=transport([result]),flow=createGenerationFlow({request:io.request});
    await assert.rejects(flow.start(payload()),/需求检查返回异常/);assert.equal(io.calls.length,1);
  }
});

test('dialog translates interface chrome while preserving and escaping model text and user answer',()=>{
  const state={phase:'clarification_required',originalTopic:'<script>topic</script>',language:'zh',question:'中文问题 <img onerror=bad>',options:['<b>choice</b>'],answer:'</textarea>edited'};
  try{
    document.documentElement.lang='en';const html=renderPreparationDialog(state);
    assert.match(html,/Original learning objective/);assert.match(html,/Your answer/);
    assert.match(html,/Continue generation/);assert.match(html,/Not sure — use original request/);
    assert.match(html,/lang="zh-CN"/);assert.match(html,/中文问题 &lt;img onerror=bad&gt;/);
    assert.match(html,/&lt;script&gt;topic&lt;\/script&gt;/);assert.match(html,/&lt;\/textarea&gt;edited/);
    assert.match(html,/data-clarification-choice="0"/);assert.match(html,/maxlength="2000"/);
    assert.match(html,/<details class="clarification-original"><summary>/);
    assert.doesNotMatch(html,/clarification-hint|You can select a reply and edit it/);
    assert.doesNotMatch(html,/<script>|<img|<b>choice/);
    document.documentElement.lang='zh-CN';assert.match(renderPreparationDialog(state),/补充信息/);
  }finally{document.documentElement.lang='zh-CN';}
});

test('unavailable dialog offers explicit original continuation without an answer field',()=>{
  const html=renderPreparationDialog({phase:'unavailable',originalTopic:'Keep me',message:'internal provider details'});
  assert.match(html,/仍按原输入尝试/);assert.match(html,/需求检查未完成/);
  assert.doesNotMatch(html,/<textarea|internal provider details/);
});

test('service outage is distinct from unusable check output and never auto-submits generation',async()=>{
  for(const code of ['upstream_unavailable','authentication','payment_required','configuration','rate_limited','timeout','network','invalid_output','PRIVATE_PROVIDER_BODY']){
    const io=transport([prep('unavailable',{failure_code:code})]),flow=createGenerationFlow({request:io.request});
    await flow.start(payload());
    assert.equal(io.calls.length,1);
    assert.equal(flow.state.originalTopic,payload().topic);
    assert.equal(flow.state.failureCode,code==='PRIVATE_PROVIDER_BODY'?'unknown':code);
    try{
      document.documentElement.lang='en';
      const html=renderPreparationDialog(flow.state);
      assert.doesNotMatch(html.replace(/<[^>]*>/g,''),/PRIVATE_PROVIDER_BODY|[\u4e00-\u9fff]/);
      if(code==='upstream_unavailable')assert.match(html,/temporarily unavailable, so generation may also fail/);
      if(code==='invalid_output')assert.match(html,/unusable response/);
      else assert.match(html,/Try original request anyway/);
    }finally{document.documentElement.lang='zh-CN';}
    flow.cancel();assert.equal(io.calls.length,1);
  }
});
