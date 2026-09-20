import test from 'node:test';
import assert from 'node:assert/strict';
import { courseLibraryRows, materialIdentity, renderLibraryRow } from '../app/static/content-library.js';

globalThis.document = {documentElement:{lang:'zh-CN'}};
const content = overrides => ({id:'own-result',title:'同一个学习目标',material:'quiz',version:1,status:'draft',created_at:'2026-09-20T08:00:00Z',course_id:'ml',course_name:'机器学习',...overrides});

test('lesson, quiz and assignment rows identify their actual saved task type with the creation icons', () => {
  for(const [material,icon,label] of [['lesson','book-open','学习讲解'],['quiz','layers','测验练习'],['assignment','file-text','课后作业']]) {
    const html=renderLibraryRow(content({material}));
    assert.match(html,new RegExp(`src="/static/icons/${icon}\\.svg"`));
    assert.match(html,new RegExp(`data-material="${material}"`));
    assert.match(html,new RegExp(`class="material-type">.*?${label}`));
    assert.match(html,/data-action="open-content" data-id="own-result"/);
    assert.match(html,/class="badge draft"/);
  }
});

test('identity comes from metadata, never title, sections or question counts', () => {
  assert.equal(materialIdentity({title:'Quiz assignment lesson',asset:{sections:[{}],questions:[{}]}}).kind,'unknown');
  assert.equal(materialIdentity({config:{material:'assignment'}}).kind,'assignment');
  assert.equal(materialIdentity({material:'quiz',config:{material:'lesson'}}).kind,'quiz');
  assert.equal(materialIdentity({material:'future-type',title:'A quiz'}).kind,'unknown');
  assert.equal(materialIdentity({material:'__proto__'}).kind,'unknown');
});

test('type copy localizes while the authored title and course name remain intact', () => {
  document.documentElement.lang='en';
  try {
    const html=renderLibraryRow(content({material:'assignment'}),{showCourse:true});
    assert.match(html,/>Assignment<\/span>/);
    assert.match(html,/>Version 1<\/span>/);
    assert.match(html,/>同一个学习目标<\/h3>/);
    assert.match(html,/>机器学习<\/span>/);
    assert.match(html,/data-date="2026-09-20T08:00:00Z"/);
    assert.doesNotMatch(renderLibraryRow(content()),/机器学习/);
  } finally { document.documentElement.lang='zh-CN'; }
});

test('row values cannot become markup, classes or arbitrary icon URLs', () => {
  const unsafe='\"><img src=x onerror="alert(1)">';
  const html=renderLibraryRow(content({id:unsafe,title:unsafe,course_name:unsafe,status:unsafe,material:unsafe,version:unsafe,created_at:unsafe}),{showCourse:true});
  assert.match(html,/data-material="unknown"/);
  assert.match(html,/src="\/static\/icons\/files.svg"/);
  assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/<img src=x|onerror="alert|class="badge ">.*?<img/);
});

test('course scope excludes other courses, unassigned rows and absent selection', () => {
  const rows=[content({id:'a'}),content({id:'b',course_id:'os'}),content({id:'c',course_id:null}),null];
  assert.deepEqual(courseLibraryRows(rows,'ml').map(item=>item.id),['a']);
  assert.deepEqual(courseLibraryRows(rows,'os').map(item=>item.id),['b']);
  assert.deepEqual(courseLibraryRows(rows,''),[]);
  assert.deepEqual(courseLibraryRows(rows,undefined),[]);
  assert.deepEqual(courseLibraryRows(rows,'deleted-course'),[]);
  assert.equal(rows.length,4);
});
