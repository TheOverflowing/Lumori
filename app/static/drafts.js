/* Browser-local drafts. The caller supplies a whitelist of form fields. */
const schema = 1;
const prefix = 'lumori.draft.v1:';
const defaultMaxBytes = 512 * 1024;
const defaultMaxAge = 30 * 24 * 60 * 60 * 1000;
const forbiddenFields = new Set(['apikey', 'password', 'accesstoken', 'refreshtoken', 'authorization', 'credentials', 'credential', 'secret', 'clientsecret']);

function identifier(value) {
  return typeof value === 'string' && value.trim() && value.length <= 512 ? value : null;
}

/** No implicit current-user or current-course lookup: every key is fully scoped. */
export function draftKey(context) {
  if (!context || !identifier(context.accountId) || !identifier(context.courseId)) return null;
  const parts = [context.kind, context.accountId, context.courseId];
  if (context.kind === 'edit') {
    if (!identifier(context.contentId) || !Number.isSafeInteger(context.baseVersion) || context.baseVersion < 1) return null;
    parts.push(context.contentId, context.baseVersion);
  } else if (context.kind !== 'generate') return null;
  return prefix + JSON.stringify(parts);
}

function snapshot(data) {
  if (data == null) return null;
  if (typeof data !== 'object') throw new TypeError('Drafts must contain form data.');
  let count = 0;
  const ancestors = new Set();
  function validate(value, depth = 0) {
    if (++count > 50000 || depth > 40) throw new TypeError('Draft is too complex.');
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
    if (typeof value === 'number' && Number.isFinite(value)) return;
    if (typeof value !== 'object' || ancestors.has(value)) throw new TypeError('Draft must be valid JSON.');
    if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) throw new TypeError('Draft must contain plain data.');
    ancestors.add(value);
    for (const [key, child] of Object.entries(value)) {
      const normalized = key.replace(/[_\s-]/g, '').toLowerCase();
      if (forbiddenFields.has(normalized) || ['__proto__', 'constructor', 'prototype'].includes(key)) throw new TypeError('Credentials are not draft fields.');
      validate(child, depth + 1);
    }
    ancestors.delete(value);
  }
  validate(data);
  const json = JSON.stringify(data);
  if (json === '{}' || json === '[]') return null;
  return JSON.parse(json);
}

function browserStorage() {
  try { return globalThis.localStorage ?? null; } catch { return null; }
}

/**
 * load -> {data, savedAt, persisted} | null
 * save/remove -> {state, persisted, savedAt?}
 * A failed write remains available in this store instance, without claiming it
 * will survive a reload. No other account's draft is read to recover a failure.
 */
export function createDraftStore({storage = browserStorage(), now = Date.now, maxBytes = defaultMaxBytes, maxAgeMs = defaultMaxAge} = {}) {
  const volatile = new Map();
  const limit = Number.isFinite(maxBytes) && maxBytes > 0 ? maxBytes : defaultMaxBytes;
  const maxAge = Number.isFinite(maxAgeMs) && maxAgeMs > 0 ? maxAgeMs : defaultMaxAge;
  const validTime = time => Number.isFinite(time) && time >= 0 && time <= now() + 60000 && now() - time <= maxAge;
  function remove(context) {
    const key = draftKey(context);
    if (!key) return {state:'disabled', persisted:false};
    try {
      if (!storage) throw new Error('Storage unavailable');
      storage.removeItem(key);
      volatile.delete(key);
      return {state:'empty', persisted:true};
    } catch {
      // The tombstone prevents a failed deletion from restoring the old value.
      volatile.set(key, null);
      return {state:'empty', persisted:false};
    }
  }
  function load(context) {
    const key = draftKey(context);
    if (!key) return null;
    let record, persisted = false;
    if (volatile.has(key)) record = volatile.get(key);
    else {
      try {
        const raw = storage?.getItem(key);
        if (!raw) return null;
        if (new TextEncoder().encode(raw).byteLength > limit) throw new Error('Oversized draft');
        record = JSON.parse(raw);
        persisted = true;
      } catch { remove(context); return null; }
    }
    if (!record) return null;
    try {
      if (record.schema !== schema || !validTime(record.savedAt)) throw new Error('Expired or incompatible draft');
      const data = snapshot(record.data);
      if (data === null) throw new Error('Empty draft');
      return {data, savedAt:record.savedAt, persisted};
    } catch { remove(context); return null; }
  }
  function save(context, data) {
    const key = draftKey(context);
    if (!key) return {state:'disabled', persisted:false};
    let clean;
    try { clean = snapshot(data); }
    catch { return {state:'invalid', persisted:false}; }
    if (clean === null) return remove(context);
    const savedAt = now();
    if (!validTime(savedAt)) return {state:'invalid', persisted:false};
    const record = {schema, savedAt, data:clean};
    const serialized = JSON.stringify(record);
    // The size limit protects synchronous browser storage, not the user's work.
    // A large but valid editor draft remains recoverable when changing views.
    if (new TextEncoder().encode(serialized).byteLength > limit) {
      volatile.set(key, record);
      return {state:'memory', savedAt, persisted:false};
    }
    try {
      if (!storage) throw new Error('Storage unavailable');
      storage.setItem(key, serialized);
      volatile.delete(key);
      return {state:'saved', savedAt, persisted:true};
    } catch {
      volatile.set(key, record);
      return {state:'memory', savedAt, persisted:false};
    }
  }
  return {load, save, remove, maxBytes:limit};
}

/**
 * Each controller owns one immutable context. schedule() snapshots immediately;
 * delayed work never reads a newly selected account/course or a reused form.
 * onStatus states: pending, saved, memory, empty, invalid, disabled.
 */
export function createDraftAutosave({
  store, context, onStatus = () => {}, debounceMs = 400,
  setTimeout: scheduleTimer = globalThis.setTimeout,
  clearTimeout: cancelTimer = globalThis.clearTimeout,
  pageTarget = globalThis.window, documentTarget = globalThis.document,
} = {}) {
  if (!store?.save || !store?.remove) throw new TypeError('A draft store is required.');
  const scope = Object.freeze({...context});
  const enabled = Boolean(draftKey(scope));
  let timer = null, pending = false, data = null, destroyed = false;
  const notify = result => { onStatus(result); return result; };
  function cancel() {
    if (timer !== null) cancelTimer(timer);
    timer = null;
    pending = false;
    data = null;
  }
  function flush() {
    if (destroyed || !pending) return null;
    const next = data;
    cancel();
    return notify(store.save(scope, next));
  }
  function schedule(value) {
    if (destroyed) return null;
    cancel();
    if (!enabled) return notify({state:'disabled', persisted:false});
    try { data = snapshot(value); }
    catch { return notify({state:'invalid', persisted:false}); }
    pending = true;
    timer = scheduleTimer(flush, Math.max(0, debounceMs));
    return notify({state:'pending', persisted:false});
  }
  function clear() {
    if (destroyed) return null;
    cancel();
    return notify(store.remove(scope));
  }
  const hidden = () => { if (documentTarget?.hidden || documentTarget?.visibilityState === 'hidden') flush(); };
  pageTarget?.addEventListener('pagehide', flush);
  documentTarget?.addEventListener('visibilitychange', hidden);
  return {
    schedule, flush, cancel, clear,
    get pending() { return pending; },
    destroy({flush: savePending = true} = {}) {
      if (destroyed) return;
      if (savePending) flush();
      cancel();
      destroyed = true;
      pageTarget?.removeEventListener('pagehide', flush);
      documentTarget?.removeEventListener('visibilitychange', hidden);
    },
  };
}
