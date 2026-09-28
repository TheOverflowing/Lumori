/* List positions live only in this tab and are scoped to an account and course.
 * Capture before starting a route read; restore after the new rows are bound.
 * The saved row offset survives changes in the height of rows above it. */
const listRoutes = new Set(['contents', 'documents', 'jobs']);
const focusable = 'button, a[href], input, select, textarea, [tabindex]';
const focusAttributes = ['id', 'data-action', 'data-id', 'data-document', 'href', 'name'];

function contextKey(context) {
  if (!context?.account || !listRoutes.has(context.route)) return null;
  return JSON.stringify([String(context.account), String(context.course || ''), context.route]);
}
function rows(root) { return [...(root?.querySelectorAll('[data-row-key]') || [])]; }
function findRow(root, key) { return rows(root).find(row => row.dataset.rowKey === key); }
function scrollPosition(win) { return Math.max(0, Number(win.scrollY) || 0); }

function describeFocus(root, active) {
  if (!active || !root.contains(active)) return null;
  const attributes = Object.fromEntries(focusAttributes
    .map(name => [name, active.getAttribute(name)])
    .filter(([, value]) => value !== null && value !== ''));
  if (!Object.keys(attributes).length) return null;
  const row = active.closest('[data-row-key]');
  return {row:row && root.contains(row) ? row.dataset.rowKey : null,
    tag:active.tagName, attributes};
}
function findFocus(root, descriptor) {
  if (!descriptor) return null;
  const scope = descriptor.row ? findRow(root, descriptor.row) : root;
  if (!scope) return null;
  const matches = [...scope.querySelectorAll(focusable)].filter(control =>
    control.tagName === descriptor.tag && !control.disabled && !control.hidden &&
    Object.entries(descriptor.attributes).every(([name, value]) => control.getAttribute(name) === value));
  // Never guess between repeated controls, or focus another row after deletion.
  return matches.length === 1 ? matches[0] : null;
}

export function createViewPositions({window:win = globalThis.window} = {}) {
  const snapshots = new Map();
  let pending = null;
  function cancel() {
    if (!pending) return;
    if (pending.frame !== null) win.cancelAnimationFrame(pending.frame);
    for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
      win.removeEventListener(event, pending.interrupt, true);
    }
    pending = null;
  }
  function guard() {
    cancel();
    const navigation = {frame:null, interrupted:false, key:null};
    navigation.interrupt = () => { navigation.interrupted = true; };
    for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
      win.addEventListener(event, navigation.interrupt, {capture:true, passive:true});
    }
    pending = navigation;
    return navigation;
  }
  return {
    capture(context, root) {
      const key = contextKey(context);
      if (key && root) {
        const visible = rows(root).find(row => {
          const box = row.getBoundingClientRect();
          return box.height > 0 && box.bottom > 0 && box.top < win.innerHeight;
        });
        snapshots.set(key, {account:String(context.account), y:scrollPosition(win),
          anchor:visible ? {key:visible.dataset.rowKey, top:visible.getBoundingClientRect().top} : null,
          focus:describeFocus(root, win.document.activeElement)});
      }
      // Even a non-list route starts a navigation guard for its destination.
      guard();
      return Boolean(key && root);
    },
    restore(context, root) {
      const key = contextKey(context), snapshot = snapshots.get(key);
      if (!snapshot || !root) { cancel(); return false; }
      const navigation = pending || guard();
      navigation.key = key;
      if (navigation.interrupted) { cancel(); return true; }
      if (navigation.frame !== null) win.cancelAnimationFrame(navigation.frame);
      navigation.frame = win.requestAnimationFrame(() => {
        if (pending !== navigation) return;
        if (!navigation.interrupted && root.isConnected !== false) {
          const anchor = snapshot.anchor && findRow(root, snapshot.anchor.key);
          const y = anchor ? scrollPosition(win) + anchor.getBoundingClientRect().top - snapshot.anchor.top : snapshot.y;
          win.scrollTo({top:Math.max(0, y), behavior:'instant'});
          findFocus(root, snapshot.focus)?.focus({preventScroll:true});
        }
        cancel();
      });
      // A user-interrupted restore still consumes the saved position: callers
      // must not replace the user's chosen position with a scroll-to-top.
      return true;
    },
    reset(context) {
      const key = contextKey(context);
      snapshots.delete(key);
      cancel();
    },
    clearAccount(account) {
      for (const [key, snapshot] of snapshots) {
        if (snapshot.account === String(account)) snapshots.delete(key);
      }
      cancel();
    },
    cancel,
    destroy() { cancel(); snapshots.clear(); },
  };
}
