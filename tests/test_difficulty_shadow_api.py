import json
import pytest
from test_workflow import rig,seed,request,done
from app.difficulty_shadow import digest

def test_content_export_preserves_observer_and_marks_edited_content_stale(rig):
    c,app,wire=rig;course,_=seed(c)
    job=done(c,c.post('/api/generations',json=request(course)))
    cid=job['result']['content_id'];base='/api/contents/'+cid
    content=c.get(base).json();store=app.state.store
    row=store.one('SELECT evidence FROM job_evidence WHERE job_id=?',(job['id'],));e=json.loads(row['evidence'])
    e['difficulty_shadow']={'status':'completed','calls_reserved':1,'questions':[
        {'slot_id':'q1','original_difficulty':'medium','target_difficulty':'hard',
         'published_difficulty':'hard','judge':{'status':'validated','result':{
             'eligibility':'eligible','estimated_difficulty':'uncertain',
             'decision':{'candidates':['easy','medium']}}},
         'audit':{'status':'validated','result':{'status':'no_issue_found'}}}],
        'asset_sha256':digest(content['asset']),'mode':'shadow','affects_primary_decision':False}
    store.save_job_evidence(job['id'],e)
    before=wire.calls
    assert c.get('/api/jobs/'+job['id']).json()['difficulty_shadow']['status']=='completed'
    assert c.get(base).json()['difficulty_shadow']['matches_current_content'] is True
    assert c.get(base).json()['difficulty_shadow']['questions'][0]['review_flags']==[
        'published_grade_outside_candidates','difficulty_boundary']
    exported=c.get(base+'/evidence').json()['difficulty_shadow']
    assert exported['asset_sha256']==digest(content['asset'])
    asset=content['asset'];asset['title']='Edited title'
    assert c.post(base+'/review',json={'version':1,'action':'save','asset':asset}).status_code==200
    assert c.get(base).json()['difficulty_shadow']['matches_current_content'] is False
    assert c.get(base+'/evidence').json()['difficulty_shadow']['observed_content_version']==1
    assert wire.calls==before


@pytest.mark.parametrize(('status','failed_check'),[
    ('content_issue','reference_correct'),('insufficient_evidence','solution_correct')])
def test_new_review_summary_keeps_host_decision_and_version_binding(rig,status,failed_check):
    c,app,wire=rig;course,_=seed(c)
    job=done(c,c.post('/api/generations',json=request(course)))
    base='/api/contents/'+job['result']['content_id']
    content=c.get(base).json();store=app.state.store
    row=store.one('SELECT evidence FROM job_evidence WHERE job_id=?',(job['id'],))
    evidence=json.loads(row['evidence'])
    decision={'status':status,'recommendation':'manual_review','target_relation':'unknown',
        'estimated_difficulty':'uncertain','candidates':[],'reason_codes':['failed:'+failed_check],
        'enforcement_enabled':False,'human_validated':False}
    checks={name:{'status':'fail' if name==failed_check else 'pass'} for name in (
        'conditions_complete','solution_correct','reference_correct','explanation_consistent','source_support','demand_supported')}
    evidence['difficulty_shadow']={'protocol':'review_v2','revision':'saved-runtime','status':'completed',
        'asset_sha256':digest(content['asset']),'coverage':{'total_questions':1,'sampled_questions':1,'assessed_questions':0},
        'questions':[{'slot_id':'q1','decision':decision,'verification':{'status':'validated',
            'messages':[{'content':'PRIVATE RAW PROVIDER CONTEXT'}],
            'result':{'checks':checks}}}]}
    store.save_job_evidence(job['id'],evidence)
    before=wire.calls
    observed=c.get(base).json()['difficulty_shadow']
    assert observed['matches_current_content'] is True
    assert observed['questions'][0]['decision']==decision
    assert observed['questions'][0]['review_flags']==[status]
    assert observed['questions'][0]['content_checks']==checks
    assert 'PRIVATE RAW PROVIDER CONTEXT' not in json.dumps(observed)
    assert c.get('/api/jobs/'+job['id']).json()['difficulty_shadow']['coverage']['assessed_questions']==0
    asset=content['asset'];asset['title']='Changed'
    assert c.post(base+'/review',json={'version':1,'action':'save','asset':asset}).status_code==200
    assert c.get(base).json()['difficulty_shadow']['matches_current_content'] is False
    assert c.get(base+'/evidence').json()['difficulty_shadow']['questions'][0]['decision']==decision
    assert wire.calls==before
