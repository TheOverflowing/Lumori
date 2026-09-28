import test from 'node:test';
import assert from 'node:assert/strict';
import { generationDraftData, editableAssetValues, restoreEditValues, applyEditValues, clearSubmittedEditDraft } from '../app/static/draft-forms.js';

test('generation drafts preserve unfinished fields but exclude provider and request state', () => {
  const data=generationDraftData({material:'quiz',topic:'',count:0,difficulty_mode:'distribution',
    difficulty_counts:{easy:'',medium:2,hard:'3'},check_missing_details:false,
    learner_profile:'旧语言的默认值',learner_profile_is_default:true,api_key:'private',request_key:'request',query_fusion:true});
  assert.equal(data.count,0);
  assert.deepEqual(data.difficulty_counts,{easy:'',medium:2,hard:3});
  assert.equal(data.check_missing_details,false);
  for (const key of ['learner_profile','api_key','request_key','query_fusion']) assert.equal(Object.hasOwn(data,key),false);
  assert.deepEqual(generationDraftData({material:'invalid',language:[],count:NaN,topic:{},difficulty_counts:{easy:{},hard:'bad'}}),
    {difficulty_counts:{easy:0,medium:0,hard:0}});
  assert.deepEqual(generationDraftData(null),{});
});

test('native numeric values do not make a pristine generation form look changed', () => {
  assert.deepEqual(generationDraftData({difficulty_counts:{easy:0,medium:3,hard:0}}),
    generationDraftData({difficulty_counts:{easy:'0',medium:'3',hard:'0'}}));
});

test('a late save only removes the exact submitted edit draft, preserving newer work', () => {
  let record={data:{title:'newer edit'}}, removed=0;
  const store={load:()=>record,remove:()=>{removed++;record=null;}};
  assert.equal(clearSubmittedEditDraft(store,{}, {title:'older submission'}),false);
  assert.equal(removed,0);
  assert.equal(clearSubmittedEditDraft(store,{}, {title:'newer edit'}),true);
  assert.equal(removed,1);
  assert.equal(clearSubmittedEditDraft(store,{}, {title:'newer edit'}),false);
});

test('editor restoration and saving retain the server asset structure and source identifiers', () => {
  const asset={title:'Title',learning_objectives:['Objective'],sections:[{heading:'Part',text:'Body',citation_ids:['source']}],
    questions:[{slot_id:'q2',stem:'Question',options:['A','B'],answer:'A',explanation:'Because',citation_ids:['source']}],visual_prompt:''};
  const original=structuredClone(asset), saved={'title':'','questions.0.stem':'Edited','questions.0.options.1':'Changed',
    'questions.0.slot_id':'q9','sections.0.citation_ids':['wrong'],'__proto__.polluted':'yes','questions.9.stem':'new'};
  const values=restoreEditValues(asset,saved);
  assert.equal(values.title,'');
  assert.equal(values['questions.0.stem'],'Edited');
  assert.equal(values['sections.0.text'],'Body');
  assert.deepEqual(Object.keys(values),Object.keys(editableAssetValues(asset)));
  const edited=applyEditValues(asset,saved);
  assert.equal(edited.questions[0].stem,'Edited');
  assert.equal(edited.questions[0].slot_id,'q2');
  assert.deepEqual(edited.sections[0].citation_ids,['source']);
  assert.equal(edited.questions.length,1);
  assert.deepEqual(asset,original);
  assert.deepEqual(edited.learning_objectives,['Objective']);
  assert.equal({}.polluted,undefined);
});
