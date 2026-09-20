import test from 'node:test';
import assert from 'node:assert/strict';
import { bindRatingPanel, readExportOptions, renderExportDialog, renderRatingPanel, renderReviewBar } from '../app/static/content-review.js';

globalThis.document = {documentElement:{lang:'zh-CN'},activeElement:null};
const snapshot = (overrides = {}) => ({id:'rating1',version:2,metrics:{correctness:4,groundedness:5,difficulty_match:3,notes:'Keep the examples.',question_difficulties:[{slot_id:'logic',assessed_difficulty:'hard'},{slot_id:'other',assessed_difficulty:'uncertain'}]},...overrides});
const content = (overrides = {}) => ({id:'content1',version:2,evaluation:null,asset:{title:'A short lesson',sections:[],questions:[{slot_id:'other',stem:'What is a heap?'},{slot_id:'logic',stem:'Compare two approaches.'}]},...overrides});

test('review toolbar groups status separately and gives approved learning view the same button treatment as export', () => {
  const html=renderReviewBar(content({status:'approved'}));
  assert.match(html,/class="content-review-meta"/);
  assert.match(html,/class="badge approved"/);
  assert.match(html,/版本 2/);
  assert.match(html,/<button type="button" class="secondary" data-action="export-content">/);
  assert.match(html,/<a class="button secondary" href="\/\?learn=content1" target="_blank" rel="noopener">/);
  assert.doesNotMatch(html,/\/evidence|导出生成依据/);
});

test('unapproved content can export but never exposes an unavailable learning view', () => {
  for(const status of ['draft','failed','insufficient_evidence']) {
    const html=renderReviewBar(content({status}));
    assert.match(html,/data-action="export-content"/);
    assert.doesNotMatch(html,/\?learn=|打开学习页/);
  }
});

