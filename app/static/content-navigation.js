import { a, m, t } from './i18n.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export const contentAnchor = (kind, index) => `${kind}-${index + 1}`;
export const displayedQuestionNumber = (question, index, {originalQuestionNumbers = false} = {}) =>
  originalQuestionNumbers && /^q[1-9]\d*$/.test(question.slot_id || '') ? Number(question.slot_id.slice(1)) : index + 1;

// Anchors use structure, never authored text or citation IDs. A changed title
// keeps its destination, and each newly rendered version gets a fresh outline.
export function contentNavigationItems(asset = {}, options = {}) {
  return [
    ...(asset.sections || []).map((section, index) => ({
      anchor:contentAnchor('section', index), kind:'section',
      title:String(section.heading || '').trim(), label:'第 {n} 节', number:index + 1,
    })),
    ...(asset.questions || []).map((question, index) => ({
      anchor:contentAnchor('question', index), kind:'question', title:'',
      label:'第 {n} 题', number:displayedQuestionNumber(question, index, options),
    })),
  ];
}

export function renderContentNavigation(asset, options = {}) {
  const items = contentNavigationItems(asset, options);
  if (items.length < 2) return '';
  const label = item => item.title ? escape(item.title) : m(item.label, {n:item.number});
  const option = item => `<option value="${item.anchor}"${item.title ? '' : ` data-i18n="${escape(item.label)}" data-i18n-values="${escape(JSON.stringify({n:item.number}))}"`}>${escape(item.title || t(item.label, {n:item.number}))}</option>`;
  return `<nav class="content-navigation" data-content-navigation ${a('内容目录','aria-label')}>
    <span class="content-navigation-caption" aria-hidden="true">${m('目录')}</span>
    <div class="content-navigation-links">${items.map(item => `<button type="button" data-content-target="${item.anchor}" class="content-navigation-item"${item.title ? ` title="${escape(item.title)}"` : ''}>${label(item)}</button>`).join('')}</div>
    <select class="content-navigation-select" data-content-jump ${a('跳转到章节或题目','aria-label')}>${items.map(option).join('')}</select>
  </nav>`;
}

