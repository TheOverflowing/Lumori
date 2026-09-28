import test from 'node:test';
import assert from 'node:assert/strict';
import {createDraftStore, createDraftAutosave, draftKey} from '../app/static/drafts.js';

const context = {kind:'generate', accountId:'account-a', courseId:'course-a'};
function memoryStorage() {
  const values = new Map(), writes = [];
  return {values, writes, getItem:key => values.get(key) ?? null, setItem(key, value) { values.set(key, value); writes.push(key); }, removeItem:key => values.delete(key)};
}
function clock() {
  let time = 0, serial = 0;
  const timers = new Map();
  return {
    setTimeout(fn, delay) { const id = ++serial; timers.set(id, {fn, at:time + delay}); return id; },
    clearTimeout:id => timers.delete(id),
    tick(ms) { time += ms; for (const [id, timer] of [...timers]) if (timer.at <= time) { timers.delete(id); timer.fn(); } },
    get count() { return timers.size; },
  };
}
function fixture(options = {}) {
  const storage = memoryStorage();
  const store = createDraftStore({storage, now:() => 1000});
  const timers = clock(), pageTarget = new EventTarget(), documentTarget = new EventTarget(), statuses = [];
  documentTarget.hidden = false;
  const autosave = createDraftAutosave({store, context, ...timers, pageTarget, documentTarget, onStatus:status => statuses.push(status.state), ...options});
  return {storage, store, timers, pageTarget, documentTarget, statuses, autosave};
}

test('drafts persist across stores, isolated by account, course, content and base version', () => {
  const storage = memoryStorage(), store = createDraftStore({storage, now:() => 1000});
  const scopes = [context, {...context, accountId:'account-b'}, {...context, courseId:'course-b'},
    {...context, kind:'edit', contentId:'content-a', baseVersion:1},
    {...context, kind:'edit', contentId:'content-a', baseVersion:2},
    {...context, kind:'edit', contentId:'content-b', baseVersion:1}];
  scopes.forEach((scope, n) => assert.equal(store.save(scope, {objective:`Draft ${n}`}).state, 'saved'));
  const reloaded = createDraftStore({storage, now:() => 1001});
  scopes.forEach((scope, n) => assert.deepEqual(reloaded.load(scope), {data:{objective:`Draft ${n}`}, savedAt:1000, persisted:true}));
  assert.equal(new Set(scopes.map(draftKey)).size, scopes.length);
});

test('missing identity, unknown kinds and invalid edit versions never touch storage', () => {
  const storage = {getItem() {assert.fail();}, setItem() {assert.fail();}, removeItem() {assert.fail();}};
  const store = createDraftStore({storage});
  for (const scope of [null, {}, {...context, accountId:''}, {...context, courseId:null}, {...context, kind:'settings'},
    {...context, kind:'edit', contentId:'c', baseVersion:0}, {...context, kind:'edit', contentId:'c', baseVersion:'1'}]) {
    assert.equal(draftKey(scope), null);
    assert.equal(store.load(scope), null);
    assert.equal(store.save(scope, {objective:'private'}).state, 'disabled');
    assert.equal(store.remove(scope).state, 'disabled');
  }
});

test('JSON punctuation in identifiers cannot collide with another context', () => {
  assert.notEqual(draftKey({...context, accountId:'a:b', courseId:'c'}), draftKey({...context, accountId:'a', courseId:'b:c'}));
});

test('save and load snapshot data instead of retaining mutable form objects', () => {
  const store = createDraftStore({storage:memoryStorage()});
  const draft = {objective:'first', selected:['doc-a']};
  store.save(context, draft);
  draft.selected.push('doc-b');
  const loaded = store.load(context);
  loaded.data.selected.push('doc-c');
  assert.deepEqual(store.load(context).data.selected, ['doc-a']);
});

test('null and empty drafts clear rather than restore blank records', () => {
  const storage = memoryStorage(), store = createDraftStore({storage});
  for (const empty of [null, {}, []]) {
    store.save(context, {objective:'previous'});
    assert.equal(store.save(context, empty).state, 'empty');
    assert.equal(store.load(context), null);
    assert.equal(storage.values.has(draftKey(context)), false);
  }
});

