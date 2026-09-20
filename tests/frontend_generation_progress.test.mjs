import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { generationProgressView, renderGenerationProgress, mountGenerationProgress, watchGenerationProgress, durationText, eventMessage, progressSelection } from '../app/static/generation-progress.js';

globalThis.document = {documentElement:{lang:'zh-CN'}};
const stage = (id, status) => ({id, kind:id, status});
const job = (extra = {}) => ({id:'task1', kind:'generate', status:'running', timeline:{version:'job-progress-v1', recorded:true, stages:[stage('retrieval','completed'),stage('writing','active'),stage('saving','pending')],active_stage:'writing',activity:'reviewing',completed:1,total:3,unit:'questions'}, ...extra});

test('shows confirmed activity and checked question counts without a percent or time estimate', () => {
  const model = generationProgressView(job());
  assert.equal(model.title,'正在检查内容');
  assert.deepEqual(model.counter,{done:1,total:3});
  assert.equal(model.terminal,false);
  const html = renderGenerationProgress(job());
  assert.match(html,/已检查 1 \/ 3 题/);
  assert.doesNotMatch(html,/aria-valuenow|\d+%|ETA/);
  assert.match(html,/aria-current="step"/);
});

test('all questions checked still waits for an actual success before offering the result', () => {
  const running = job({result:{content_id:'premature'},timeline:{...job().timeline,completed:3,activity:'saving'}});
  assert.equal(generationProgressView(running).title,'正在保存结果');
  assert.equal(generationProgressView(running).contentId,null);
  assert.doesNotMatch(renderGenerationProgress(running),/data-action="open-content"/);
  const completed = {...running,status:'succeeded'};
  assert.equal(generationProgressView(completed).terminal,true);
  assert.match(renderGenerationProgress(completed),/data-action="open-content" data-id="premature"/);
});

test('legacy jobs show their real status without inventing stages or a denominator', () => {
  const old = job({timeline:{recorded:false,stages:[]},progress:{completed:1,total:3}});
  assert.deepEqual(generationProgressView(old).stages,[]);
  assert.equal(generationProgressView(old).counter,null);
  assert.doesNotMatch(renderGenerationProgress(old),/data-stage=/);
  assert.equal(generationProgressView({...old,status:'queued'}).title,'等待开始');
});

test('queued resumed jobs retain completed counts but never retain a stale running headline', () => {
  const queued = job({status:'queued'});
  assert.equal(generationProgressView(queued).title,'等待开始');
  assert.deepEqual(generationProgressView(queued).counter,{done:1,total:3});
});

test('exact current and historical text service outages show a recoverable cause in both progress layouts',()=>{
  for(const hint of ['请核对平台、模型及参数','模型服务暂不可用或繁忙，请稍后重试']){
    const failed=job({status:'failed',error:`text API 返回 HTTP 503；${hint}。本次未自动重试。`,progress:{resumable:true},
      timeline:{...job().timeline,stages:[stage('retrieval','completed'),stage('writing','failed'),stage('saving','pending')]}});
    assert.equal(generationProgressView(failed).title,'模型服务暂不可用');
    assert.match(generationProgressView(failed).message,/服务恢复后可继续生成/);
    for(const mode of ['simple','detailed']){
      const html=renderGenerationProgress(failed,{mode});
      assert.match(html,/模型服务暂不可用/);
      assert.match(html,/data-action="resume-generation"/);
      assert.match(html,/data-action="restore-generation"/);
    }
    const running={...failed,status:'running'};
    assert.equal(generationProgressView(running).providerFailure,null);
    assert.equal(generationProgressView(running).title,'正在检查内容');
    assert.equal(generationProgressView({...failed,error:failed.error+' arbitrary upstream text'}).title,'生成暂时中断');
    assert.equal(generationProgressView({...failed,kind:'speech'}).title,'生成暂时中断');
    try{
      document.documentElement.lang='en';
      assert.match(renderGenerationProgress(failed,{mode:'detailed'}),/Model service temporarily unavailable/);
    }finally{document.documentElement.lang='zh-CN';}
  }
});

test('only valid question counters render; lesson and media do not claim progress counts', () => {
  for (const fields of [{total:0},{completed:-1},{completed:4},{completed:1.2},{total:null},{unit:null},{unit:'images'}]) {
    assert.equal(generationProgressView(job({timeline:{...job().timeline,...fields}})).counter,null);
  }
});

test('failure, missing sources and resume use explicit terminal actions', () => {
  const failed = job({status:'failed',progress:{resumable:true}});
  assert.equal(generationProgressView(failed).title,'生成暂时中断');
  assert.match(renderGenerationProgress(failed),/data-action="resume-generation"/);
  assert.doesNotMatch(renderGenerationProgress({...failed,progress:{resumable:false}}),/data-action="resume-generation"/);
  const insufficient = {...failed,status:'insufficient_evidence'};
  assert.equal(generationProgressView(insufficient).resumable,false);
  assert.match(renderGenerationProgress(insufficient),/data-action="documents"/);
  assert.doesNotMatch(renderGenerationProgress(insufficient),/data-action="resume-generation"/);
});

