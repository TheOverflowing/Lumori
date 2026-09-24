/* Navigation stays interruptible; quick reads do not flash a loading screen. */
export function beginViewLoad(root, loadingHTML, delay = 120) {
  const controller = new AbortController();
  let settled = false;
  root.setAttribute('aria-busy', 'true');
  root.inert = true;
  if (root.childNodes?.length === 0) root.innerHTML = loadingHTML;
  const timer = setTimeout(() => { if (!settled) root.innerHTML = loadingHTML; }, delay);
  const finish = () => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    root.inert = false;
    root.removeAttribute('aria-busy');
  };
  return {signal:controller.signal, finish, cancel() { controller.abort(); finish(); }};
}

/* Keep unchanged rows, focused controls and selection alive during polling. */
export function patchCollection(root, html) {
  if (root.innerHTML === html) return;
  const template = document.createElement('template');
  template.innerHTML = html;
  let oldParent = root, newParent = template.content;
  const wrapped = root.children.length === 1 && root.firstElementChild.classList.contains('collection');
  const nextWrapped = newParent.children.length === 1 && newParent.firstElementChild.classList.contains('collection');
  if (wrapped && nextWrapped) {
    oldParent = root.firstElementChild;
    newParent = newParent.firstElementChild;
  } else if (wrapped !== nextWrapped) {
    root.replaceChildren(template.content);
    return;
  }
  const before = [...oldParent.children], after = [...newParent.children];
  if (!after.length || [...before, ...after].some(row => !row.dataset.rowKey)) {
    root.replaceChildren(template.content);
    return;
  }
  const previous = new Map(before.map(row => [row.dataset.rowKey, row]));
  const keep = new Set(after.map(row => row.dataset.rowKey));
  for (const row of before) if (!keep.has(row.dataset.rowKey)) row.remove();
  let cursor = oldParent.firstElementChild;
  for (const incoming of after) {
    const existing = previous.get(incoming.dataset.rowKey);
    let row = existing;
    if (!existing) row = incoming;
    else if (!existing.isEqualNode(incoming)) {
      if (cursor === existing) cursor = incoming;
      existing.replaceWith(incoming);
      row = incoming;
    }
    if (row !== cursor) oldParent.insertBefore(row, cursor);
    cursor = row.nextElementSibling;
  }
}
