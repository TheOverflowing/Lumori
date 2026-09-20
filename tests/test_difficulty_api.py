"""Exact allocation and version-aware teacher evaluation, without live APIs."""
import csv
import io
import json

import pytest

from test_workflow import rig, seed, request, done


@pytest.mark.parametrize('distribution',[
    {'easy':0,'medium':0,'hard':5}, {'easy':5,'medium':0,'hard':0},
    {'easy':0,'medium':5,'hard':0}, {'easy':1,'medium':2,'hard':2},
])
def test_exact_distribution_is_preserved_in_asset_and_plan(rig,distribution):
    c,_,_=rig;course,_=seed(c)
    job=done(c,c.post('/api/generations',json=request(course)|{'count':5,'difficulty_distribution':distribution}))
    assert job['status']=='succeeded',job
    content=c.get('/api/contents/'+job['result']['content_id']).json()
    questions=content['asset']['questions']
    assert {level:sum(q['difficulty']==level for q in questions) for level in distribution}==distribution
    assert [(q['slot_id'],q['difficulty']) for q in questions]==[(p['slot_id'],p['difficulty']) for p in content['config']['difficulty_plan']]
    assert content['difficulty_assessment']['status']=='model_checked'


@pytest.mark.parametrize('distribution',[
    {'easy':False,'medium':0,'hard':5}, {'easy':0.0,'medium':0,'hard':5},
    {'easy':'0','medium':0,'hard':5}, {'easy':-1,'medium':1,'hard':5},
    {'easy':0,'medium':0,'hard':0}, {'easy':0,'medium':0,'hard':11},
    {'easy':0,'hard':5}, {'easy':1,'medium':1,'hard':1},
])
def test_invalid_allocations_rejected_without_model_calls(rig,distribution):
    c,_,wire=rig;course,_=seed(c);before=wire.calls
    response=c.post('/api/generations',json=request(course)|{'count':5,'difficulty_distribution':distribution})
    assert response.status_code==422 and wire.calls==before


def test_lesson_rejects_hidden_distribution_but_accepts_depth(rig):
    c,_,wire=rig;course,_=seed(c);before=wire.calls
    req=request(course)|{'material':'lesson','difficulty':'hard'}
    assert c.post('/api/generations',json=req|{'difficulty_distribution':{'easy':0,'medium':0,'hard':1}}).status_code==422
    assert wire.calls==before
    job=done(c,c.post('/api/generations',json=req))
    assert job['status']=='succeeded' and wire.calls==before+2
    result=c.get('/api/contents/'+job['result']['content_id']).json()
    assert result['asset']['questions']==[] and result['difficulty_assessment']['status']=='not_applicable'


def test_edits_invalidate_model_check_and_cannot_relabel_targets(rig):
    c,_,_=rig;course,_=seed(c)
    job=done(c,c.post('/api/generations',json=request(course)|{'difficulty':'hard'}))
    cid=job['result']['content_id'];base='/api/contents/'+cid
    content=c.get(base).json();asset=content['asset']
    asset['questions'][0]['difficulty']='easy'
    assert c.post(base+'/review',json={'version':1,'action':'save','asset':asset}).status_code==400
    asset['questions'][0]['difficulty']='hard';asset['title']='Edited draft'
    assert c.post(base+'/review',json={'version':1,'action':'save','asset':asset}).status_code==200
    assert c.get(base).json()['difficulty_assessment']['status']=='needs_review'
    assert c.post(base+'/review',json={'version':2,'action':'approve'}).status_code==200
    for reveal in ('false','true'):
        questions=c.get('/api/learn/'+cid+'?reveal_answers='+reveal).json()['asset']['questions']
        assert all(not {'difficulty','difficulty_design','slot_id'}&q.keys() for q in questions)


def test_teacher_ratings_capture_disagreement_uncertainty_and_version(rig):
    c,app,_=rig;course,_=seed(c)
    job=done(c,c.post('/api/generations',json=request(course)|{'count':3,'difficulty':'hard'}))
    cid=job['result']['content_id'];base='/api/contents/'+cid
    payload={'version':1,'correctness':4,'groundedness':4,'difficulty_match':3,
             'question_difficulties':[{'slot_id':'q1','assessed_difficulty':'hard'},
                 {'slot_id':'q2','assessed_difficulty':'medium'},{'slot_id':'q3','assessed_difficulty':'uncertain'}]}
    assert c.post(base+'/evaluations',json=payload|{'question_difficulties':payload['question_difficulties'][:1]}).status_code==400
    assert c.post(base+'/evaluations',json=payload|{'question_difficulties':[payload['question_difficulties'][0]]*3}).status_code==400
    result=c.post(base+'/evaluations',json=payload);assert result.status_code==201
    metrics=json.loads(app.state.store.one('SELECT metrics FROM evaluations WHERE id=?',(result.json()['id'],))['metrics'])
    assert metrics['difficulty_agreement']['rate']==.5
    assert metrics['difficulty_agreement']['uncertain']==1
    assert [r['matches_target'] for r in metrics['question_difficulties']]==[True,False,None]
    asset=c.get(base).json()['asset'];asset['title']='Next version'
    c.post(base+'/review',json={'version':1,'action':'save','asset':asset})
    assert c.post(base+'/evaluations',json=payload).status_code==409
    exported=list(csv.DictReader(io.StringIO(c.get('/api/difficulty/evaluations/export').text.lstrip('\ufeff'))))
    assert len(exported)==3 and {r['version'] for r in exported}=={'1'}
    assert [r['teacher_assessed_difficulty'] for r in exported]==['hard','medium','uncertain']


def test_historical_material_stays_unverified_and_reviewable(rig):
    c,app,_=rig;course,_=seed(c)
    job=done(c,c.post('/api/generations',json=request(course)))
    cid=job['result']['content_id'];base='/api/contents/'+cid;content=c.get(base).json()
    for q in content['asset']['questions']:
        for key in ('difficulty','difficulty_design','slot_id'):q.pop(key)
    for key in ('difficulty_plan','rubric_version'):content['config'].pop(key,None)
    app.state.store.execute('UPDATE contents SET asset=?,config=? WHERE id=?',
        (json.dumps(content['asset']),json.dumps(content['config']),cid))
    assert c.get(base).json()['difficulty_assessment']['status']=='legacy_unverified'
    assert c.post(base+'/review',json={'version':1,'action':'approve'}).status_code==200