test('malformed, expired, future, incompatible and oversized stored drafts are ignored', () => {
  const storage = memoryStorage(), store = createDraftStore({storage, now:() => 1000, maxBytes:40, maxAgeMs:500});
  for (const raw of ['broken json', 'null', JSON.stringify({schema:2, savedAt:1000, data:{a:1}}),
    JSON.stringify({schema:1, savedAt:499, data:{a:1}}), JSON.stringify({schema:1, savedAt:62000, data:{a:1}}),
    JSON.stringify({schema:1, savedAt:1000, data:{a:'x'.repeat(400)}})]) {
    storage.values.set(draftKey(context), raw);
    assert.equal(store.load(context), null);
  }
});

test('invalid JSON values and credentials do not replace a good draft', () => {
  const storage = memoryStorage(), store = createDraftStore({storage, maxBytes:256});
  store.save(context, {objective:'safe'});
  const circular = {}; circular.self = circular;
  for (const invalid of [{a:undefined}, {a:NaN}, {a:new Date()}, circular,
    {settings:{api_key:'never-store-me'}}, {password:'never-store-me'}, JSON.parse('{"__proto__":{"a":1}}')]) {
    assert.equal(store.save(context, invalid).state, 'invalid');
    assert.deepEqual(store.load(context).data, {objective:'safe'});
  }
  assert.equal([...storage.values.values()].join('').includes('never-store-me'), false);
});

test('the byte limit includes UTF-8 data and envelope; oversized work stays in memory without a storage write', () => {
  const storage = memoryStorage();
  const record = {schema:1, savedAt:1000, data:{objective:'中文'}};
  const exactBytes = new TextEncoder().encode(JSON.stringify(record)).byteLength;
  const within = createDraftStore({storage, now:() => 1000, maxBytes:exactBytes});
  assert.equal(within.save(context, record.data).state, 'saved');
  assert.equal(storage.writes.length, 1);
  const smaller = createDraftStore({storage, now:() => 1000, maxBytes:exactBytes - 1});
  assert.equal(smaller.save(context, record.data).state, 'memory');
  assert.equal(storage.writes.length, 1, 'oversized drafts never attempt localStorage.setItem');
  assert.deepEqual(smaller.load(context), {data:record.data, savedAt:1000, persisted:false});
});

test('oversized editor drafts survive controller teardown and restoration in the same store', () => {
  const storage = memoryStorage(), store = createDraftStore({storage, now:() => 1000});
  const scope = {...context, kind:'edit', contentId:'content-a', baseVersion:1};
  const states = [];
  const first = createDraftAutosave({store, context:scope, onStatus:status => states.push(status.state)});
  const values = {title:'Large lesson', 'sections.0.text':'中'.repeat(180000)};
  first.schedule(values);
  first.destroy();
  assert.deepEqual(states, ['pending', 'memory']);
  assert.equal(storage.writes.length, 0);
  const restored = store.load(scope);
  assert.deepEqual(restored, {data:values, savedAt:1000, persisted:false});
  assert.equal(store.load({...scope, baseVersion:2}), null);
  assert.equal(store.load({...scope, accountId:'other-account'}), null);
  const second = createDraftAutosave({store, context:scope});
  second.schedule({...restored.data, title:'Continue editing'});
  second.destroy();
  assert.equal(store.load(scope).data.title, 'Continue editing');
  assert.equal(createDraftStore({storage}).load(scope), null, 'memory status does not promise reload persistence');
});

test('shrinking a memory-only draft persists the latest data and removes the volatile override', () => {
  const storage = memoryStorage(), store = createDraftStore({storage, maxBytes:128});
  assert.equal(store.save(context, {objective:'x'.repeat(200)}).state, 'memory');
  assert.equal(store.save(context, {objective:'short'}).state, 'saved');
  assert.equal(store.load(context).persisted, true);
  assert.equal(createDraftStore({storage}).load(context).data.objective, 'short');
});

test('storage denial keeps a scoped in-memory draft and reports that it will not survive reload', () => {
  const denied = {getItem() {throw Error('denied');}, setItem() {throw Error('denied');}, removeItem() {throw Error('denied');}};
  const store = createDraftStore({storage:denied, now:() => 1000});
  assert.equal(store.save(context, {objective:'keep this'}).state, 'memory');
  assert.deepEqual(store.load(context), {data:{objective:'keep this'}, savedAt:1000, persisted:false});
  assert.equal(store.load({...context, accountId:'other'}), null);
  store.remove(context);
  assert.equal(store.load(context), null);
});

