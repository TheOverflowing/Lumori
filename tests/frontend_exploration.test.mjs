import test from 'node:test';
import assert from 'node:assert/strict';
import {generationEligibility, renderExternalSource} from '../app/static/exploration.js';
import {createGenerationPreferences, generationPreferencesKey} from '../app/static/generation-preferences.js';
import {createGenerationFlow} from '../app/static/clarification.js';
import {renderSettings, renderModelConnections} from '../app/static/settings.js';
import {eventMessage, generationProgressView, renderGenerationProgress} from '../app/static/generation-progress.js';

globalThis.document={documentElement:{lang:'zh-CN'}};
const storage=()=>{const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};};
const configured={capabilities:{text:{configured:true},embedding:{configured:true}},auto_exploration:{available:true,provider:'curated'}};

test('exploration requires strict opt-in, restores per account, and preserves existing preferences',()=>{
  const store=storage(),changes=[],prefs=createGenerationPreferences({storage:store,onChange:change=>changes.push(change)});
  prefs.setAutoExplore(true);assert.equal(prefs.autoExplore,false);
  store.setItem(generationPreferencesKey('a'),'{"query_fusion":true,"progress_mode":"simple"}');
  prefs.setAccount('a');assert.equal(prefs.autoExplore,false);
  prefs.setAutoExplore(true);assert.deepEqual(prefs.requestFields(),{query_fusion:true,auto_explore:true});
  assert.equal(prefs.progressMode,'simple');assert.deepEqual(changes[0].changed,['autoExplore']);
  prefs.setQueryFusion(false);prefs.setProgressMode('detailed');assert.deepEqual(prefs.requestFields(),{auto_explore:true});
  prefs.setAccount('b');assert.deepEqual(prefs.requestFields(),{});
  prefs.setAccount('a');assert.equal(prefs.autoExplore,true);
  prefs.setAutoExplore('true');assert.deepEqual(prefs.requestFields(),{});
  prefs.setAccount(null);assert.equal(prefs.autoExplore,false);
});

test('storage synchronization isolates exploration and rejects malformed or truthy opt-ins',()=>{
  const store=storage(),changes=[],prefs=createGenerationPreferences({storage:store,onChange:change=>changes.push(change)});
  prefs.setAccount('a');
  for(const raw of ['true','null','broken','{"auto_explore":"true"}','{"auto_explore":1}']) {
    store.setItem(generationPreferencesKey('a'),raw);prefs.syncStorage(generationPreferencesKey('a'));assert.equal(prefs.autoExplore,false);
  }
  store.setItem(generationPreferencesKey('b'),'{"auto_explore":true}');prefs.syncStorage(generationPreferencesKey('b'));assert.equal(prefs.autoExplore,false);
  store.setItem(generationPreferencesKey('a'),'{"auto_explore":true}');prefs.syncStorage(generationPreferencesKey('a'));
  assert.equal(prefs.autoExplore,true);assert.deepEqual(changes[0].changed,['autoExplore']);
  store.setItem(generationPreferencesKey('a'),'{}');prefs.syncStorage(null);assert.equal(prefs.autoExplore,false);
});

test('no-material generation opens only with exploration and required model connections',()=>{
  const request={course:'course',status:configured};
  assert.equal(generationEligibility(request).allowed,false);
  assert.equal(generationEligibility({...request,autoExplore:true}).allowed,true);
  assert.equal(generationEligibility({...request,autoExplore:'true'}).allowed,false);
  assert.equal(generationEligibility({...request,course:'',autoExplore:true}).allowed,false);
  assert.equal(generationEligibility({...request,autoExplore:true,status:{...configured,auto_exploration:{available:false}}}).allowed,false);
  for(const capability of ['text','embedding']) {
    const status=structuredClone(configured);status.capabilities[capability].configured=false;
    assert.equal(generationEligibility({...request,autoExplore:true,status}).allowed,false);
  }
});

test('exploration never repairs invalid explicit source scope; disabled, deleted and stale sources do not count',()=>{
  const request={course:'course',status:configured,documents:[{id:'one',index_current:true}]};
  assert.equal(generationEligibility(request).allowed,true);
  for(const patch of [{enabled:false},{deleted_at:'today'},{index_current:false}]) {
    const documents=[{...request.documents[0],...patch}];
    assert.equal(generationEligibility({...request,documents}).allowed,false);
    assert.equal(generationEligibility({...request,documents,autoExplore:true,documentIds:['one']}).allowed,false);
  }
  assert.equal(generationEligibility({...request,autoExplore:true,documentIds:['missing']}).allowed,false);
});

