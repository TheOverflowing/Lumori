"""Contract and integration tests with isolated HTTP fixtures, never live model calls."""
import base64
import json
import time
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings, Endpoint
from app.main import create_app
from app.providers import ApiProviders
from app.store import Store

def apply_difficulty_fixture(asset,instruction):
    plan=instruction.get('difficulty_plan',[])
    for question,slot in zip(asset['questions'],plan):
        level=slot['difficulty']
        question.update(slot_id=slot['slot_id'],difficulty=level,difficulty_design={
            'cognitive_process':{'easy':'understand','medium':'apply','hard':'evaluate'}[level],
            'concepts':['课程证据','审核约束'],
            'expected_steps':['识别条件','应用规则','比较方案并论证'][:{'easy':1,'medium':2,'hard':3}[level]]})
    return {slot['slot_id']:slot['difficulty'] for slot in plan}

def difficulty_judge_fixture(instruction,levels):
    return {'items':[{'slot_id':q['slot_id'],'assessed_difficulty':levels[q['slot_id']],
        'confidence':'high','rationale':'Isolated fixture classification, not a real difficulty judgment.',
        'answerable_from_sources':True,'ambiguity_free':True} for q in instruction['questions']]}

class Wire:
    mode='valid'
    calls=0
    def __call__(self,request):
        self.calls+=1
        data=json.loads(request.content)
        assert request.headers['authorization']=='Bearer test-key-not-real'
        if self.mode=='http_error':return httpx.Response(401,text='test-key-not-real secret upstream body')
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,.5,.25]} for i,_ in enumerate(data['input'])]})
        if request.url.path.endswith('/chat/completions'):
            instruction=json.loads(data['messages'][1]['content'])
            if instruction.get('task')=='difficulty_assessment':
                asset=difficulty_judge_fixture(instruction,self.difficulty_levels)
                return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(asset)}}]})
            req=instruction['request']
            refs=[instruction['reference_chunks'][0]['id'] if self.mode!='bad_citation' else 'invented']
            asset={'title':'检索与生成','evidence_sufficient':self.mode!='insufficient','sections':[], 'questions':[], 'visual_prompt':'课程资料到检索结果的流程图'}
            if asset['evidence_sufficient']:
                if req.get('include_explanations') or req['material']=='lesson':
                    asset['sections']=[{'heading':'知识讲解','text':'检索为生成提供参考资料。','citation_ids':refs}]
                asset['questions']=[{'kind':'mcq','stem':f'第 {i+1} 题：检索的作用？','options':['提供参考','保证正确','取代审核','更新模型参数'],'answer':'A','explanation':'参考仍需核对。','difficulty_reason':'单一概念识别','citation_ids':refs} for i in range(instruction['question_count'])]
            self.difficulty_levels=apply_difficulty_fixture(asset,instruction)
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(asset)}}]})
        if request.url.path.endswith('/audio/speech'):
            assert data['response_format']=='mp3'
            self.speech_input=data['input']
            return httpx.Response(200,content=b'ID3transport-fixture-only',headers={'content-type':'audio/mpeg'})
        if request.url.path.endswith('/images/generations'):
            return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(b'\x89PNG\r\n\x1a\ntransport-fixture-only').decode()}]})
        raise AssertionError(request.url)

def authenticate(client, email='fixture@example.test', password='Fixture account password 2026!'):
    response=client.post('/api/auth/register',json={'email':email,'password':password,'display_name':'Fixture account'})
    assert response.status_code==201,response.text
    session=response.json()
    client.headers.update({'X-CSRF-Token':session['csrf_token'],'X-Account-ID':session['user']['id']})
    return session['user']

