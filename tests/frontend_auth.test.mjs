import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createSessionClient, isSessionChange, accountCourseKey} from '../app/static/auth.js';
import {english} from '../app/static/translations.js';
const userA = {id:'user-a',email:'a@example.test',display_name:'A'};
const userB = {id:'user-b',email:'b@example.test',display_name:'B'};
const session = user => ({user,csrf_token:`csrf-${user.id}`});
const response = (body, status = 200) => ({ok:status>=200&&status<300,status,json:async()=>body});
const deferred = () => { let resolve; const promise = new Promise(done=>resolve=done); return {promise,resolve}; };

function clientWith(queue, options = {}) {
  const requests = [];
  const client = createSessionClient({...options,fetchImpl:(url,request)=>{
    requests.push({url,...request});
    const next = queue.shift();
    if (!next) throw new Error(`Unexpected request ${url}`);
    return typeof next === 'function' ? next(url,request) : Promise.resolve(next);
  }});
  return {client,requests};
}

test('signed-out client refuses protected reads and mutations without contacting server', async () => {
  const {client,requests} = clientWith([]);
  await assert.rejects(client.request('/courses'),isSessionChange);
  await assert.rejects(client.request('/courses','POST',{name:'secret'}),isSessionChange);
  assert.equal(requests.length,0);
});

test('binary exports retain account scope and decode safe attachment filenames', async () => {
  const bytes = new Blob(['%PDF-1.4'],{type:'application/pdf'});
  const {client,requests} = clientWith([response(session(userA)),{
    ok:true,status:200,blob:async()=>bytes,
    headers:new Headers({'Content-Disposition':"attachment; filename*=UTF-8''%E5%AD%A6%E4%B9%A0.pdf"}),
    json:()=>{throw new Error('Binary download must not consume JSON');},
  }]);
  await client.restore();
  const result=await client.request('/contents/own/export?format=pdf','GET',undefined,{responseType:'blob'});
  assert.equal(result.blob,bytes); assert.equal(result.filename,'学习.pdf');
  assert.equal(requests[1].headers['X-Account-ID'],userA.id);
});

test('binary download is discarded if its body arrives after an account switch', async () => {
  const body=deferred();
  const {client}=clientWith([response(session(userA)),{ok:true,status:200,blob:()=>body.promise},response(session(userB))]);
  await client.restore(); const download=client.request('/contents/own/export','GET',undefined,{responseType:'blob'});
  await Promise.resolve(); await client.authenticate('login',{email:userB.email,password:'test password'});
  body.resolve(new Blob(['private file']));
  await assert.rejects(download,isSessionChange);
});

test('canceling one document transfer aborts its transport without changing the account or other requests', async () => {
  const active=deferred(), other=deferred();
  const {client,requests}=clientWith([
    response(session(userA)),
    (_url,options)=>{options.signal.addEventListener('abort',()=>active.resolve({ok:true,blob:async()=>{throw new DOMException('User canceled','AbortError');}}),{once:true});return active.promise;},
    ()=>other.promise,
  ]);
  await client.restore();
  const epoch=client.epoch, controller=new AbortController();
  const download=client.request('/contents/own/export','GET',undefined,{responseType:'blob',signal:controller.signal});
  const courses=client.request('/courses');
  controller.abort();
  await assert.rejects(download,error=>error.name==='AbortError' && !isSessionChange(error));
  assert.equal(requests[1].signal.aborted,true);
  assert.equal(requests[2].signal.aborted,false);
  assert.equal(client.epoch,epoch);
  assert.equal(client.session.user.id,userA.id);
  other.resolve(response([{id:'course'}]));
  assert.deepEqual(await courses,[{id:'course'}]);
});