test('toolbar keeps content identifiers inside escaped URLs and never accepts a status as markup', () => {
  const id='"/><img src=x onerror="alert(1)">&other=value';
  const approved=renderReviewBar(content({id,status:'approved'}));
  assert.doesNotMatch(approved,/<img|onerror="alert/);
  assert.match(approved,new RegExp(encodeURIComponent(id).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
  const badStatus=renderReviewBar(content({status:'approved"><img src=x>',version:'<script>x</script>'}));
  assert.doesNotMatch(badStatus,/<img|<script|\?learn=/);
  assert.match(badStatus,/&lt;script&gt;/);
});

test('generation evidence remains available under a collapsed export disclosure for the correct content', () => {
  const html=renderExportDialog(content({id:'own content/1'}));
  assert.match(html,/<details class="content-export-more"><summary>/);
  assert.doesNotMatch(html,/<details class="content-export-more"[^>]*\bopen\b/);
  assert.match(html,/href="\/api\/contents\/own%20content%2F1\/evidence" download/);
  assert.match(html,/导出生成依据/);
});

test('rating is optional and collapsed, with data exports tucked inside the panel', () => {
  const html = renderRatingPanel(content());
  assert.match(html,/^<details[^>]+data-rating-panel>/);
  assert.doesNotMatch(html,/^<details[^>]+\bopen\b/);
  assert.match(html,/未评价/);
  assert.match(html,/保存评价/);
  assert.match(html,/可选评价/);
  assert.match(html,/<details class="content-rating-data">/);
  assert.match(html,/\/api\/evaluations\/export/);
  assert.match(html,/\/api\/difficulty\/evaluations\/export/);
});

test('saved ratings display current-version values, including questions matched by slot rather than order', () => {
  const value = content({evaluation:snapshot()}), before = structuredClone(value);
  const html = renderRatingPanel(value);
  assert.match(html,/已评价 · 版本 2/);
  assert.match(html,/更新评价/);
  assert.match(html,/<select name="correctness"[^>]*>[\s\S]*?<option value="4" selected/);
  assert.match(html,/<textarea[^>]+name="notes"[^>]*>Keep the examples\./);
  const other = html.match(/<select data-teacher-difficulty="other">([\s\S]*?)<\/select>/)[1];
  assert.match(other,/<option value="uncertain" selected/);
  assert.doesNotMatch(other,/<option value="hard" selected/);
  const logic = html.match(/<select data-teacher-difficulty="logic">([\s\S]*?)<\/select>/)[1];
  assert.match(logic,/<option value="hard" selected/);
  assert.deepEqual(value,before);
});

test('a prior version rating does not prefill or mark a newly edited version as rated', () => {
  const html = renderRatingPanel(content({version:3,evaluation:snapshot()}));
  assert.match(html,/未评价/);
  assert.doesNotMatch(html,/更新评价|Keep the examples\.|value="4" selected/);
});

test('authored title, feedback and question stems stay escaped without translating their content', () => {
  const unsafe = '<img src=x onerror="alert(1)">';
  const item = content({asset:{title:unsafe,questions:[{slot_id:'x',stem:unsafe}],sections:[]},evaluation:snapshot({metrics:{...snapshot().metrics,notes:unsafe}})});
  assert.doesNotMatch(renderRatingPanel(item),/<img/);
  assert.match(renderRatingPanel(item),/&lt;img/);
  assert.doesNotMatch(renderExportDialog(item),/<img/);
  assert.match(renderExportDialog(item),/&lt;img/);
});

function element(initial = {}) {
  return {dataset:{},value:'',disabled:false,hidden:false,textContent:'',...initial,setCustomValidity(message){this.validationMessage=message;}};
}
function harness(value = content(), callbacks = {}) {
  const fields = new Map(['correctness','groundedness','difficulty_match','notes'].map(name=>[name,element()]));
  const questionControls = (value.asset.questions || []).map((question,index)=>element({dataset:{teacherDifficulty:question.slot_id || `q${index+1}`}}));
  const submit = element(), error = element({hidden:true}), status = element({textContent:'initial status'}), difficultyError = element({hidden:true});
  let listener;
  const summary = {focus(){this.focused=true;}};
  const form = {
    attributes:{},valid:true,
    querySelector(selector) {
      if (selector === 'button[type="submit"]') return submit;
      if (selector === '[data-rating-error]') return error;
      if (selector === '.difficulty-evaluation-error') return difficultyError;
      return fields.get(selector.match(/^\[name="(.+)"\]$/)?.[1]);
    },
    querySelectorAll:selector=>selector === '[data-teacher-difficulty]' ? questionControls : [...fields.values(),...questionControls,submit],
    reportValidity(){return this.valid;},
    setAttribute(name,value){this.attributes[name]=value;},
    removeAttribute(name){delete this.attributes[name];},
    addEventListener(name,fn){assert.equal(name,'submit');listener=fn;},
    removeEventListener(name,fn){if(listener===fn)listener=null;},
    contains:node=>node===submit,
  };
  const panel = {open:true,querySelector:selector=>({'#evaluation':form,'[data-rating-summary]':status,summary})[selector]};
  const root = {isConnected:true,querySelector:()=>panel};
  const dispose = bindRatingPanel(root,value,callbacks);
  return {root,panel,form,fields,questionControls,submit,error,status,summary,dispose,submitForm:()=>listener?.({preventDefault(){}})};
}
function enterRatings(view) {
  view.fields.get('correctness').value='4';
  view.fields.get('groundedness').value='5';
  view.fields.get('difficulty_match').value='3';
  view.fields.get('notes').value='Preserve this note.';
}

test('save collapses the panel, preserves returned values and changes the action to update', async () => {
  let payload, saved;
  const view = harness(content(),{save:async value=>{payload=value;return {evaluation:snapshot({metrics:{...value,notes:'Returned note.'}})};},onSaved:value=>{saved=value;}});
  enterRatings(view);
  view.questionControls[0].value='uncertain';
  view.questionControls[1].value='hard';
  document.activeElement=view.submit;
  try {
    await view.submitForm();
    assert.equal(payload.version,2);
    assert.equal(payload.correctness,4);
    assert.deepEqual(payload.question_difficulties,[{slot_id:'other',assessed_difficulty:'uncertain'},{slot_id:'logic',assessed_difficulty:'hard'}]);
    assert.equal(saved.id,'rating1');
    assert.equal(view.panel.open,false);
    assert.equal(view.status.textContent,'已评价 · 版本 2');
    assert.equal(view.submit.textContent,'更新评价');
    assert.equal(view.fields.get('notes').value,'Returned note.');
    assert.equal(view.summary.focused,true);
    assert.equal(view.submit.disabled,false);
    assert.equal(view.form.attributes['aria-busy'],undefined);
    view.panel.open=true;
    assert.equal(view.fields.get('correctness').value,'4');
    assert.equal(view.questionControls[0].value,'uncertain');
  } finally { document.activeElement=null; view.dispose(); }
});

test('returning to a saved version prefills all controls and submitting edits keeps the pinned version', async () => {
  let payload;
  const view = harness(content({evaluation:snapshot()}),{save:async value=>{payload=value;return {evaluation:snapshot({metrics:value})};}});
  assert.equal(view.fields.get('notes').value,'Keep the examples.');
  assert.deepEqual(view.questionControls.map(control=>control.value),['uncertain','hard']);
  view.fields.get('correctness').value='2';
  await view.submitForm();
  assert.equal(payload.version,2);
  assert.equal(payload.correctness,2);
  view.dispose();
});

test('a slow save blocks duplicate submits and restores disabled states after a recoverable failure', async () => {
  let reject, count=0, reported;
  const view = harness(content(),{save:()=>{count++;return new Promise((_,fail)=>{reject=fail;});},onError:error=>{reported=error;}});
  enterRatings(view);
  view.fields.get('notes').disabled=true;
  const pending=view.submitForm();
  assert.equal(view.submit.disabled,true);
  assert.equal(view.form.attributes['aria-busy'],'true');
  await view.submitForm();
  assert.equal(count,1);
  const failure=new Error('Please retry');
  reject(failure);
  await pending;
  assert.equal(view.error.hidden,false);
  assert.equal(view.error.textContent,'Please retry');
  assert.equal(view.fields.get('notes').value,'Preserve this note.');
  assert.equal(view.fields.get('notes').disabled,true);
  assert.equal(view.submit.disabled,false);
  assert.equal(view.panel.open,true);
  assert.equal(reported,failure);
  view.dispose();
});

test('partial question difficulty ratings prevent a save without discarding user inputs', async () => {
  let count=0;
  const view=harness(content(),{save:async()=>{count++;}});
  enterRatings(view);
  view.questionControls[0].value='hard';
  await view.submitForm();
  assert.equal(count,0);
  assert.equal(view.error.hidden,false);
  assert.match(view.error.textContent,/全部填写/);
  assert.equal(view.questionControls[0].value,'hard');
  view.dispose();
});

test('a reply for a different version cannot display a false saved state', async () => {
  let saved=0;
  const view=harness(content(),{save:async()=>({evaluation:snapshot({version:3})}),onSaved:()=>saved++});
  enterRatings(view);
  await view.submitForm();
  assert.equal(saved,0);
  assert.equal(view.panel.open,true);
  assert.equal(view.status.textContent,'initial status');
  assert.equal(view.error.hidden,false);
  view.dispose();
});

test('route disposal or detached UI ignores late success and does not invoke old-page callbacks', async () => {
  for (const detached of [false,true]) {
    let resolve, saved=0;
    const view=harness(content(),{save:()=>new Promise(done=>{resolve=done;}),onSaved:()=>saved++});
    enterRatings(view);
    const pending=view.submitForm();
    if(detached)view.root.isConnected=false;else view.dispose();
    resolve({evaluation:snapshot()});
    await pending;
    assert.equal(saved,0);
    assert.equal(view.status.textContent,'initial status');
    assert.equal(view.panel.open,true);
    view.dispose();
    assert.ok(view.questionControls.every(control=>control.onchange===null));
  }
});

test('session-change errors do not display obsolete account errors', async () => {
  let errors=0;
  const view=harness(content(),{save:async()=>{const error=new Error('old account');error.name='SessionChangedError';throw error;},onError:()=>errors++});
  enterRatings(view);
  await view.submitForm();
  assert.equal(errors,0);
  assert.equal(view.error.hidden,true);
  view.dispose();
});

test('export offers PDF, Word and Markdown and only question material offers answer inclusion', () => {
  const quiz=renderExportDialog(content());
  assert.match(quiz,/name="format" value="pdf" checked/);
  assert.match(quiz,/name="format" value="docx"/);
  assert.match(quiz,/name="format" value="md"/);
  assert.match(quiz,/name="include_answers" checked/);
  const lesson=renderExportDialog(content({asset:{title:'Lesson',sections:[],questions:[]}}));
  assert.doesNotMatch(lesson,/name="include_answers"/);
});

test('export options reflect the explicit choice and UI language without rewriting authored text', () => {
  const form=(format,answers)=>({querySelector:selector=>selector==='[name="format"]:checked'?{value:format}:answers==null?null:{checked:answers}});
  for(const format of ['pdf','docx','md']) {
    assert.deepEqual(readExportOptions(form(format,false)),{format,include_answers:false,language:'zh'});
    assert.equal(readExportOptions(form(format,true)).include_answers,true);
  }
  assert.equal(readExportOptions(form('pdf',null)).include_answers,false);
  try {
    document.documentElement.lang='en';
    assert.equal(readExportOptions(form('docx',true)).language,'en');
    const html=renderExportDialog(content({asset:{title:'课程标题保持中文',questions:[]}}));
    assert.match(html,/课程标题保持中文/);
    assert.match(html,/File format/);
    assert.match(html,/Download file/);
  } finally { document.documentElement.lang='zh-CN'; }
  assert.throws(()=>readExportOptions(form('html',false)),/请选择文件格式/);
});