@pytest.fixture
def rig(tmp_path):
    settings=Settings(data_dir=tmp_path)
    for cap,path in [('text','/chat/completions'),('embedding','/embeddings'),('speech','/audio/speech'),('image','/images/generations')]:
        setattr(settings,cap,Endpoint('https://test.invalid/v1','test-key-not-real','fixture-model',path))
    wire=Wire()
    app=create_app(settings,lambda s,db:ApiProviders(s,db,httpx.AsyncClient(transport=httpx.MockTransport(wire))))
    with TestClient(app) as client:
        authenticate(client)
        yield client,app,wire

def done(client,response):
    assert response.status_code==202,response.text
    jid=response.json()['job_id']
    for _ in range(200):
        job=client.get('/api/jobs/'+jid).json()
        if job['status'] not in ('queued','running'):return job
        time.sleep(.005)
    pytest.fail('job did not finish')

def seed(client):
    course=client.post('/api/courses',json={'name':'测试课程'}).json()['id']
    doc=client.post('/api/documents',data={'course_id':course},files={'file':('lecture.md','检索为生成提供课程参考资料。'.encode())}).json()['id']
    assert done(client,client.post('/api/documents/'+doc+'/index'))['status']=='succeeded'
    return course,doc

def request(course,key='generate-001'):
    return dict(course_id=course,topic='检索的作用',count=1,question_type='mcq',request_key=key,
                include_explanations=True)

def generated(client,course):
    job=done(client,client.post('/api/generations',json=request(course)))
    assert job['status']=='succeeded',job
    return job['result']['content_id']

def test_full_review_media_version_workflow(rig):
    c,app,wire=rig;course,doc=seed(c);cid=generated(c,course)
    assert c.get('/api/learn/'+cid).status_code==404
    assert c.post('/api/contents/'+cid+'/review',json={'version':1,'action':'approve'}).status_code==200
    assert 'answer' not in c.get('/api/learn/'+cid).json()['asset']['questions'][0]
    assert c.get('/api/learn/'+cid+'?reveal_answers=true').json()['asset']['questions'][0]['answer']=='A'
    for kind in ('audio','image'):
        job=done(c,c.post('/api/contents/'+cid+'/media',json={'version':1,'kind':kind,'request_key':'media-'+kind}))
        assert job['status']=='succeeded',job
    content=c.get('/api/contents/'+cid).json()
    assert len(content['media'])==2
    assert c.get('/api/learn/'+cid).json()['media']==[]
    for m in content['media']:
        assert c.post('/api/media/'+m['id']+'/approve',json={'version':1}).status_code==200
    assert len(c.get('/api/learn/'+cid).json()['media'])==2
    assert c.post('/api/contents/'+cid+'/review',json={'version':1,'action':'approve','asset':content['asset']}).status_code==400
    content['asset']['title']='修订标题'
    assert c.post('/api/contents/'+cid+'/review',json={'version':1,'action':'save','asset':content['asset']}).json()['version']==2
    assert c.get('/api/learn/'+cid).status_code==404
    assert c.post('/api/contents/'+cid+'/review',json={'version':1,'action':'approve'}).status_code==409
    assert c.post('/api/contents/'+cid+'/review',json={'version':2,'action':'approve'}).status_code==200
    assert c.get('/api/learn/'+cid).json()['media']==[]
    assert c.post('/api/media/'+content['media'][0]['id']+'/approve',json={'version':1}).status_code==409

def test_questions_only_assessment_audio_has_prompts_but_no_answers(rig):
    client,app,wire=rig
    course,_=seed(client)
    payload=request(course,'questions-only-audio') | {'include_explanations':False}
    generated_job=done(client,client.post('/api/generations',json=payload))
    assert generated_job['status']=='succeeded',generated_job
    cid=generated_job['result']['content_id']
    asset=client.get('/api/contents/'+cid).json()['asset']
    assert asset['sections']==[]
    assert client.post('/api/contents/'+cid+'/review',json={'version':1,'action':'approve'}).status_code==200
    media_job=done(client,client.post('/api/contents/'+cid+'/media',json={
        'version':1,'kind':'audio','request_key':'questions-only-speech'}))
    assert media_job['status']=='succeeded',media_job
    assert asset['questions'][0]['stem'] in wire.speech_input
    assert 'A. 提供参考' in wire.speech_input
    assert asset['questions'][0]['explanation'] not in wire.speech_input