test('a binary body interruption without account invalidation is not misclassified as a session change', async () => {
  const {client}=clientWith([
    response(session(userA)),
    {ok:true,status:200,blob:async()=>{throw new DOMException('Body interrupted','AbortError');}},
  ]);
  await client.restore();
  const epoch=client.epoch;
  await assert.rejects(client.request('/contents/own/export','GET',undefined,{responseType:'blob'}),
    error=>error.name==='AbortError' && !isSessionChange(error));
  assert.equal(client.epoch,epoch);
  assert.equal(client.session.user.id,userA.id);
});

test('caller abort listeners are removed after a successful document transfer', async () => {
  const controller=new AbortController();
  const signal=controller.signal;
  let added=0, removed=0;
  const add=signal.addEventListener.bind(signal), remove=signal.removeEventListener.bind(signal);
  signal.addEventListener=(name,...args)=>{if(name==='abort')added++;return add(name,...args);};
  signal.removeEventListener=(name,...args)=>{if(name==='abort')removed++;return remove(name,...args);};
  const {client,requests}=clientWith([
    response(session(userA)),
    {ok:true,status:200,blob:async()=>new Blob(['file']),headers:new Headers()},
  ]);
  await client.restore();
  await client.request('/contents/own/export','GET',undefined,{responseType:'blob',signal});
  assert.equal(added,1);
  assert.equal(removed,1);
  controller.abort();
  assert.equal(requests[1].signal.aborted,false);
});

test('restored account scopes every fetch and sends CSRF only on mutations', async () => {
  const {client,requests} = clientWith([response(session(userA)),response([]),response({id:'course'})]);
  await client.restore(); await client.request('/courses'); await client.request('/courses','POST',{name:'Course'});
  assert.equal(requests[0].headers['X-Account-ID'],undefined);
  assert.equal(requests[1].headers['X-Account-ID'],userA.id);
  assert.equal(requests[1].headers['X-CSRF-Token'],undefined);
  assert.equal(requests[2].headers['X-CSRF-Token'],'csrf-user-a');
  assert.equal(requests[2].headers['Content-Type'],'application/json');
  for (const request of requests) { assert.equal(request.credentials,'same-origin'); assert.equal(request.cache,'no-store'); }
});

test('multipart uploads carry account and CSRF but let browser set the boundary', async () => {
  const {client,requests} = clientWith([response(session(userA)),response({id:'doc'})]);
  await client.restore(); const data = new FormData(); data.set('course_id','own-course');
  await client.request('/documents','POST',data);
  assert.equal(requests[1].body,data);
  assert.equal(requests[1].headers['Content-Type'],undefined);
  assert.equal(requests[1].headers['X-CSRF-Token'],'csrf-user-a');
});

test('account change aborts reads and rejects late responses even if transport ignores abort', async () => {
  const pending = deferred(); const resets = [];
  const {client,requests} = clientWith([response(session(userA)),()=>pending.promise,response(session(userB))],{onReset:reason=>resets.push(reason)});
  await client.restore(); const old = client.request('/contents');
  await client.authenticate('login',{email:userB.email,password:'a long test password'});
  assert.equal(requests[1].signal.aborted,true);
  pending.resolve(response([{title:'Private A lesson'}]));
  await assert.rejects(old,isSessionChange);
  assert.equal(client.session.user.id,userB.id);
  assert.equal(resets.length,2);
});

test('a response body that finishes after account switch is discarded', async () => {
  const body = deferred();
  const {client} = clientWith([response(session(userA)),{ok:true,status:200,json:()=>body.promise},response(session(userB))]);
  await client.restore(); const old = client.request('/contents'); await Promise.resolve();
  await client.authenticate('login',{email:userB.email,password:'a long test password'});
  body.resolve([{title:'Private A lesson'}]); await assert.rejects(old,isSessionChange);
});