test('failed, evidence-blocked and stopped generation tasks restore the saved request in either progress view', () => {
  for(const mode of ['simple','detailed'])for(const status of ['failed','insufficient_evidence','cancelled']) {
    const item=job({status});
    assert.equal(generationProgressView(item).canRestore,true);
    const html=renderGenerationProgress(item,{mode});
    assert.equal((html.match(/data-action="restore-generation"/g)||[]).length,1);
    assert.match(html,/data-action="restore-generation" data-id="task1"/);
    assert.match(html,/返回修改/);
    assert.doesNotMatch(html,/data-action="open-content"/);
    if(status==='insufficient_evidence')assert.match(html,/data-action="documents"/);
  }
});

test('running and successful jobs and all media jobs cannot restore a text-generation request', () => {
  for(const mode of ['simple','detailed']) {
    for(const status of ['queued','running','succeeded']) {
      const item=job({status,result:status==='succeeded'?{content_id:'saved'}:null});
      assert.equal(generationProgressView(item).canRestore,false);
      assert.doesNotMatch(renderGenerationProgress(item,{mode}),/data-action="restore-generation"/);
    }
    for(const status of ['failed','insufficient_evidence','cancelled','running','succeeded']) {
      const item=job({kind:'media',status});
      assert.equal(generationProgressView(item).canRestore,false);
      assert.doesNotMatch(renderGenerationProgress(item,{mode}),/data-action="restore-generation"/);
    }
  }
});

test('a resumable generation offers continuing saved work separately from editing the request', () => {
  for(const mode of ['simple','detailed']) {
    const html=renderGenerationProgress(job({status:'failed',progress:{resumable:true}}),{mode});
    assert.equal((html.match(/data-action="resume-generation"/g)||[]).length,1);
    assert.equal((html.match(/data-action="restore-generation"/g)||[]).length,1);
    assert.match(html,/<button class="secondary" data-action="restore-generation"/);
    assert.match(html,/已完成的题目已保留/);
  }
});

test('refining resumes only remaining questions with a localized explicit action in both layouts', () => {
  const failed=job({status:'failed',progress:{resumable:true,resume_kind:'repair'},
    timeline:{...job().timeline,stages:[stage('retrieval','completed'),stage('writing','failed'),stage('saving','pending')]}});
  assert.equal(generationProgressView(failed).title,'题目还需完善');
  assert.equal(generationProgressView(failed).resumeKind,'repair');
  assert.deepEqual(generationProgressView(failed).counter,{done:1,total:3});
  try {
    for(const lang of ['zh-CN','en']) for(const mode of ['simple','detailed']) {
      document.documentElement.lang=lang;
      const html=renderGenerationProgress(failed,{mode});
      assert.equal((html.match(/data-action="resume-generation"/g)||[]).length,1);
      assert.equal((html.match(/data-action="restore-generation"/g)||[]).length,1);
      assert.match(html,lang==='en'?/Continue refining/:/继续修正/);
      assert.match(html,lang==='en'?/Completed questions are saved\. Continue refining only the remaining questions\./:/已完成的题目已保留，仅继续修正未通过的题目/);
      assert.doesNotMatch(html,/data-action="open-content"/);
      if(mode==='detailed') assert.match(html,lang==='en'?/This step can be refined further/:/可继续修正此步骤/);
    }
  } finally {document.documentElement.lang='zh-CN';}
  for(const extra of [
    {progress:{resumable:false,resume_kind:'repair'}},
    {status:'running'}, {status:'queued'}, {kind:'speech'},
    {progress:{resumable:true,resume_kind:'<script>secret</script>'}},
  ]) {
    const value={...failed,...extra};
    assert.equal(generationProgressView(value).resumeKind,'continue');
    assert.doesNotMatch(renderGenerationProgress(value),/继续修正|<script>/);
  }
});

test('a failed job can gain a refining action without remounting its progress view', () => {
  const root=mockRoot(), initial=job({status:'failed',progress:{resumable:true,resume_kind:'continue'}});
  const controller=mountGenerationProgress(root,initial);
  controller.update({...initial,progress:{resumable:true,resume_kind:'repair'}});
  assert.match(root.querySelector('[data-progress-actions]').innerHTML,/继续修正/);
  assert.doesNotMatch(root.querySelector('[data-progress-actions]').innerHTML,/data-action="open-content"/);
  controller.destroy?.();
});

