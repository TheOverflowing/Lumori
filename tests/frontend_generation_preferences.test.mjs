import test from 'node:test';
import assert from 'node:assert/strict';
import {createGenerationPreferences, generationPreferencesKey} from '../app/static/generation-preferences.js';
import {renderSettings, renderSwitch, renderModelConnections} from '../app/static/settings.js';
import {createGenerationFlow} from '../app/static/clarification.js';

globalThis.document={documentElement:{lang:'zh-CN'}};
const memoryStorage=()=>{const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};};

test('progress view migrates old preferences, stays account-scoped, and preserves fusion',()=>{
  const storage=memoryStorage();
  storage.setItem(generationPreferencesKey('a'),'{"query_fusion":true}');
  const preferences=createGenerationPreferences({storage});
  preferences.setAccount('a');
  assert.equal(preferences.progressMode,'detailed');
  preferences.setProgressMode('simple');
  assert.deepEqual(preferences.requestFields(),{query_fusion:true});
  preferences.setQueryFusion(false);
  assert.equal(JSON.parse(storage.getItem(generationPreferencesKey('a'))).progress_mode,'simple');
  preferences.setAccount('b');assert.equal(preferences.progressMode,'detailed');
  preferences.setAccount('a');assert.equal(preferences.progressMode,'simple');
  preferences.setAccount(null);assert.equal(preferences.progressMode,'detailed');
  preferences.setProgressMode('simple');assert.equal(preferences.progressMode,'detailed');
});

test('display-only changes are distinguishable and never enter generation requests',()=>{
  const storage=memoryStorage(),changes=[];
  const preferences=createGenerationPreferences({storage,onChange:c=>changes.push(c)});
  preferences.setAccount('a');
  preferences.setProgressMode('simple');
  assert.deepEqual(changes[0].changed,['progressMode']);
  assert.deepEqual(preferences.requestFields(),{});
  preferences.setProgressMode('unknown');assert.equal(changes.length,1);
  storage.setItem(generationPreferencesKey('a'),'{"progress_mode":"detailed"}');
  preferences.syncStorage(generationPreferencesKey('a'));
  assert.equal(preferences.progressMode,'detailed');
  assert.deepEqual(changes[1].changed,['progressMode']);
  preferences.setQueryFusion(true);assert.deepEqual(changes[2].changed,['queryFusion']);
});

test('settings exposes native bilingual progress choices and the selected view',()=>{
  const html=renderSettings(false,'simple');
  assert.match(html,/<legend>.*进度显示/);
  assert.match(html,/type="radio" name="progress-mode" value="simple" checked/);
  assert.doesNotMatch(html,/value="detailed" checked/);
  try {
    document.documentElement.lang='en';
    const english=renderSettings(false,'detailed');
    assert.match(english,/>Progress view</);
    assert.match(english,/>Simple</);
    assert.match(english,/>Detailed</);
  } finally {document.documentElement.lang='zh-CN';}
});

test('fusion defaults off and restores per account without a shared fallback',()=>{
  const storage=memoryStorage(), changes=[];
  storage.setItem('zhixu.query_fusion','true');
  const preferences=createGenerationPreferences({storage,onChange:change=>changes.push(change)});
  assert.deepEqual(preferences.requestFields(),{});
  preferences.setQueryFusion(true);assert.equal(preferences.queryFusion,false);
  preferences.setAccount('account/a');assert.equal(preferences.queryFusion,false);
  preferences.setQueryFusion(true);assert.deepEqual(preferences.requestFields(),{query_fusion:true});
  preferences.setAccount('account/b');assert.equal(preferences.queryFusion,false);
  preferences.setAccount('account/a');assert.equal(preferences.queryFusion,true);
  assert.match(generationPreferencesKey('account/a'),/account%2Fa/);
  assert.equal(changes.length,1);
  assert.equal(JSON.parse(storage.getItem(generationPreferencesKey('account/a'))).query_fusion,true);
  preferences.setAccount(null);assert.deepEqual(preferences.requestFields(),{});
});

test('only strict boolean storage enables fusion; malformed data stays off',()=>{
  for(const value of ['broken','null','true','{"query_fusion":"true"}','{"query_fusion":1}']){
    const storage=memoryStorage();storage.setItem(generationPreferencesKey('a'),value);
    const preferences=createGenerationPreferences({storage});preferences.setAccount('a');
    assert.equal(preferences.queryFusion,false);
  }
});