test('failed updates and deletions cannot resurrect a stale persisted draft', () => {
  const storage = memoryStorage(), store = createDraftStore({storage});
  store.save(context, {objective:'stale'});
  storage.setItem = () => {throw Error('full');};
  storage.removeItem = () => {throw Error('denied');};
  store.save(context, {objective:'current'});
  assert.equal(store.load(context).data.objective, 'current');
  store.remove(context);
  assert.equal(store.load(context), null);
});

test('autosave debounces edits and snapshots values when scheduled', () => {
  const {autosave, timers, store, storage, statuses} = fixture();
  autosave.schedule({objective:'first'});
  timers.tick(399);
  assert.equal(storage.writes.length, 0);
  const draft = {objective:'second'};
  autosave.schedule(draft);
  draft.objective = 'later mutation';
  timers.tick(399);
  assert.equal(storage.writes.length, 0);
  timers.tick(1);
  assert.equal(store.load(context).data.objective, 'second');
  assert.equal(storage.writes.length, 1);
  assert.deepEqual(statuses, ['pending', 'pending', 'saved']);
  assert.equal(autosave.pending, false);
  autosave.destroy();
});

test('pagehide and hidden visibility flush synchronously without duplicate writes', () => {
  const {autosave, timers, pageTarget, documentTarget, storage} = fixture();
  autosave.schedule({objective:'first'});
  pageTarget.dispatchEvent(new Event('pagehide'));
  assert.equal(storage.writes.length, 1);
  timers.tick(500);
  assert.equal(storage.writes.length, 1);
  autosave.schedule({objective:'second'});
  documentTarget.dispatchEvent(new Event('visibilitychange'));
  assert.equal(storage.writes.length, 1, 'visible pages do not force saves');
  documentTarget.hidden = true;
  documentTarget.dispatchEvent(new Event('visibilitychange'));
  assert.equal(storage.writes.length, 2);
  autosave.destroy();
});

test('destroy flushes by default, detaches lifecycle listeners and stops future work', () => {
  const {autosave, timers, pageTarget, documentTarget, storage} = fixture();
  autosave.schedule({objective:'last edit'});
  autosave.destroy();
  assert.equal(storage.writes.length, 1);
  assert.equal(timers.count, 0);
  autosave.schedule({objective:'ignored'});
  pageTarget.dispatchEvent(new Event('pagehide'));
  documentTarget.hidden = true;
  documentTarget.dispatchEvent(new Event('visibilitychange'));
  autosave.destroy();
  assert.equal(storage.writes.length, 1);
});

test('cancel, clear and destroy without flush cannot save delayed values', () => {
  const {autosave, timers, store} = fixture();
  autosave.schedule({objective:'cancelled'});
  autosave.cancel();
  timers.tick(1000);
  assert.equal(store.load(context), null);
  autosave.schedule({objective:'saved'});
  autosave.flush();
  autosave.schedule({objective:'stale delayed'});
  autosave.clear();
  timers.tick(1000);
  assert.equal(store.load(context), null);
  autosave.schedule({objective:'discarded on close'});
  autosave.destroy({flush:false});
  timers.tick(1000);
  assert.equal(store.load(context), null);
});

test('a changed account/course object cannot redirect a delayed write', () => {
  const scope = {...context};
  const {autosave, timers, store} = fixture({context:scope});
  autosave.schedule({objective:'belongs to a'});
  scope.accountId = 'account-b';
  scope.courseId = 'course-b';
  timers.tick(400);
  assert.equal(store.load(context).data.objective, 'belongs to a');
  assert.equal(store.load(scope), null);
  autosave.destroy();
});

test('invalid autosave cancels an earlier pending snapshot and reports a recoverable failure', () => {
  const {autosave, timers, store, statuses} = fixture();
  autosave.schedule({objective:'earlier'});
  autosave.schedule({password:'no'});
  timers.tick(400);
  assert.equal(store.load(context), null);
  assert.deepEqual(statuses, ['pending', 'invalid']);
  autosave.schedule({objective:'safe edit'});
  autosave.flush();
  assert.equal(store.load(context).data.objective, 'safe edit');
  autosave.destroy();
});
