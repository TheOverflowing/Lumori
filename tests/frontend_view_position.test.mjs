import test from 'node:test';
import assert from 'node:assert/strict';
import { createViewPositions } from '../app/static/view-position.js';

const context = (course = 'course-a', route = 'contents', account = 'user-a') => ({account, course, route});

// Geometry changes with scrollY just as browser client rectangles do. Frames
// and user input are controlled so asynchronous navigation can be exercised.
function fixture() {
  let nextFrame = 0;
  const frames = new Map(), listeners = new Map();
  const win = {scrollY:0, innerHeight:600, document:{activeElement:null}, scrolls:[],
    requestAnimationFrame(callback) { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame(id) { frames.delete(id); },
    scrollTo(options) { this.scrolls.push(options); this.scrollY = options.top; },
    addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  const flush = () => { for (const [id, callback] of [...frames]) { frames.delete(id); callback(); } };
  const input = name => { for (const callback of listeners.get(name) || []) callback({type:name}); };
  const root = {isConnected:true, rows:[], controls:[],
    querySelectorAll(selector) { return selector === '[data-row-key]' ? this.rows.filter(row => row.dataset.rowKey) : [...this.controls, ...this.rows.flatMap(row => row.controls)]; },
    contains(control) { return control === this || this.rows.includes(control) || this.querySelectorAll('controls').includes(control); },
  };
  function row(key, top, height = 140) {
    const item = {dataset:key ? {rowKey:key} : {}, top, height, controls:[],
      getBoundingClientRect() { return {top:this.top-win.scrollY, bottom:this.top-win.scrollY+this.height, height:this.height}; },
      querySelectorAll() { return this.controls; },
    };
    root.rows.push(item); return item;
  }
  function button(owner, attributes = {'data-action':'open-content', 'data-id':'item'}) {
    const control = {tagName:'BUTTON', disabled:false, hidden:false, focusOptions:null,
      getAttribute(name) { return attributes[name] ?? null; },
      closest() { return owner === root ? null : owner; },
      focus(options) { this.focusOptions=options; win.document.activeElement=this; },
    };
    owner.controls.push(control); return control;
  }
  return {win, root, row, button, flush, input, frames, listeners, positions:createViewPositions({window:win})};
}

test('return restores the same visible row and button after heights above it change', () => {
  const f = fixture(), first=f.row('content:1',200), anchor=f.row('content:2',500);
  const active=f.button(anchor); f.win.scrollY=480; f.win.document.activeElement=active;
  f.positions.capture(context(), f.root);
  f.win.scrollY=0;
  first.height=500; anchor.top=860;
  const replacement=f.button(anchor); anchor.controls=[replacement];
  assert.equal(f.positions.restore(context(), f.root),true);
  f.flush();
  assert.deepEqual(f.win.scrolls,[{top:840,behavior:'instant'}]);
  assert.equal(anchor.getBoundingClientRect().top,20);
  assert.equal(f.win.document.activeElement,replacement);
  assert.deepEqual(replacement.focusOptions,{preventScroll:true});
});

test('deleting a saved row falls back to the saved offset without focusing a different row', () => {
  const f=fixture(), removed=f.row('gone',400), remaining=f.row('kept',600);
  f.win.scrollY=420; f.win.document.activeElement=f.button(removed);
  f.positions.capture(context(),f.root);
  const unrelated=f.button(remaining); f.root.rows=[remaining]; f.win.scrollY=0; f.win.document.activeElement=null;
  f.positions.restore(context(),f.root); f.flush();
  assert.equal(f.win.scrollY,420); assert.equal(unrelated.focusOptions,null);
});

test('unkeyed and empty lists still restore pixel position, with unique non-row focus', () => {
  const f=fixture(); f.row('',600); f.win.scrollY=610;
  const search=f.button(f.root,{id:'content-search'}); f.win.document.activeElement=search;
  f.positions.capture(context(),f.root); f.win.scrollY=0;
  f.positions.restore(context(),f.root); f.flush();
  assert.equal(f.win.scrollY,610); assert.deepEqual(search.focusOptions,{preventScroll:true});
  f.root.rows=[]; f.win.scrollY=0;
  f.positions.restore(context(),f.root); f.flush(); assert.equal(f.win.scrollY,610);
});

test('account, course and list route are independent and invalid routes are not captured', () => {
  const f=fixture();
  for (const [scope,y] of [[context(),100],[context('course-b'),200],[context('course-a','documents'),300],[context('course-a','contents','user-b'),400]]) {
    f.win.scrollY=y; f.positions.capture(scope,f.root);
  }
  for (const [scope,y] of [[context(),100],[context('course-b'),200],[context('course-a','documents'),300],[context('course-a','contents','user-b'),400]]) {
    f.win.scrollY=0; assert.equal(f.positions.restore(scope,f.root),true); f.flush(); assert.equal(f.win.scrollY,y);
  }
  assert.equal(f.positions.capture(context('course-a','content'),f.root),false);
  assert.equal(f.positions.restore(context('course-a','content'),f.root),false);
  assert.equal(f.positions.capture(context('course-a','contents',''),f.root),false);
  assert.equal(f.positions.restore(context('course-a','contents',''),f.root),false);
});

test('filter reset and account clear forget only their matching snapshots', () => {
  const f=fixture();
  for (const scope of [context(),context('course-b'),context('course-a','jobs','user-b')]) f.positions.capture(scope,f.root);
  f.positions.reset(context()); assert.equal(f.positions.restore(context(),f.root),false);
  assert.equal(f.positions.restore(context('course-b'),f.root),true);
  f.positions.clearAccount('user-a'); f.flush();
  assert.equal(f.positions.restore(context('course-b'),f.root),false);
  assert.equal(f.positions.restore(context('course-a','jobs','user-b'),f.root),true); f.flush();
});

test('user scrolling or interacting during the read prevents a late scroll or focus jump', () => {
  for (const event of ['wheel','touchstart','pointerdown','keydown']) {
    const f=fixture(), anchor=f.row('saved',400), active=f.button(anchor);
    f.win.scrollY=410; f.win.document.activeElement=active; f.positions.capture(context(),f.root);
    f.positions.capture(context('course-a','content'),f.root);
    f.input(event); f.win.scrollY=75; f.win.document.activeElement=null;
    assert.equal(f.positions.restore(context(),f.root),true); f.flush();
    assert.equal(f.win.scrollY,75); assert.equal(active.focusOptions,null); assert.equal(f.win.scrolls.length,0);
  }
});

test('input between binding and its layout frame also cancels restoration', () => {
  const f=fixture(); f.win.scrollY=400; f.positions.capture(context(),f.root); f.win.scrollY=0;
  f.positions.restore(context(),f.root); f.input('wheel'); f.win.scrollY=90; f.flush();
  assert.equal(f.win.scrollY,90); assert.equal(f.win.scrolls.length,0);
  assert.equal([...f.listeners.values()].reduce((n,set)=>n+set.size,0),0);
});

test('superseded navigation and destroy cancel queued frames and release listeners', () => {
  const f=fixture(); f.positions.capture(context(),f.root); f.positions.restore(context(),f.root);
  f.positions.capture(context('course-b'),f.root); f.flush(); assert.equal(f.win.scrolls.length,0);
  f.positions.restore(context(),f.root); f.positions.destroy(); f.flush();
  assert.equal(f.win.scrolls.length,0); assert.equal(f.frames.size,0);
  assert.equal([...f.listeners.values()].reduce((n,set)=>n+set.size,0),0);
  assert.equal(f.positions.restore(context(),f.root),false);
});

test('ambiguous or disabled replacement buttons never receive focus', () => {
  for (const disabled of [false,true]) {
    const f=fixture(), anchor=f.row('saved',100), active=f.button(anchor);
    f.win.document.activeElement=active; f.positions.capture(context(),f.root);
    const duplicate=f.button(anchor);
    if (disabled) { anchor.controls=[duplicate]; duplicate.disabled=true; }
    f.win.document.activeElement=null; f.positions.restore(context(),f.root); f.flush();
    assert.equal(f.win.document.activeElement,null);
  }
});
