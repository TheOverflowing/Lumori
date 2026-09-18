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
def test_malformed_text_gets_only_one_repair(rig,repair_succeeds):
    c,app,wire=rig;course,_=seed(c)
    provider=app.state.pipeline.providers
    original=provider.generate
    attempts=[]
    async def generate(messages,job_id):
        if json.loads(messages[1]['content']).get('task')=='difficulty_assessment':
            return await original(messages,job_id)
        attempts.append(messages.copy())
        if len(attempts)==1 or not repair_succeeds:raise ProviderOutputError('文本 JSON 不完整。')
        return await original(messages,job_id)
    provider.generate=generate
    job=done(c,c.post('/api/generations',json=request(course)))
    assert len(attempts)==2
    assert job['status']==('succeeded' if repair_succeeds else 'failed')
    evidence=c.get('/api/jobs/'+job['id']+'/evidence').json()
    assert evidence['evidence']['sources'] and len(evidence['evidence']['attempts'])==2


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
