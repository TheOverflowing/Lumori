"""Provider compatibility, metering and failure regressions; no external calls."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from app.config import Endpoint, Settings
from app.providers import ApiProviders, ProviderError, ProviderOutputError
from app.store import Store
from test_workflow import rig, seed, done, request


def invoke(tmp_path, response, capability='text', check_request=None):
    settings=Settings(data_dir=tmp_path, text_json_mode=True)
    endpoint=Endpoint('https://provider.invalid/v1','fixture-secret','fixture-model','/chat/completions')
    settings.text=endpoint
    settings.embedding=Endpoint(endpoint.base_url,endpoint.api_key,'fixture-embedding','/embeddings')
    store=Store(tmp_path)
    def wire(req):
        if check_request:check_request(req)
        return response
    async def run():
        provider=ApiProviders(settings,store,httpx.AsyncClient(transport=httpx.MockTransport(wire)))
        try:
            if capability=='text':return await provider.generate([{'role':'user','content':'Return JSON.'}],None)
            return await provider.embed(['first passage','second passage'])
        finally:await provider.close()
    return run,store


def test_json_mode_and_safe_numeric_telemetry(tmp_path):
    def check(req):
        body=json.loads(req.content)
        assert body['response_format']=={'type':'json_object'} and body['stream'] is False
        assert req.headers['authorization']=='Bearer fixture-secret'
    response=httpx.Response(200,json={'model':'fixture-secret','choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
        'usage':{'prompt_tokens':12,'completion_tokens':3,'cost':0.001,'secret':'fixture-secret',
                 'total_tokens':'fixture-secret','prompt_tokens_details':{'cached_tokens':2,'debug':'fixture-secret'}}})
    run,store=invoke(tmp_path,response,check_request=check)
    assert asyncio.run(run())=={'ok':True}
    call=store.one('SELECT * FROM calls')
    assert call['status']=='succeeded' and call['duration_ms']>=0 and call['http_status']==200
    assert call['response_model'] is None and 'fixture-secret' not in json.dumps(call)
    assert json.loads(call['usage'])=={'prompt_tokens':12,'completion_tokens':3,'cost':0.001,'prompt_tokens_details':{'cached_tokens':2}}


@pytest.mark.parametrize('body',[[],None,{'error':{'message':'fixture-secret'}}])
def test_bad_envelope_finalizes_call_without_echo(tmp_path,body):
    run,store=invoke(tmp_path,httpx.Response(200,content=json.dumps(body),headers={'content-type':'application/json'}))
    with pytest.raises(ProviderError) as error:asyncio.run(run())
    assert 'fixture-secret' not in str(error.value)
    assert store.one('SELECT status FROM calls')['status']=='failed'


@pytest.mark.parametrize('rows',[
    [{'index':0,'embedding':[1.,0.]},{'index':0,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[0.,0.]},{'index':1,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[True,0.]},{'index':1,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[1.]},{'index':1,'embedding':[1.,0.]}],
])
def test_invalid_embeddings_fail_call(tmp_path,rows):
    run,store=invoke(tmp_path,httpx.Response(200,json={'data':rows}),'embedding')
    with pytest.raises(ProviderError,match='向量'):asyncio.run(run())
    assert store.one('SELECT status FROM calls')['status']=='failed'


def test_embeddings_restore_input_order(tmp_path):
    rows=[{'index':1,'embedding':[0.,1.]},{'index':0,'embedding':[1.,0.]}]
    run,_=invoke(tmp_path,httpx.Response(200,json={'data':rows}),'embedding')
    assert asyncio.run(run())==[[1.,0.],[0.,1.]]


@pytest.mark.parametrize('repair_succeeds',[True,False])
def test_malformed_text_repairs_obey_provider_budget(rig,monkeypatch,repair_succeeds):
    c,app,wire=rig;course,_=seed(c)
    app.state.settings.max_daily_calls=8
    original=type(wire).__call__
    attempts=[]
    def generate(self,http_request):
        response=original(self,http_request)
        if not http_request.url.path.endswith('/chat/completions'):
            return response
        messages=json.loads(http_request.content)['messages']
        if json.loads(messages[1]['content']).get('task')=='difficulty_assessment':
            return response
        attempts.append(messages)
        if len(attempts)==1 or not repair_succeeds:
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{unfinished'}}]})
        return response
    monkeypatch.setattr(type(wire),'__call__',generate)
    job=done(c,c.post('/api/generations',json=request(course)))
    assert job['status']==('succeeded' if repair_succeeds else 'failed')
    evidence=c.get('/api/jobs/'+job['id']+'/evidence').json()['evidence']
    assert evidence['sources']
    author_attempts=[a for a in evidence['attempts'] if a['status']!='provider_error']
    assert len(author_attempts)==len(attempts)
    if repair_succeeds:
        assert len(attempts)==2
    else:
        assert len(attempts)>3
        assert len(app.state.store.all('SELECT id FROM calls'))==8
        assert c.get('/api/contents',params={'course_id':course}).json()==[]


@pytest.mark.parametrize('content',['{unfinished','{"invalid":NaN}','{"invalid":1e999}'])
def test_invalid_json_marks_wire_call_failed(tmp_path,content):
    run,store=invoke(tmp_path,httpx.Response(200,json={'choices':[{'message':{'content':content}}]}))
    with pytest.raises(ProviderOutputError):asyncio.run(run())
    assert store.one('SELECT status FROM calls')['status']=='failed'


def test_old_database_additive_migration_and_export_filter(tmp_path):
    with sqlite3.connect(tmp_path/'studio.sqlite3') as db:
        db.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,job_id TEXT,capability TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,usage TEXT NOT NULL,created_at TEXT NOT NULL)')
        db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?)',('legacy','job','text','model','succeeded',json.dumps({'prompt_tokens':4,'debug':'fixture-secret'}),'2026-09-11'))
    store=Store(tmp_path)
    Store(tmp_path)  # Restarting an already migrated database is safe.
    legacy=store.calls_for_job('job')[0]
    assert legacy['usage']=={'prompt_tokens':4} and legacy['duration_ms'] is None
    assert store.reserve_call(None,'text','model',100)


@pytest.mark.parametrize('status,code,hint',[
    (503,'upstream_unavailable','模型服务暂不可用'),
    (502,'upstream_unavailable','网关暂时异常'),
    (500,'upstream_unavailable','模型服务暂时异常'),
    (504,'upstream_unavailable','模型服务响应超时'),
    (401,'authentication','密钥无效'),
    (402,'payment_required','账户余额不足'),
    (429,'rate_limited','平台限流'),
    (422,'configuration','请求参数无效'),
])
def test_http_failure_is_classified_without_exposing_body_or_retrying(tmp_path,status,code,hint):
    seen=[]
    run,store=invoke(tmp_path,httpx.Response(status,json={'error':{'message':'fixture-secret'}}),
                     check_request=lambda request:seen.append(request))
    with pytest.raises(ProviderError) as error:asyncio.run(run())
    assert error.value.http_status==status and error.value.code==code
    assert hint in str(error.value) and 'fixture-secret' not in str(error.value)
    assert len(seen)==1
    rows=store.all('SELECT * FROM calls')
    assert len(rows)==1 and rows[0]['status']=='failed' and rows[0]['http_status']==status


@pytest.mark.parametrize('error_type,code,hint',[
    (httpx.ReadTimeout,'timeout','响应超时'),
    (httpx.ConnectError,'network','网络连接异常'),
])
def test_transport_failure_is_classified_without_echo_or_retry(tmp_path,error_type,code,hint):
    seen=[]
    def fail(request):
        seen.append(request)
        raise error_type('fixture-secret',request=request)
    run,store=invoke(tmp_path,None,check_request=fail)
    with pytest.raises(ProviderError) as error:asyncio.run(run())
    assert error.value.code==code and error.value.http_status is None
    assert hint in str(error.value) and 'fixture-secret' not in str(error.value)
    assert len(seen)==1 and store.one('SELECT status FROM calls')['status']=='failed'
