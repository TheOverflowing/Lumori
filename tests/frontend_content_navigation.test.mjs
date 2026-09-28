import test from 'node:test';
import assert from 'node:assert/strict';
import { contentNavigationItems, renderContentNavigation, mountContentNavigation } from '../app/static/content-navigation.js';
import { renderAsset } from '../app/static/content-renderer.js';

globalThis.document = {documentElement:{lang:'zh-CN'}};

test('outline and reading destinations agree without using untrusted titles or slot IDs as selectors', () => {
  const asset = {title:'Test', sections:[{heading:'<img src=x onerror=alert(1)> "A"',text:'Text'},{heading:'',text:'More'}], questions:[{slot_id:'q7',stem:'One'},{slot_id:'q3',stem:'Two'}]};
  const before = JSON.stringify(asset);
  const options = {originalQuestionNumbers:true};
  const items = contentNavigationItems(asset,options);
  assert.deepEqual(items.map(item=>item.anchor),['section-1','section-2','question-1','question-2']);
  assert.deepEqual(items.slice(2).map(item=>item.number),[7,3]);
  const html = renderContentNavigation(asset,options);
  const body = renderAsset(asset,false,null,[],options);
  assert.doesNotMatch(html,/<img|href="#/);
  assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html,/第 7 题/);
  for (const {anchor} of items) {
    assert.match(html,new RegExp(`data-content-target="${anchor}"`));
    assert.match(body,new RegExp(`data-content-anchor="${anchor}" tabindex="-1"`));
  }
  assert.equal(JSON.stringify(asset),before);
});

test('empty or single-block content has no redundant navigator and each version builds a fresh outline', () => {
  assert.equal(renderContentNavigation({}), '');
  assert.equal(renderContentNavigation({questions:[{stem:'Only'}]}), '');
  const first = {sections:[{heading:'Old'},{heading:'Removed'}]};
  const next = {sections:[{heading:'New'}],questions:[{stem:'Added'}]};
  assert.match(renderContentNavigation(first),/Removed/);
  assert.doesNotMatch(renderContentNavigation(next),/Removed|Old/);
  assert.deepEqual(contentNavigationItems(next).map(item=>item.anchor),['section-1','question-1']);
});

class Node {
  constructor(dataset = {}) { this.dataset=dataset; this.attrs=new Map(); this.handlers=new Map(); this.style={values:new Map(),setProperty:(k,v)=>this.style.values.set(k,v),removeProperty:k=>this.style.values.delete(k)}; }
  setAttribute(k,v) { this.attrs.set(k,v); }
  removeAttribute(k) { this.attrs.delete(k); }
  addEventListener(k,fn) { this.handlers.set(k,fn); }
  removeEventListener(k,fn) { if(this.handlers.get(k)===fn)this.handlers.delete(k); }
  emit(type,event) { this.handlers.get(type)?.(event); }
  focus(options) { this.focusOptions=options; }
  scrollIntoView(options) { this.scrollOptions=options; }
  getBoundingClientRect() { const top=this.top || 0,height=this.height || 0; return {top,height,bottom:top+height}; }
}