test('unavailable exploration blocks its opted-in request even with ready local sources',()=>{
  const request={course:'course',status:{...configured,auto_exploration:{available:false}},documents:[{id:'one',index_current:true}]};
  assert.equal(generationEligibility({...request,autoExplore:true}).allowed,false);
  assert.equal(generationEligibility({...request,autoExplore:false}).allowed,true);
  assert.equal(generationEligibility({...request,autoExplore:true,status:{capabilities:configured.capabilities}}).allowed,false);
});

test('switching exploration invalidates an in-flight preparation without changing the submitted draft',async()=>{
  let resolve;const reply=new Promise(done=>resolve=done),calls=[];
  const flow=createGenerationFlow({request:async(path,method,body)=>{calls.push({path,body});return reply;}});
  const prefs=createGenerationPreferences({storage:storage(),onChange:()=>flow.cancel()});prefs.setAccount('a');
  prefs.setAutoExplore(true);
  const draft={topic:'Explain process scheduling',course_id:'course',request_key:'first'};
  const pending=flow.start({...draft,...prefs.requestFields()});
  prefs.setAutoExplore(false);
  resolve({preparation_id:'a'.repeat(32),status:'ready',options:[],expires_at:1900000000});await pending;
  assert.equal(flow.state.phase,'cancelled');assert.equal(calls.length,1);assert.equal(calls[0].body.auto_explore,true);
  assert.equal(draft.auto_explore,undefined);
});

test('source provenance links retain their origin and escape untrusted names, including nested metadata',()=>{
  assert.equal(renderExternalSource({name:'upload.pdf'}),'');
  const html=renderExternalSource({external_source:{url:'https://example.edu/book?x=1&y=2',title:'" ><script>alert(1)</script>',provider:'<script>provider</script>'}});
  assert.match(html,/自动发现/);assert.match(html,/href="https:\/\/example.edu\/book\?x=1&amp;y=2"/);
  assert.match(html,/rel="noopener noreferrer"/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|provider<\/script>/);
  assert.match(renderExternalSource({metadata:{external_source:{url:'https://example.edu'}}}),/example.edu/);
  for(const url of ['javascript:alert(1)','data:text/html,hello','file:///tmp/test','https://user:secret@example.edu','/relative','broken']) {
    const unsafe=renderExternalSource({external_source:{url}});assert.match(unsafe,/自动发现/);assert.doesNotMatch(unsafe,/href=/);
  }
});

test('settings and provenance localize while provider names use a closed display mapping',()=>{
  try {
    document.documentElement.lang='en';
    const html=renderSettings(true,'detailed',true);
    assert.match(html,/Auto exploration/);assert.match(html,/role="switch" id="auto-explore" checked/);assert.doesNotMatch(html,/<small|hint/);
    assert.match(renderModelConnections({},configured.auto_exploration),/Public course directory/);
    assert.match(renderModelConnections({},{provider:'brave',available:false}),/Not configured/);
    assert.doesNotMatch(renderModelConnections({},{provider:'<script>x</script>'}),/<script>/);
    assert.match(renderExternalSource({external_source:{url:'https://example.edu'}}),/Discovered source/);
  } finally {document.documentElement.lang='zh-CN';}
});

test('exploration progress uses recorded times and closed structured events in both modes',()=>{
  const job={id:'one',status:'running',timeline:{recorded:true,active_stage:'exploration',activity:'searching_sources',elapsed_ms:4123,timing_complete:true,stages:[{id:'exploration',kind:'exploration',status:'active',elapsed_ms:4123,timing_complete:true}],events:[{id:'e1',stage:'exploration',code:'exploration_search',data:{round:1,queries:2,reasoning:'NEVER DISPLAY'}}]}};
  const view=generationProgressView(job);assert.equal(view.title,'正在查找参考资料');assert.equal(view.stages[0].label,'探索资料');assert.equal(view.stages[0].elapsed,4123);
  for(const mode of ['simple','detailed']) {const html=renderGenerationProgress(job,{mode});assert.match(html,/探索资料/);assert.doesNotMatch(html,/NEVER DISPLAY/);assert.match(html,/0:04/);}
  assert.match(renderGenerationProgress(job,{mode:'detailed'}),/data-scene-kind="exploration"/);
  try {document.documentElement.lang='en';assert.match(renderGenerationProgress(job,{mode:'detailed'}),/Round 1: searching 2 source queries/);} finally {document.documentElement.lang='zh-CN';}
  for(const code of ['exploration_source_accepted','exploration_complete','exploration_stopped']) {
    assert.equal(eventMessage({code,data:{accepted:'5'}}),null);
    assert.equal(eventMessage({code,data:{accepted:-1}}),null);
    assert.deepEqual(eventMessage({code,data:{accepted:2,reasoning:'NEVER DISPLAY'}}).values,{n:2});
  }
  assert.equal(eventMessage({code:'exploration_search',data:{round:1,queries:'2'}}),null);
});