test('unavailable storage retains a usable account-scoped session preference',()=>{
  const storage={getItem:()=>{throw Error('blocked');},setItem:()=>{throw Error('blocked');}};
  const preferences=createGenerationPreferences({storage});preferences.setAccount('a');
  preferences.setQueryFusion(true);assert.deepEqual(preferences.requestFields(),{query_fusion:true});
  preferences.setAccount('a');assert.equal(preferences.queryFusion,true);
  preferences.setQueryFusion(false);assert.deepEqual(preferences.requestFields(),{});
  preferences.setQueryFusion(true);preferences.setAccount('b');assert.equal(preferences.queryFusion,false);
});

test('external storage changes affect only the active account and publish actual changes',()=>{
  const storage=memoryStorage(), changes=[],preferences=createGenerationPreferences({storage,onChange:c=>changes.push(c)});
  preferences.setAccount('a');
  storage.setItem(generationPreferencesKey('b'),'{"query_fusion":true}');preferences.syncStorage(generationPreferencesKey('b'));
  assert.equal(preferences.queryFusion,false);assert.deepEqual(changes,[]);
  storage.setItem(generationPreferencesKey('a'),'{"query_fusion":true}');preferences.syncStorage(generationPreferencesKey('a'));
  assert.equal(preferences.queryFusion,true);assert.equal(changes.length,1);
  preferences.syncStorage(generationPreferencesKey('a'));assert.equal(changes.length,1);
  storage.setItem(generationPreferencesKey('a'),'{}');preferences.syncStorage(null);
  assert.equal(preferences.queryFusion,false);assert.equal(changes.length,2);
});

test('a preference change invalidates a pending check while preserving the caller draft',async()=>{
  const storage=memoryStorage(),calls=[];let finish;
  const reply=new Promise(resolve=>{finish=resolve;});
  const flow=createGenerationFlow({request:async(path,method,body)=>{calls.push({path,body});return reply;}});
  const preferences=createGenerationPreferences({storage,onChange:()=>flow.cancel()});preferences.setAccount('a');
  const draft={topic:'Explain this step',course_id:'course-a',request_key:'first'};
  const original=structuredClone(draft);const pending=flow.start({...draft,...preferences.requestFields()});
  storage.setItem(generationPreferencesKey('a'),'{"query_fusion":true}');preferences.syncStorage(generationPreferencesKey('a'));
  finish({preparation_id:'a'.repeat(32),status:'ready',options:[],expires_at:1900000000});await pending;
  assert.equal(flow.state.phase,'cancelled');assert.equal(calls.length,1);assert.deepEqual(draft,original);
});

test('fusion is captured in prepare and generation retries while the original topic stays untouched',async()=>{
  const storage=memoryStorage(), preferences=createGenerationPreferences({storage}),calls=[];
  preferences.setAccount('a');preferences.setQueryFusion(true);
  const snapshot={topic:'Explain the fast thing',request_key:'one',...preferences.requestFields()};
  const flow=createGenerationFlow({request:async(path,method,body)=>{
    calls.push({path,body:structuredClone(body)});
    if(path.endsWith('/prepare'))return {preparation_id:'a'.repeat(32),status:'ready',options:[],expires_at:1900000000};
    if(calls.length===2)throw Error('network');
    return {job_id:'one'};
  }});
  await assert.rejects(flow.start(snapshot),/network/);preferences.setQueryFusion(false);await flow.retry();
  assert.equal(calls[0].body.query_fusion,true);assert.equal(calls[1].body.query_fusion,true);
  assert.deepEqual(calls[1],calls[2]);assert.equal(calls[2].body.topic,'Explain the fast thing');
});

test('minimal settings and switch preserve bilingual accessible labels and escape model names',()=>{
  try{
    document.documentElement.lang='en';
    const html=renderSettings(true);
    assert.match(html,/Generation/);assert.match(html,/Fusion mode/);assert.match(html,/Model connections/);
    assert.match(html,/role="switch" id="query-fusion" checked/);
    assert.doesNotMatch(html,/hint|<small|调用|call/);
    assert.match(renderSwitch({id:'ask',label:'生成前追问'}),/Ask before generating/);
    const models=renderModelConnections({text:{model:'<script>private</script>',configured:true}});
    assert.match(models,/&lt;script&gt;/);assert.doesNotMatch(models,/<script>/);
    document.documentElement.lang='zh-CN';assert.match(renderSettings(false),/>融合模式</);
  }finally{document.documentElement.lang='zh-CN';}
});
