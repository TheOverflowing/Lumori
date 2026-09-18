import test from 'node:test';
import assert from 'node:assert/strict';
import { watchResource } from '../app/static/live-updates.js';

class Visibility extends EventTarget {
  hidden = false;
  change(hidden) {
    this.hidden = hidden;
    this.dispatchEvent(new Event('visibilitychange'));
  }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function advance(context, milliseconds = 0) {
  context.mock.timers.tick(milliseconds);
  await Promise.resolve();
}

function watch(context, options = {}) {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  const updates = [], errors = [];
  const dispose = watchResource({
    interval: 100, visibility,
    update: value => updates.push(value),
    error: cause => errors.push(cause),
    ...options,
  });
  context.after(dispose);
  return { visibility, updates, errors, dispose };
}

test('polling updates only changed payloads and respects the initial snapshot', async context => {
  let loads = 0;
  let current = [{ id: 'material', status: 'draft' }];
  const observer = watch(context, {
    initial: current,
    load: async () => { loads++; return structuredClone(current); },
  });
  await advance(context, 99);
  assert.equal(loads, 0);
  await advance(context, 1);
  assert.equal(loads, 1);
  assert.deepEqual(observer.updates, [], 'equal freshly allocated data must not repaint');

  current = [...current, { id: 'new-material', status: 'draft' }];
  await advance(context, 100);
  assert.deepEqual(observer.updates, [current]);
  await advance(context, 100);
  assert.equal(loads, 3);
  assert.equal(observer.updates.length, 1);
  assert.deepEqual(observer.errors, [null, null, null]);
});

test('hidden pages skip reads and refresh immediately when they become visible', async context => {
  let loads = 0;
  const observer = watch(context, { load: async () => ++loads });
  observer.visibility.change(true);
  await advance(context, 100);
  await advance(context, 100);
  assert.equal(loads, 0);

  observer.visibility.change(false);
  await advance(context);
  assert.deepEqual(observer.updates, [1]);
  await advance(context, 99);
  assert.equal(loads, 1);
  await advance(context, 1);
  assert.deepEqual(observer.updates, [1, 2]);
});

test('slow reads never overlap despite repeated visibility changes', async context => {
  const pending = deferred();
  let loads = 0;
  const observer = watch(context, {
    load: () => { loads++; return loads === 1 ? pending.promise : Promise.resolve('second'); },
  });
  await advance(context, 100);
  assert.equal(loads, 1);
  observer.visibility.change(true);
  observer.visibility.change(false);
  observer.visibility.change(false);
  await advance(context, 1000);
  assert.equal(loads, 1, 'visibility events must not start parallel requests');

  pending.resolve('first');
  await advance(context);
  assert.deepEqual(observer.updates, ['first']);
  await advance(context, 99);
  assert.equal(loads, 1);
  await advance(context, 1);
  assert.equal(loads, 2, 'the next interval starts after the previous read completes');
  assert.deepEqual(observer.updates, ['first', 'second']);
});

test('disposing before the first interval cancels timers and visibility refreshes', async context => {
  let loads = 0;
  const observer = watch(context, { load: async () => ++loads });
  observer.dispose();
  observer.dispose();
  observer.visibility.change(false);
  await advance(context, 1000);
  assert.equal(loads, 0);
  assert.deepEqual(observer.updates, []);
  assert.deepEqual(observer.errors, []);
});

test('a response from the previous page cannot update the new view after disposal', async context => {
  const pending = deferred();
  let loads = 0;
  const observer = watch(context, {
    load: () => { loads++; return pending.promise; },
  });
  await advance(context, 100);
  observer.dispose();
  pending.resolve([{ id: 'old-course-material' }]);
  await advance(context);
  observer.visibility.change(false);
  await advance(context, 1000);
  assert.equal(loads, 1);
  assert.deepEqual(observer.updates, []);
  assert.deepEqual(observer.errors, [], 'a stale success must not clear errors in another view');
});

test('a failed response after disposal does not report a stale error or restart polling', async context => {
  const pending = deferred();
  let loads = 0;
  const observer = watch(context, {
    load: () => { loads++; return pending.promise; },
  });
  await advance(context, 100);
  observer.dispose();
  pending.reject(new Error('Old page request failed'));
  await advance(context);
  await advance(context, 1000);
  assert.equal(loads, 1);
  assert.deepEqual(observer.updates, []);
  assert.deepEqual(observer.errors, []);
});

test('an update that leaves the page does not clear errors on the next page', async context => {
  let loads = 0;
  const observer = watch(context, {
    load: async () => ++loads,
    update: () => observer.dispose(),
  });
  await advance(context, 100);
  await advance(context, 1000);
  assert.equal(loads, 1);
  assert.deepEqual(observer.errors, []);
});

test('temporary failures are reported and recovery continues even when data is unchanged', async context => {
  const failure = new Error('Temporarily offline');
  let loads = 0;
  const observer = watch(context, {
    initial: [],
    load: async () => {
      loads++;
      if (loads === 1) throw failure;
      return loads === 2 ? [] : [{ id: 'finished-material' }];
    },
  });
  await advance(context, 100);
  assert.deepEqual(observer.errors, [failure]);
  assert.deepEqual(observer.updates, []);
  await advance(context, 100);
  assert.deepEqual(observer.errors, [failure, null], 'recovery clears the error without needing changed data');
  assert.deepEqual(observer.updates, []);
  await advance(context, 100);
  assert.equal(loads, 3);
  assert.deepEqual(observer.updates, [[{ id: 'finished-material' }]]);
  assert.deepEqual(observer.errors, [failure, null, null]);
});
