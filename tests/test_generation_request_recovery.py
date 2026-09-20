"""Saved request recovery is read-only, owner-scoped and never reuses job identity."""
import json
import pytest
from app.models import GenerateRequest
from app.store import dumps
from app.generation_preparation import fingerprint
from test_workflow import rig, seed, generated
from test_account_isolation import another_client, register


def test_saved_request_recovers_inputs_without_starting_work_or_reusing_tokens(rig):
    client,app,wire=rig
    course,_=seed(client);cid=generated(client,course)
    content=client.get('/api/contents/'+cid).json();jid=content['job_id']
    app.state.store.execute("UPDATE jobs SET status='failed' WHERE id=?",(jid,))
    before=wire.calls
    response=client.get(f'/api/jobs/{jid}/generation-request')
    assert response.status_code==200,response.text
    request=response.json()['request']
    assert request['course_id']==course and request['count']==1
    assert request['topic']==json.loads(app.state.store.one('SELECT payload FROM jobs WHERE id=?',(jid,))['payload'])['topic']
    assert not {'request_key','preparation_id','clarification_action','text_model','sources'} & request.keys()
    assert wire.calls==before
    assert app.state.store.one('SELECT status FROM jobs WHERE id=?',(jid,))['status']=='failed'
    with another_client(app,client) as other:
        register(other,'recovery-other@example.test','Other')
        assert other.get(f'/api/jobs/{jid}/generation-request').status_code==404


def test_recovery_keeps_clarification_scope_after_expiry_but_excludes_used_preparation(rig):
    client,app,wire=rig;course,_=seed(client);store=app.state.store
    owner=store.one('SELECT user_id FROM course_owners WHERE course_id=?',(course,))['user_id']
    request=GenerateRequest(course_id=course,topic='Explain the lock.',count=2,language='en',
        difficulty_distribution={'easy':0,'medium':0,'hard':2},request_key='recover-with-clarification')
    prep,_=store.job('prepare-recovery','prepare',request.model_dump(),owner_id=owner)
    result={'preparation_id':prep['id'],'status':'clarification_required','question':'Which kind of lock?',
        'options':['Database locking','Thread locking'],'request_fingerprint':fingerprint(request),'expires_at':1}
    store.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?",(dumps(result),prep['id']))
    answered=request.model_copy(update={'preparation_id':prep['id'],'clarification_action':'answer','clarification_answer':'Database locking'})
    job,_=store.job(answered.request_key,'generate',answered.model_dump(),owner_id=owner,preparation_id=prep['id'])
    store.execute("UPDATE jobs SET status='failed' WHERE id=?",(job['id'],))
    before=wire.calls
    response=client.get(f'/api/jobs/{job["id"]}/generation-request')
    assert response.status_code==200,response.text
    recovered=response.json()['request']
    assert 'Explain the lock.' in recovered['topic'] and 'Database locking' in recovered['topic']
    assert 'Which kind of lock?' in recovered['topic']
    assert recovered['difficulty_distribution']=={'easy':0,'medium':0,'hard':2}
    assert 'preparation_id' not in recovered and 'clarification_answer' not in recovered
    assert wire.calls==before


@pytest.mark.parametrize('status,expected',[('strict','model_checked'),('adjusted','model_adjusted')])
def test_current_calibration_is_visible_but_does_not_verify_edited_versions(rig,status,expected):
    client,app,_=rig;course,_=seed(client);cid=generated(client,course);store=app.state.store
    row=store.one('SELECT * FROM contents WHERE id=?',(cid,));config=json.loads(row['config'])
    assessed='hard' if status=='strict' else 'medium'
    config['difficulty_acceptance']={'status':status,'strict_passed':status=='strict','rounds':3,
        'items':[{'slot_id':'q1','target_difficulty':'hard','assessed_difficulty':assessed,'confidence':'high'}]}
    store.save_job_evidence(row['job_id'],{'attempts':[{'status':'valid' if status=='strict' else 'accepted_adjusted',
        'assessment':{'response':{'items':[{'slot_id':'q1','assessed_difficulty':assessed,
            'rationale':'The task requires applying a known concept.','answerable_from_sources':True}]}}}]})
    store.execute('UPDATE contents SET config=? WHERE id=?',(dumps(config),cid))
    checked=client.get('/api/contents/'+cid).json()['difficulty_assessment']
    assert checked['status']==expected and checked['items'][0]['assessed_difficulty']==assessed
    assert checked['items'][0]['rationale']=='The task requires applying a known concept.'
    store.execute('UPDATE contents SET version=2 WHERE id=?',(cid,))
    checked=client.get('/api/contents/'+cid).json()['difficulty_assessment']
    assert checked['status']=='needs_review' and 'acceptance' not in checked


@pytest.mark.parametrize('raw_flag,config_flag,expected',[
    (False,False,False),(True,True,True),(None,False,False),(None,None,True),
])
def test_recovery_preserves_introduction_choice_and_legacy_requests(rig,raw_flag,config_flag,expected):
    client,app,_=rig;course,_=seed(client);cid=generated(client,course);store=app.state.store
    row=store.one('SELECT job_id FROM contents WHERE id=?',(cid,));jid=row['job_id']
    payload=json.loads(store.one('SELECT payload FROM jobs WHERE id=?',(jid,))['payload'])
    if raw_flag is None:payload.pop('include_explanations',None)
    else:payload['include_explanations']=raw_flag
    store.execute('UPDATE jobs SET payload=? WHERE id=?',(dumps(payload),jid))
    evidence=json.loads(store.one('SELECT evidence FROM job_evidence WHERE job_id=?',(jid,))['evidence'])
    if config_flag is None:evidence['configuration'].pop('include_explanations',None)
    else:evidence['configuration']['include_explanations']=config_flag
    store.save_job_evidence(jid,evidence)
    response=client.get(f'/api/jobs/{jid}/generation-request')
    assert response.status_code==200,response.text
    assert response.json()['request']['include_explanations'] is expected