function fixture({reduced = false} = {}) {
  const nav=new Node(), root=new Node(), toolbar=new Node(), select=new Node();
  const buttons=['section-1','question-1','question-2'].map(key=>new Node({contentTarget:key}));
  const anchors=buttons.map((button,index)=>Object.assign(new Node({contentAnchor:button.dataset.contentTarget}),{top:[240,1000,1700][index]}));
  const observers=[], resizeObservers=[];
  const win=new Node();
  win.matchMedia=()=>({matches:reduced});
  win.IntersectionObserver=class {constructor(fn,options){this.fn=fn;this.options=options;this.observed=[];observers.push(this);}observe(node){this.observed.push(node);}disconnect(){this.disconnected=true;}fire(){this.fn();}};
  win.ResizeObserver=class {constructor(fn){this.fn=fn;resizeObservers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
  const frames=new Map();let nextFrame=0;
  win.requestAnimationFrame=fn=>{frames.set(++nextFrame,fn);return nextFrame;};
  win.cancelAnimationFrame=id=>frames.delete(id);
  win.location={hash:'#content/current'};
  win.onscrollend=null;
  win.innerHeight=800;
  toolbar.height=88;nav.height=60;
  const scrolling={scrollTop:0,scrollHeight:3000,clientHeight:800};
  root.ownerDocument={defaultView:win,scrollingElement:scrolling,querySelector:selector=>selector==='.toolbar'?toolbar:null};
  root.querySelector=selector=>selector==='[data-content-navigation]'?nav:null;
  root.querySelectorAll=()=>anchors;
  nav.querySelectorAll=()=>buttons;
  nav.querySelector=()=>select;
  nav.contains=node=>buttons.includes(node);
  for(const button of buttons)button.closest=()=>button;
  return {root,nav,win,toolbar,select,buttons,anchors,observers,resizeObservers,frames,scrolling,
    flush(){const queued=[...frames.values()];frames.clear();queued.forEach(fn=>fn());}};
}

test('clicking an outline item focuses its destination and scrolls without replacing the application route', () => {
  const f=fixture();
  const destroy=mountContentNavigation(f.root);
  f.nav.emit('click',{target:f.buttons[1],detail:1});
  assert.deepEqual(f.anchors[1].focusOptions,{preventScroll:true});
  assert.deepEqual(f.anchors[1].scrollOptions,{block:'start',behavior:'smooth'});
  assert.equal(f.buttons[1].attrs.get('aria-current'),'location');
  assert.equal(f.buttons[0].attrs.has('aria-current'),false);
  assert.equal(f.select.value,'question-1');
  assert.equal(f.win.location.hash,'#content/current');
  f.nav.emit('click',{target:f.buttons[2],detail:0});
  assert.equal(f.anchors[2].scrollOptions.behavior,'instant');
  destroy();
});

test('reduced motion and the compact selector use immediate navigation', () => {
  const f=fixture({reduced:true});
  const destroy=mountContentNavigation(f.root);
  f.nav.emit('click',{target:f.buttons[1],detail:1});
  assert.equal(f.anchors[1].scrollOptions.behavior,'instant');
  f.select.value='question-2';
  f.nav.emit('change',{target:f.select});
  assert.equal(f.anchors[2].scrollOptions.behavior,'instant');
  assert.equal(f.buttons[2].attrs.get('aria-current'),'location');
  f.select.value='missing-target';
  f.nav.emit('change',{target:f.select});
  assert.equal(f.buttons[2].attrs.get('aria-current'),'location');
  destroy();
});

test('the outline follows reading position and remeasures a wrapping toolbar without per-frame scroll listeners', () => {
  const f=fixture();
  const destroy=mountContentNavigation(f.root);
  assert.equal(f.root.style.values.get('--content-toolbar-height'),'88px');
  assert.equal(f.win.handlers.has('scroll'),false);
  f.anchors[0].top=-500;f.anchors[1].top=140;
  f.observers.at(-1).fire();
  assert.equal(f.select.value,'question-1');
  f.toolbar.height=132;
  f.win.emit('resize',{});f.win.emit('resize',{});
  assert.equal(f.frames.size,1);
  f.flush();
  assert.equal(f.root.style.values.get('--content-toolbar-height'),'132px');
  assert.equal(f.observers[0].disconnected,true);
  assert.equal(f.observers.at(-1).options.rootMargin,'-208px 0px -50% 0px');
  destroy();
});

test('a short last question stays selected when the document bottom prevents full alignment', () => {
  const f=fixture();
  const destroy=mountContentNavigation(f.root);
  f.nav.emit('click',{target:f.buttons[2],detail:1});
  // Intersections during the animation must not flash earlier outline items.
  f.observers.at(-1).fire();
  assert.equal(f.select.value,'question-2');
  f.anchors[0].top=-700;f.anchors[1].top=-300;
  f.anchors[2].top=229;f.anchors[2].height=300;
  f.scrolling.scrollTop=2200;
  f.win.emit('scrollend',{});
  assert.equal(f.select.value,'question-2');
  assert.equal(f.buttons[2].attrs.get('aria-current'),'location');
  destroy();
});

test('document-bottom correction does not override reading position when a long footer hides the last target', () => {
  const f=fixture();
  const destroy=mountContentNavigation(f.root);
  f.scrolling.scrollTop=2200;
  // Deliberately isolate the visibility check from the normal heading scan.
  // An out-of-order/positioned target must not become current below the viewport.
  f.anchors[0].top=-100;f.anchors[1].top=229;f.anchors[1].height=200;
  f.anchors[2].top=850;f.anchors[2].height=300;
  f.win.emit('scrollend',{});
  assert.equal(f.select.value,'section-1');
  // A bottom-aligned final target is visible but entirely behind the toolbar.
  f.anchors[2].top=30;f.anchors[2].height=60;
  f.win.emit('scrollend',{});
  assert.equal(f.select.value,'section-1');
  destroy();
});

test('manual scroll input releases a pending smooth navigation highlight', () => {
  const f=fixture();
  const destroy=mountContentNavigation(f.root);
  f.nav.emit('click',{target:f.buttons[2],detail:1});
  f.win.emit('wheel',{});
  f.observers.at(-1).fire();
  assert.equal(f.select.value,'section-1');
  destroy();
});

test('destroy removes listeners, pending work and observers so an old version cannot navigate a new page', () => {
  const f=fixture();
  const destroy=mountContentNavigation(f.root);
  f.win.emit('resize',{});
  const staleClick=f.nav.handlers.get('click'), staleResize=[...f.frames.values()][0];
  destroy();destroy();
  assert.equal(f.nav.handlers.size,0);
  assert.equal(f.win.handlers.size,0);
  assert.equal(f.frames.size,0);
  assert.ok(f.observers.every(observer=>observer.disconnected));
  assert.ok(f.resizeObservers.every(observer=>observer.disconnected));
  staleClick({target:f.buttons[1],detail:1});staleResize();
  assert.equal(f.anchors[1].scrollOptions,undefined);
  assert.equal(f.root.style.values.size,0);
  assert.doesNotThrow(()=>mountContentNavigation({querySelector:()=>null})());
});