// A mounted outline belongs to this rendered page only. No hash writes: the
// application owns routing, and navigation should not add browser history.
export function mountContentNavigation(root) {
  const nav = root.querySelector('[data-content-navigation]');
  if (!nav) return () => {};
  const doc = root.ownerDocument || document;
  const win = doc.defaultView || window;
  const toolbar = doc.querySelector('.toolbar');
  const buttons = [...nav.querySelectorAll('[data-content-target]')];
  const select = nav.querySelector('[data-content-jump]');
  const links = nav.querySelector('.content-navigation-links');
  const targets = new Map([...root.querySelectorAll('[data-content-anchor]')].map(node => [node.dataset.contentAnchor, node]));
  const anchors = buttons.map(button => targets.get(button.dataset.contentTarget)).filter(Boolean);
  let disposed = false, active = '', observer, frame = null, offset = 176, pendingTarget = '';

  const activate = key => {
    if (disposed || active === key || !targets.has(key)) return;
    active = key;
    for (const button of buttons) {
      if (button.dataset.contentTarget === key) button.setAttribute('aria-current','location');
      else button.removeAttribute('aria-current');
    }
    if (select) select.value = key;
    const current = buttons.find(button => button.dataset.contentTarget === key);
    if (links?.clientWidth && current) {
      const strip = links.getBoundingClientRect(), item = current.getBoundingClientRect();
      if (item.left < strip.left) links.scrollLeft -= strip.left - item.left;
      else if (item.right > strip.right) links.scrollLeft += item.right - strip.right;
    }
  };
  const syncActive = () => {
    if (disposed || !anchors.length || pendingTarget) return;
    let current = anchors[0];
    for (const anchor of anchors) {
      if (anchor.getBoundingClientRect().top > offset + 24) break;
      current = anchor;
    }
    // The final question can be too short to reach the sticky toolbar before
    // the page runs out of scroll space. Prefer it at the document bottom only
    // while it is actually visible; a long inspector/footer must not select an
    // offscreen question merely because the document has reached its end.
    const scrolling = doc.scrollingElement || doc.documentElement;
    const viewportHeight = win.innerHeight || scrolling?.clientHeight || 0;
    const scrollTop = scrolling?.scrollTop ?? win.scrollY ?? 0;
    if (viewportHeight > 0 && scrolling?.scrollHeight > 0 && scrollTop + viewportHeight >= scrolling.scrollHeight - 2) {
      const last = anchors.at(-1), bounds = last.getBoundingClientRect();
      if (bounds.top < viewportHeight && bounds.bottom > offset) current = last;
    }
    activate(current.dataset.contentAnchor);
  };
  const finishScroll = () => { pendingTarget = ''; syncActive(); };
  const interruptScroll = () => { pendingTarget = ''; };
  const measure = () => {
    if (disposed) return;
    const toolbarHeight = Math.ceil(toolbar?.getBoundingClientRect().height || 0);
    const navHeight = Math.ceil(nav.getBoundingClientRect().height);
    offset = toolbarHeight + navHeight + 16;
    root.style.setProperty('--content-toolbar-height',`${toolbarHeight}px`);
    root.style.setProperty('--content-navigation-height',`${navHeight}px`);
    observer?.disconnect();
    if (win.IntersectionObserver) {
      observer = new win.IntersectionObserver(syncActive, {rootMargin:`-${offset}px 0px -50% 0px`, threshold:0});
      // Observe the short heading, not a potentially screen-long question.
      // Otherwise entering a long question could leave its previous item active
      // until the entire question has scrolled out of view.
      for (const anchor of anchors) observer.observe(anchor.querySelector?.('.question-number') || anchor);
    }
    syncActive();
  };
  const scheduleMeasure = () => {
    if (!disposed && frame === null) frame = win.requestAnimationFrame(() => { frame = null; measure(); });
  };
  const jump = (key, pointer) => {
    if (disposed) return;
    const target = targets.get(key);
    if (!target) return;
    activate(key);
    // Native smooth scrolling remains interruptible. Keyboard/reduced-motion
    // navigation is immediate, with focus carried to the reading destination.
    const reduced = win.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    // Browsers with scrollend can keep the chosen item steady during a smooth
    // jump, then resume read-position tracking. User input cancels that lock.
    pendingTarget = pointer && !reduced && 'onscrollend' in win ? key : '';
    target.focus({preventScroll:true});
    target.scrollIntoView({block:'start', behavior:pointer && !reduced ? 'smooth' : 'instant'});
  };
  const click = event => {
    const button = event.target.closest?.('[data-content-target]');
    if (!button || !nav.contains(button)) return;
    jump(button.dataset.contentTarget, event.detail > 0);
  };
  const change = event => { if (event.target === select) jump(select.value, false); };
  nav.addEventListener('click', click);
  nav.addEventListener('change', change);
  win.addEventListener('resize', scheduleMeasure, {passive:true});
  win.addEventListener('scrollend', finishScroll, {passive:true});
  for (const type of ['wheel','pointerdown','keydown']) win.addEventListener(type, interruptScroll, {passive:true});
  const resize = win.ResizeObserver ? new win.ResizeObserver(scheduleMeasure) : null;
  if (toolbar) resize?.observe(toolbar);
  resize?.observe(nav);
  measure();
  return () => {
    if (disposed) return;
    disposed = true;
    observer?.disconnect(); resize?.disconnect();
    if (frame !== null) win.cancelAnimationFrame(frame);
    win.removeEventListener('resize', scheduleMeasure);
    win.removeEventListener('scrollend', finishScroll);
    for (const type of ['wheel','pointerdown','keydown']) win.removeEventListener(type, interruptScroll);
    nav.removeEventListener('click', click);
    nav.removeEventListener('change', change);
    root.style.removeProperty('--content-toolbar-height');
    root.style.removeProperty('--content-navigation-height');
  };
}