test('restore request controls are localized and escape their task identifiers', () => {
  try {
    document.documentElement.lang='en';
    const html=renderGenerationProgress(job({id:'"><img src=x onerror="alert(1)">',status:'failed'}));
    assert.match(html,/Edit request/);
    assert.match(html,/Your request is saved\. Return to edit it and try again\./);
    assert.match(html,/data-id="&quot;&gt;&lt;img/);
    assert.doesNotMatch(html,/<img|onerror="alert/);
  } finally {document.documentElement.lang='zh-CN';}
});

test('live transition into failure adds restore controls without inventing a generated result', () => {
  const root=mockRoot(), initial=job(), controller=mountGenerationProgress(root,initial);
  controller.update(job({status:'failed'}));
  const actions=root.querySelector('[data-progress-actions]').innerHTML;
  assert.match(actions,/data-action="restore-generation"/);
  assert.doesNotMatch(actions,/data-action="open-content"/);
  controller.destroy?.();
});

test('media stages are opt-in from actual server stages and unknown kinds degrade to generic content', () => {
  const regular = renderGenerationProgress(job());
  assert.doesNotMatch(regular,/data-stage="(audio|image|video)"/);
  for (const [kind,label] of [['audio','生成语音'],['speech','生成语音'],['image','生成图片'],['video','生成视频'],['future_modality','处理内容']]) {
    const view = generationProgressView(job({timeline:{recorded:true,stages:[stage(kind,'active')]}}));
    assert.equal(view.stages[0].label,label);
  }
});

test('titles and action text switch language while untrusted identifiers remain escaped', () => {
  document.documentElement.lang = 'en';
  try {
    const html = renderGenerationProgress(job({id:'"><script>alert(1)</script>',status:'failed',progress:{resumable:true}}));
    assert.match(html,/Generation interrupted/);
    assert.match(html,/Continue generation/);
    assert.doesNotMatch(html,/<script>/);
    assert.match(html,/&quot;&gt;&lt;script&gt;/);
    const success = renderGenerationProgress(job({status:'succeeded',result:{content_id:'" onclick="x'}}));
    assert.match(success,/View result/);
    assert.match(success,/data-id="&quot; onclick=&quot;x"/);
  } finally { document.documentElement.lang='zh-CN'; }
});

function element() {
  let html = '', writes = 0;
  return {dataset:{},hidden:false,textContent:'',get innerHTML(){return html;},set innerHTML(value){html=value;writes++;},get writes(){return writes;}};
}
function mockRoot() {
  const elements = new Map(['#generation-progress-title','[data-progress-icon]','[data-progress-count]','[data-progress-message]','[data-progress-reason]','[data-progress-stages]','[data-progress-actions]','[data-progress-connection]'].map(selector=>[selector,element()]));
  layerContainer(elements.get('[data-progress-icon]'),['retrieval','writing','saving','check','paused'],'writing');
  return {dataset:{},querySelector:selector=>elements.get(selector)};
}
function layerContainer(node, kinds, active) {
  const layers = kinds.map(kind=>({dataset:{motionLayer:kind,visible:String(kind===active),running:String(kind===active)}}));
  node.querySelectorAll=()=>layers;
  node.insertAdjacentHTML=(_position,html)=>{layers.push({dataset:{motionLayer:html.match(/data-motion-layer="([^"]+)"/)[1],visible:'false',running:'false'}});};
  return layers;
}

test('poll updates do not replace orbit or interactive actions while activity and counts change', () => {
  const root=mockRoot(), initial=job(), view=mountGenerationProgress(root,initial);
  view.update(job({timeline:{...initial.timeline,activity:'solving',completed:2}}));
  assert.equal(root.querySelector('#generation-progress-title').textContent,'正在核对答案');
  assert.equal(root.querySelector('[data-progress-count]').textContent,'已检查 2 / 3 题');
  assert.equal(root.querySelector('[data-progress-icon]').writes,0);
  assert.equal(root.querySelector('[data-progress-actions]').writes,0);
  assert.equal(root.querySelector('[data-progress-stages]').writes,0);
  view.update(job({status:'succeeded',result:{content_id:'result'}}));
  assert.equal(root.querySelector('[data-progress-actions]').writes,1);
  assert.equal(root.querySelector('[data-progress-icon]').writes,0);
  assert.equal(root.querySelector('[data-progress-icon]').querySelectorAll().find(layer=>layer.dataset.motionLayer==='check').dataset.visible,'true');
});

test('connection loss preserves the last known task status and clears on recovery', () => {
  const root=mockRoot(), initial=job(), view=mountGenerationProgress(root,initial);
  view.update(initial);
  view.connection(new Error('offline'));
  assert.equal(root.dataset.state,'running');
  assert.equal(root.dataset.connection,'interrupted');
  assert.equal(root.querySelector('[data-progress-connection]').hidden,false);
  assert.equal(root.querySelector('[data-progress-actions]').writes,0);
  view.connection(null);
  assert.equal(root.querySelector('[data-progress-connection]').hidden,true);
});

class Visibility extends EventTarget {hidden=false;}
const advance=async(context,n=0)=>{context.mock.timers.tick(n);await Promise.resolve();};
function observe(context, options={}) {
  context.mock.timers.enable({apis:['setTimeout']});
  const updates=[], errors=[], visibility=new Visibility();
  const stop=watchGenerationProgress({load:async()=>job(),initial:job(),interval:100,visibility,update:value=>updates.push(value),error:value=>errors.push(value),...options});
  context.after(stop);
  return {stop,updates,errors,visibility};
}

test('terminal snapshots do not poll and a newly completed job stops further reads', async context => {
  let loads=0;
  const observer=observe(context,{load:async()=>{loads++;return job({status:'succeeded'});}});
  await advance(context,100);
  assert.equal(loads,1);
  await advance(context,1000);
  assert.equal(loads,1);
  assert.equal(observer.updates.length,1);
  let terminalLoads=0;
  const stop=watchGenerationProgress({initial:job({status:'failed'}),load:async()=>terminalLoads++,visibility:observer.visibility});
  await advance(context,1000);stop();
  assert.equal(terminalLoads,0);
});

test('leaving the page suppresses a late progress reply and terminal action', async context => {
  let resolve;
  const pending=new Promise(done=>{resolve=done;});
  const observer=observe(context,{load:()=>pending});
  await advance(context,100);
  observer.stop();
  resolve(job({status:'succeeded',result:{content_id:'old-account'}}));
  await advance(context);
  assert.deepEqual(observer.updates,[]);
  assert.deepEqual(observer.errors,[]);
});

test('temporary polling failure retries and recovers even when job contents do not change', async context => {
  let calls=0;
  const error=new Error('offline');
  const observer=observe(context,{load:async()=>{if(++calls===1)throw error;return job();}});
  await advance(context,100);
  assert.deepEqual(observer.errors,[error]);
  await advance(context,100);
  assert.deepEqual(observer.errors,[error,null]);
  assert.equal(observer.updates.length,0);
});

test('generation and media creation route to progress, while course changes leave the task context', () => {
  const source=readFileSync('app/static/app.js','utf8');
  assert.match(source,/onCreated:job=>\{\s*state\.library=.*?go\('progress',job\.job_id\)/s);
  assert.match(source,/name === 'progress' \? 'jobs'/);
  assert.match(source,/else if\(state\.route\.page==='progress'\)go\('jobs'\)/);
  assert.match(source,/make-media.*?go\('progress',job\.job_id\)/);
});


test('failure details escape provider text and only appear for terminal problems', () => {
  const reason='<script>bad</script> exceeded limit';
  const html=renderGenerationProgress(job({status:'failed',error:reason}));
  assert.match(html,/data-progress-reason ><summary>/);
  assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.equal(generationProgressView(job({error:reason})).reason,'');
  assert.match(renderGenerationProgress(job()),/data-action="retry"/);
});


test('known provider failure details translate to English while unknown messages remain escaped', () => {
  document.documentElement.lang='en';
  try {
    for (const [reason, expected] of [
      ['text API 返回 HTTP 503；请核对平台、模型及参数。本次未自动重试。','Text API returned HTTP 503: Model service temporarily unavailable; try again later. No automatic retry was made.'],
      ['text API 返回 HTTP 429；平台限流或额度已用尽。本次未自动重试。','Text API returned HTTP 429: Provider rate limit or quota reached. No automatic retry was made.'],
    ]) {
      const html=renderGenerationProgress(job({status:'failed',error:reason}));
      assert.ok(html.includes('>'+expected+'</span>'));
    }
    const html=renderGenerationProgress(job({status:'failed',error:'Unknown <img src=x onerror=attack()>'}));
    assert.match(html,/Unknown &lt;img src=x onerror=attack\(\)&gt;/);
    assert.doesNotMatch(html,/<img/);
  } finally {document.documentElement.lang='zh-CN';}
});

test('server timings distinguish recorded durations from unknown or incomplete history', () => {
  assert.equal(durationText(null),'—');
  assert.equal(durationText(-1),'—');
  assert.equal(durationText(NaN),'—');
  assert.equal(durationText(0),'0:00');
  assert.equal(durationText(125500),'2:05');
  assert.equal(durationText(3725000),'1:02:05');
  const timed=job({timeline:{...job().timeline,elapsed_ms:65000,timing_complete:false,stages:[{...stage('writing','active'),elapsed_ms:65000,timing_complete:false}]}});
  const html=renderGenerationProgress(timed,{mode:'detailed'});
  assert.match(html,/≥ 1:05/);
  assert.match(html,/data-progress-total-time/);
  assert.doesNotMatch(html,/100%|预计|ETA/);
  assert.match(renderGenerationProgress(job(),{mode:'detailed'}),/耗时未记录/);
});

test('detail renderer contains stage navigation and distinct decorative scenes without fake model content', () => {
  const html=renderGenerationProgress(job(),{mode:'detailed'});
  assert.match(html,/generation-workbench/);
  assert.match(html,/data-stage-select="writing" aria-pressed="true"/);
  assert.match(html,/data-scene-kind="writing"/);
  assert.match(html,/data-progress-events tabindex="0"/);
  assert.match(html,/aria-hidden="true"/);
  assert.doesNotMatch(renderGenerationProgress(job()),/data-stage-select/);
  for(const kind of ['retrieval','planning','writing','saving','audio','image','video']) {
    const value=job({timeline:{recorded:true,stages:[stage(kind,'active')],active_stage:kind}});
    assert.match(renderGenerationProgress(value,{mode:'detailed'}),new RegExp(`data-scene-kind="${kind}"`));
  }
});

test('structured event copy admits known data only and gives factual reasons for fusion fallback', () => {
  assert.equal(eventMessage({code:'unknown',data:{message:'secret'}}),null);
  assert.equal(eventMessage({code:'activity_changed',data:{activity:'__proto__'}}),null);
  assert.equal(eventMessage({code:'retrieval_selected',data:{selected:'<script>'}}),null);
  const rewrite=eventMessage({code:'fusion_fallback',data:{reason:'literal_changed',prompt:'secret'}});
  assert.equal(rewrite.key,'改写改变了关键条件，保留原始检索');
  assert.deepEqual(rewrite.values,{});
  assert.equal(eventMessage({code:'fusion_fallback',data:{reason:'user secret'}}).key,'已使用原始检索结果');
  assert.equal(eventMessage({code:'question_rejected',data:{number:2,attempt:3}}).key,'第 {n} 题未通过第 {attempt} 次检查');
  assert.equal(eventMessage({code:'question_repair',data:{number:2,attempt:3}}).key,'正在修正第 {n} 题 · 第 {attempt} 轮');
});

test('question check categories are localized without exposing arbitrary reviewer text', () => {
  const labels={
    format:['需调整格式','needs a format adjustment'],
    difficulty:['需调整难度','needs a difficulty adjustment'],
    quality:['需修正内容','needs content refinement'],
    evidence:['需补充依据','needs more supporting evidence'],
    duplicate:['与已有题目重复','duplicates an existing question'],
  };
  try {
    for(const [reason,[zh,en]] of Object.entries(labels)) {
      const event={id:1,stage:'writing',code:'question_check_failed',at:'2026-09-20T09:29:53Z',
        data:{number:2,attempt:3,reason,model_reasoning:'PRIVATE_REASONING',detail:'<script>PRIVATE_OUTPUT</script>'}};
      assert.deepEqual(eventMessage(event).values,{n:2,attempt:3});
      for(const lang of ['zh-CN','en']) {
        document.documentElement.lang=lang;
        const html=renderGenerationProgress(job({timeline:{...job().timeline,events:[event]}}),{mode:'detailed'});
        assert.ok(html.includes(lang==='en'?en:zh));
        assert.doesNotMatch(html,/PRIVATE_|<script>/);
      }
    }
  } finally {document.documentElement.lang='zh-CN';}
  for(const data of [
    {number:2,attempt:3,reason:'PRIVATE_REASONING'},
    {number:2,attempt:3,reason:'__proto__'},
    {number:2,attempt:3,reason:null},
    {number:2,attempt:3,reason:{toString:'PRIVATE_REASONING'}},
    {number:true,attempt:3,reason:'format'},
    {number:2,attempt:'3',reason:'format'},
  ]) assert.equal(eventMessage({code:'question_check_failed',data}),null);
  assert.equal(eventMessage({code:'question_rejected',data:{number:2,attempt:3}}).key,'第 {n} 题未通过第 {attempt} 次检查');
});

test('format adaptation events never claim that an answer passed its checks', () => {
  const events=[
    {id:1,stage:'writing',code:'question_format_adapting',data:{number:2,private:'PRIVATE_OUTPUT'}},
    {id:2,stage:'writing',code:'question_format_adapted',data:{number:2,level:'compatible',private:'PRIVATE_OUTPUT'}},
  ];
  assert.equal(eventMessage(events[0]).key,'正在调整第 {n} 题的格式要求');
  assert.equal(eventMessage(events[1]).key,'第 {n} 题已兼容格式差异');
  for(const event of events) assert.deepEqual(eventMessage(event).values,{n:2});
  assert.equal(eventMessage({...events[1],data:{number:2,level:'unchecked'}}),null);
  assert.equal(eventMessage({...events[1],data:{number:2,level:{toString:'compatible'}}}),null);
  assert.equal(eventMessage({...events[0],data:{number:'2'}}),null);
  try {
    for(const lang of ['zh-CN','en']) {
      document.documentElement.lang=lang;
      const html=renderGenerationProgress(job({timeline:{...job().timeline,events}}),{mode:'detailed'});
      assert.match(html,lang==='en'?/Adapting format requirements for question 2/:/正在调整第 2 题的格式要求/);
      assert.match(html,lang==='en'?/Adapted formatting for question 2/:/第 2 题已兼容格式差异/);
      assert.doesNotMatch(html,/PRIVATE_|Question 2 passed its checks|第 2 题已通过检查|data-action="open-content"/);
    }
  } finally {document.documentElement.lang='zh-CN';}
});

test('detail activity escapes identifiers and does not expose arbitrary model event data', () => {
  const timeline={...job().timeline,events:[
    {id:'"><script>x</script>',stage:'writing',code:'question_passed',at:'2026-09-20T02:03:04Z',data:{number:2,chain_of_thought:'PRIVATE_REASONING',prompt:'PRIVATE_PROMPT'}},
    {id:2,stage:'writing',code:'arbitrary',data:{message:'PRIVATE_PROVIDER_BODY'}},
  ]};
  const html=renderGenerationProgress(job({timeline}),{mode:'detailed'});
  assert.match(html,/第 2 题已通过检查/);
  assert.doesNotMatch(html,/PRIVATE_|<script>/);
  assert.match(html,/&lt;script&gt;/);
});

test('selecting historical stages remains pinned while following selects the active stage', () => {
  const view=generationProgressView(job());
  assert.equal(progressSelection(view,'retrieval',false).id,'retrieval');
  assert.equal(progressSelection(view,'retrieval',true).id,'writing');
  assert.equal(progressSelection(view,'deleted',false).id,'writing');
});

function richElement() {
  const node=element();
  node.attrs=new Map();
  node.setAttribute=(key,value)=>node.attrs.set(key,value);
  node.removeAttribute=key=>node.attrs.delete(key);
  node.scrollHeight=240;node.clientHeight=100;node.scrollTop=140;
  return node;
}
function detailedRoot(initial) {
  const selectors=['#generation-progress-title','#generation-stage-title','[data-progress-count]','[data-progress-total-time]','[data-progress-selected-time]','[data-progress-message]','[data-progress-reason]','[data-progress-actions]','[data-progress-connection]','[data-progress-truncated]','[data-progress-visual]','[data-progress-stage-headline]','[data-progress-events]','[data-progress-follow]'];
  const elements=new Map(selectors.map(selector=>[selector,richElement()]));
  const layers=layerContainer(elements.get('[data-progress-visual]'),[...new Set(initial.timeline.stages.map(stage=>stage.kind))],initial.timeline.active_stage);
  const rows=initial.timeline.stages.map(stage=>{
    const row=richElement();row.dataset.stage=stage.id;
    const button=richElement();button.dataset.stageSelect=stage.id;
    button.closest=selector=>selector==='[data-stage-select]'?button:null;
    const children=new Map([['[data-stage-select]',button],['.generation-stage-state',richElement()],['.generation-stage-icon',richElement()],['.generation-stage-time',richElement()]]);
    row.querySelector=selector=>children.get(selector);
    return row;
  });
  const nav=richElement();
  nav.querySelectorAll=selector=>selector==='[data-stage]'?rows:rows.map(row=>row.querySelector('[data-stage-select]'));
  elements.set('[data-progress-stages]',nav);
  const follow=elements.get('[data-progress-follow]');
  follow.closest=selector=>selector==='[data-progress-follow]'?follow:null;
  const handlers=new Map();
  return {dataset:{},querySelector:selector=>elements.get(selector),rows,layers,contains:()=>true,addEventListener:(type,handler)=>handlers.set(type,handler),removeEventListener:type=>handlers.delete(type),click:(target,detail=1)=>handlers.get('click')?.({target,detail}),handlers};
}

test('detailed timing updates preserve selected navigation, animation nodes and event scroll state', () => {
  const initial=job({timeline:{...job().timeline,elapsed_ms:12000,timing_complete:true,events:[],stages:job().timeline.stages.map(stage=>({...stage,elapsed_ms:4000,timing_complete:true}))}});
  const root=detailedRoot(initial), view=mountGenerationProgress(root,initial,{mode:'detailed'});
  const historyButton=root.rows[0].querySelector('[data-stage-select]');
  root.click(historyButton);
  assert.equal(root.querySelector('#generation-stage-title').textContent,'检索资料');
  assert.equal(root.querySelector('[data-progress-follow]').disabled,false);
  const visualWrites=root.querySelector('[data-progress-visual]').writes, eventWrites=root.querySelector('[data-progress-events]').writes;
  const advanced={...initial,timeline:{...initial.timeline,elapsed_ms:14000,stages:initial.timeline.stages.map(stage=>stage.id==='writing'?{...stage,elapsed_ms:6000}:stage)}};
  view.update(advanced);
  assert.equal(root.querySelector('#generation-stage-title').textContent,'检索资料');
  assert.equal(root.querySelector('[data-progress-stages]').writes,0);
  assert.equal(root.querySelector('[data-progress-visual]').writes,visualWrites);
  assert.equal(root.querySelector('[data-progress-events]').writes,eventWrites);
  assert.equal(root.querySelector('[data-progress-events]').scrollTop,0,'history selection deliberately starts at the beginning');
  assert.equal(root.rows[0].querySelector('[data-stage-select]'),historyButton);
  assert.equal(historyButton.attrs.get('aria-pressed'),'true');
  root.click(root.querySelector('[data-progress-follow]'));
  assert.equal(root.querySelector('#generation-stage-title').textContent,'生成与检查');
  assert.equal(root.querySelector('[data-progress-follow]').disabled,true);
  view.destroy();
  assert.equal(root.handlers.size,0);
});

test('detailed terminal state stops scene motion and preserves results for explicit review', () => {
  const initial=job(),root=detailedRoot(initial),view=mountGenerationProgress(root,initial,{mode:'detailed'});
  view.update({...initial,status:'succeeded',result:{content_id:'finished'},timeline:{...initial.timeline,active_stage:null,stages:initial.timeline.stages.map(stage=>({...stage,status:'completed'}))}});
  assert.equal(root.dataset.state,'succeeded');
  assert.equal(root.querySelector('[data-progress-visual]').dataset.motion,'static');
  assert.match(root.querySelector('[data-progress-actions]').innerHTML,/data-id="finished"/);
  view.destroy();
});

test('compact horizontal navigation reveals the followed stage only on entry or a stage change', () => {
  const initial=job(),root=detailedRoot(initial),nav=root.querySelector('[data-progress-stages]');
  nav.clientWidth=200;nav.scrollWidth=600;nav.scrollLeft=0;
  nav.getBoundingClientRect=()=>({left:10,right:210});
  root.rows.forEach((row,index)=>{row.querySelector('[data-stage-select]').getBoundingClientRect=()=>({left:10+index*180-nav.scrollLeft,right:170+index*180-nav.scrollLeft});});
  const view=mountGenerationProgress(root,initial,{mode:'detailed'});
  assert.equal(nav.scrollLeft,148,'the active writing step is revealed on initial entry');
  nav.scrollLeft=0;
  view.update({...initial,timeline:{...initial.timeline,elapsed_ms:2000}});
  assert.equal(nav.scrollLeft,0,'a time-only snapshot respects manual navigation scrolling');
  view.update({...initial,timeline:{...initial.timeline,active_stage:'saving',activity:'saving',stages:initial.timeline.stages.map(stage=>({...stage,status:stage.id==='saving'?'active':'completed'}))}});
  assert.ok(nav.scrollLeft>300,'following reveals a newly active step');
  view.destroy();
});

test('new activity in another stage does not replace historical records being read or selected', () => {
  const initial=job({timeline:{...job().timeline,events:[{id:1,stage:'retrieval',code:'retrieval_selected',at:'2026-09-20T00:00:00Z',data:{selected:4}}]}});
  const root=detailedRoot(initial),view=mountGenerationProgress(root,initial,{mode:'detailed'});
  root.click(root.rows[0].querySelector('[data-stage-select]'));
  const list=root.querySelector('[data-progress-events]'),writes=list.writes,content=list.innerHTML;
  list.scrollTop=18;
  view.update({...initial,timeline:{...initial.timeline,events:[...initial.timeline.events,{id:2,stage:'writing',code:'question_passed',at:'2026-09-20T00:00:10Z',data:{number:1}}]}});
  assert.equal(list.writes,writes,'unrelated live events must not clear a user text selection');
  assert.equal(list.innerHTML,content);
  assert.equal(list.scrollTop,18);
  view.destroy();
});

function motionBrowser(root, {reduced = false} = {}) {
  const calls=[], listeners=new Map();
  const preference={matches:reduced,addEventListener:(type,callback)=>listeners.set(type,callback),removeEventListener:type=>listeners.delete(type)};
  root.ownerDocument={defaultView:{matchMedia:()=>preference,getComputedStyle:node=>({opacity:node.presentedOpacity || '.86'})}};
  for (const selector of ['#generation-progress-title','#generation-stage-title','[data-progress-stage-headline]','[data-progress-events]','[data-progress-actions]']) {
    const node=root.querySelector(selector);
    if (!node) continue;
    node.animate=(frames,options)=>{
      const animation={cancelled:false,cancel(){this.cancelled=true;}};
      calls.push({node,frames,options,animation});return animation;
    };
  }
  return {calls,preference,listeners};
}

test('stage changes crossfade persistent scenes and retain navigation instead of remounting them', () => {
  const initial=job(),root=detailedRoot(initial),browser=motionBrowser(root);
  const controller=mountGenerationProgress(root,initial,{mode:'detailed'}), visual=root.querySelector('[data-progress-visual]');
  const writing=root.layers.find(layer=>layer.dataset.motionLayer==='writing'), saving=root.layers.find(layer=>layer.dataset.motionLayer==='saving');
  const next={...initial,timeline:{...initial.timeline,active_stage:'saving',activity:'saving',stages:initial.timeline.stages.map(stage=>({...stage,status:stage.id==='saving'?'active':'completed'}))}};
  controller.update(next);
  assert.equal(visual.writes,0);
  assert.equal(root.querySelector('[data-progress-stages]').writes,0);
  assert.equal(writing.dataset.visible,'false');assert.equal(writing.dataset.running,'false');
  assert.equal(saving.dataset.visible,'true');assert.equal(saving.dataset.running,'true');
  assert.equal(visual.dataset.transition,'soft');
  const calls=browser.calls.length;
  controller.update({...next,timeline:{...next.timeline,elapsed_ms:22000}});
  assert.equal(browser.calls.length,calls,'timer polling must not restart text transitions');
  assert.equal(root.layers.find(layer=>layer.dataset.motionLayer==='saving'),saving);
  controller.update({...next,status:'succeeded',result:{content_id:'done'},timeline:{...next.timeline,active_stage:null,stages:next.timeline.stages.map(stage=>({...stage,status:'completed'}))}});
  assert.equal(saving.dataset.running,'false','completion freezes the current pose instead of removing its animation');
  assert.equal(saving.dataset.visible,'true');
  assert.match(root.querySelector('[data-progress-actions]').innerHTML,/data-id="done"/);
  controller.destroy();
});

test('rapid updates retarget from the presented opacity and never queue stale stage text', () => {
  const initial=job(),root=detailedRoot(initial),browser=motionBrowser(root);
  const controller=mountGenerationProgress(root,initial,{mode:'detailed'}), title=root.querySelector('#generation-progress-title');
  controller.update({...initial,timeline:{...initial.timeline,activity:'writing'}});
  const first=browser.calls.find(call=>call.node===title);
  assert.equal(first.frames[0].opacity,.72);
  title.presentedOpacity='.91';
  controller.update({...initial,timeline:{...initial.timeline,activity:'solving'}});
  const latest=browser.calls.filter(call=>call.node===title).at(-1);
  assert.equal(first.animation.cancelled,true);
  assert.equal(latest.frames[0].opacity,.91);
  assert.equal(title.textContent,'正在核对答案');
  root.click(root.rows[0].querySelector('[data-stage-select]'));
  root.click(root.rows[1].querySelector('[data-stage-select]'));
  assert.deepEqual(root.layers.filter(layer=>layer.dataset.visible==='true').map(layer=>layer.dataset.motionLayer),['writing']);
  assert.equal(root.querySelector('[data-progress-visual]').writes,0);
  controller.destroy();
  assert.equal(latest.animation.cancelled,true,'leaving the page releases animation work');
  assert.equal(browser.listeners.size,0);
});

test('keyboard stage selection is immediate and changing reduced-motion preference cancels local transitions', () => {
  const initial=job(),root=detailedRoot(initial),browser=motionBrowser(root);
  const controller=mountGenerationProgress(root,initial,{mode:'detailed'});
  root.click(root.rows[0].querySelector('[data-stage-select]'),0);
  assert.equal(root.querySelector('#generation-stage-title').textContent,'检索资料');
  assert.equal(root.querySelector('[data-progress-visual]').dataset.transition,'instant');
  assert.equal(root.dataset.progressInput,'keyboard');
  assert.equal(browser.calls.length,0);
  root.click(root.rows[1].querySelector('[data-stage-select]'),1);
  assert.ok(browser.calls.length>0);
  browser.preference.matches=true;browser.listeners.get('change')();
  assert.ok(browser.calls.every(call=>call.animation.cancelled));
  const count=browser.calls.length;
  root.click(root.rows[0].querySelector('[data-stage-select]'),1);
  assert.equal(browser.calls.length,count);
  assert.equal(root.querySelector('#generation-stage-title').textContent,'检索资料');
  controller.destroy();
});

test('newly added modality gets its own layer without replacing existing illustrations', () => {
  const initial=job(),root=detailedRoot(initial),controller=mountGenerationProgress(root,initial,{mode:'detailed'});
  const writing=root.layers.find(layer=>layer.dataset.motionLayer==='writing');
  controller.update({...initial,timeline:{...initial.timeline,active_stage:'audio',activity:'generating_audio',stages:[...initial.timeline.stages.map(stage=>({...stage,status:'completed'})),stage('audio','active')]}});
  assert.equal(root.querySelector('[data-progress-visual]').writes,0);
  assert.equal(root.layers.find(layer=>layer.dataset.motionLayer==='writing'),writing);
  assert.equal(root.layers.find(layer=>layer.dataset.motionLayer==='audio').dataset.visible,'true');
  controller.destroy();
});

test('simple progress duration updates preserve stage nodes and completion glyph layers', () => {
  const initial=job(),root=detailedRoot(initial),icons=richElement();
  layerContainer(icons,['retrieval','writing','saving','check','paused'],'writing');
  const find=root.querySelector;
  root.querySelector=selector=>selector==='[data-progress-icon]'?icons:find(selector);
  const controller=mountGenerationProgress(root,initial);
  const row=root.rows[1], icon=row.querySelector('.generation-stage-icon');
  controller.update({...initial,timeline:{...initial.timeline,stages:initial.timeline.stages.map(stage=>({...stage,elapsed_ms:4200,timing_complete:true}))}});
  assert.equal(root.querySelector('[data-progress-stages]').writes,0);
  assert.equal(icon.writes,0);
  controller.update({...initial,status:'succeeded',result:{content_id:'done'},timeline:{...initial.timeline,stages:initial.timeline.stages.map(stage=>({...stage,status:'completed'}))}});
  assert.equal(row.dataset.state,'completed');assert.equal(icon.writes,0);
  assert.equal(icons.querySelectorAll().find(layer=>layer.dataset.motionLayer==='check').dataset.visible,'true');
  controller.destroy();
});