test('protected401 clears account and old requests, while invalid login401 remains a form error', async () => {
  const resets = [];
  const {client} = clientWith([response(session(userA)),response({detail:'Login required'},401),response({code:'invalid_credentials'},401)],{onReset:reason=>resets.push(reason)});
  await client.restore(); await assert.rejects(client.request('/jobs'),isSessionChange);
  assert.equal(client.session.user,null); assert.equal(client.session.csrf_token,null);
  assert.equal(resets.at(-1),'expired');
  await assert.rejects(client.authenticate('login',{email:userA.email,password:'wrong'}),error=>!isSessionChange(error)&&error.message==='邮箱或密码不正确。');
});

test('logout clears visible state before network finishes and leaves no tokens', async () => {
  const pending = deferred(), resets = [];
  const {client,requests} = clientWith([response(session(userA)),()=>pending.promise],{onReset:reason=>resets.push(reason)});
  await client.restore(); const logout = client.logout();
  assert.equal(resets.at(-1),'logout');
  assert.equal(requests[1].headers['X-CSRF-Token'],'csrf-user-a');
  pending.resolve(response({ok:true})); await logout;
  assert.equal(client.session.user,null); assert.equal(client.session.csrf_token,null);
  await assert.rejects(client.request('/courses'),isSessionChange);
});

test('failed logout lets caller rebuild existing session without claiming it ended', async () => {
  const seen = [];
  const {client} = clientWith([response(session(userA)),response({detail:'Unavailable'},503)],{onSession:(value,meta)=>seen.push({value,meta})});
  await client.restore(); await assert.rejects(client.logout(),/Unavailable/);
  assert.equal(client.session.user.id,userA.id); assert.equal(seen.at(-1).meta.changed,true);
});

test('cross-tab suspension discards late payload and restore detects cookie identity change', async () => {
  const pending = deferred();
  const {client} = clientWith([response(session(userA)),()=>pending.promise,response(session(userB))]);
  await client.restore(); const old = client.request('/jobs');
  client.suspend('checking'); await client.restore();
  pending.resolve(response([{id:'private-a-job'}])); await assert.rejects(old,isSessionChange);
  assert.equal(client.session.user.id,userB.id);
});

test('same-account session checks preserve in-flight work and retain current CSRF token', async () => {
  const pending = deferred();
  const {client,requests} = clientWith([response(session(userA)),()=>pending.promise,response(session(userA))]);
  await client.restore(); const old = client.request('/jobs'), epoch=client.epoch;
  await client.restore(); assert.equal(client.epoch,epoch); assert.equal(requests[1].signal.aborted,false);
  pending.resolve(response([{id:'own-job'}])); assert.deepEqual(await old,[{id:'own-job'}]);
});

test('account course selection has distinct keys and cannot fall back to the old shared key', () => {
  assert.notEqual(accountCourseKey(userA.id),accountCourseKey(userB.id));
  assert.equal(accountCourseKey(null),null);
  assert.equal(accountCourseKey('a/b'),'zhixu.account.a%2Fb.course');
});

test('auth markup starts gated and provides localized accessible form and secret-free storage', async () => {
  const html = await readFile(new URL('../app/static/index.html',import.meta.url),'utf8');
  const app = await readFile(new URL('../app/static/app.js',import.meta.url),'utf8');
  assert.match(html,/class="app-shell" hidden/);
  assert.match(html,/id="auth-gate"[^>]+aria-labelledby="auth-title"/);
  assert.match(app,/autocomplete="username"/);
  assert.match(app,/new-password/); assert.match(app,/current-password/);
  assert.match(app,/minlength="15"/); assert.match(app,/maxlength="128"/);
  assert.doesNotMatch(app,/localStorage\.(?:getItem|setItem)\(['"]zhixu\.course/);
  assert.doesNotMatch(app,/localStorage\.setItem\([^\n]*(?:password|csrf_token|pendingRestore)/);
  assert.match(app,/history\.replaceState\(null, '', location\.pathname \+ location\.search \+ '#home'\)/);
  for (const key of ['登录知序','创建你的账号','邮箱','确认密码','退出登录','有一份原有工作空间等待恢复，请使用对应邮箱登录或注册。']) assert.ok(english[key]);
});