@pytest.mark.parametrize('mode,status',[('bad_citation','failed'),('insufficient','insufficient_evidence')])
def test_invalid_or_insufficient_never_published(rig,mode,status):
    c,app,wire=rig;course,_=seed(c);wire.mode=mode
    assert done(c,c.post('/api/generations',json=request(course)))['status']==status
    assert c.get('/api/contents',params={'course_id':course}).json()==[]
    text_calls=app.state.store.all("SELECT * FROM calls WHERE capability='text'")
    assert len(text_calls)==(3 if mode=='bad_citation' else 1)

def test_signature_change_blocks_stale_vectors(rig):
    c,app,wire=rig;course,_=seed(c)
    app.state.settings.embedding=replace(app.state.settings.embedding,model='new-model')
    before=wire.calls
    job=done(c,c.post('/api/generations',json=request(course)))
    assert job['status']=='failed' and '嵌入配置已更换' in job['error']
    assert wire.calls==before

def test_idempotency_and_course_isolation(rig):
    c,app,wire=rig;course,doc=seed(c);cid=generated(c,course);before=wire.calls
    retry=c.post('/api/generations',json=request(course))
    assert retry.json()['reused'] is True and wire.calls==before
    assert c.post('/api/generations',json=request(course)|{'count':2}).status_code==409
    another=c.post('/api/courses',json={'name':'另一个课程'}).json()['id']
    assert c.post('/api/generations',json=request(another,'other-key')|{'document_ids':[doc]}).status_code==400

def test_upstream_error_redacted_no_retry(rig):
    c,app,wire=rig;course,_=seed(c);wire.mode='http_error';before=wire.calls
    job=done(c,c.post('/api/generations',json=request(course)))
    assert job['status']=='failed' and 'HTTP 401' in job['error']
    assert 'test-key' not in json.dumps(job) and wire.calls==before+1

def test_unconfigured_import_duplicate_and_origin(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as c:
        assert c.post('/api/courses',json={'name':'x'},headers={'origin':'https://evil.invalid'}).status_code==403
        authenticate(c)
        course=c.post('/api/courses',json={'name':'资料'}).json()['id']
        def upload():return c.post('/api/documents',data={'course_id':course},files={'file':('notes.md','课程资料'.encode())})
        doc=upload().json();assert upload().json()['duplicate'] is True
        assert c.post('/api/documents/'+doc['id']+'/index').status_code==503
        assert c.get('/api/status').json()['calls_today']==0

def test_call_budget_counts_failed_attempts(rig):
    c,app,wire=rig;course,_=seed(c);app.state.settings.max_daily_calls=1
    before=wire.calls
    job=done(c,c.post('/api/generations',json=request(course)))
    assert job['status']=='failed' and '上限' in job['error'] and wire.calls==before

def test_evidence_export_preserves_sources_without_credentials(rig):
    c,app,wire=rig;course,_=seed(c);cid=generated(c,course)
    result=c.get('/api/contents/'+cid+'/evidence')
    assert result.status_code==200 and 'attachment' in result.headers['content-disposition']
    bundle=result.json();assert bundle['schema_version']=='ca1-evidence-v1'
    assert bundle['asset']['questions'][0]['citation_ids'][0]==bundle['sources'][0]['id']
    assert len(bundle['revisions'])==1 and bundle['configuration']['prompt_version']
    assert {r['capability'] for r in bundle['generation_calls']}=={'embedding','text'}
    assert 'test-key-not-real' not in result.text
    exported=c.get('/api/contents/'+cid+'/export').text
    assert bundle['sources'][0]['id'] in exported.split('## 资料依据')[0]
