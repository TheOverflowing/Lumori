"""Isolated adapter fixtures: lifecycle, provenance, cache and account boundaries."""
import asyncio
import io
import json
import time
from contextlib import contextmanager
import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from app import document_backends, document_jobs
from app.config import Settings,Endpoint
from app.main import create_app
from app.providers import ApiProviders
from app.document_parsing import ParsedDocument,ParsedPage,VisualAsset
from app.models import GenerateRequest
from app.rag_runtime import runtime_context
from app.rag_candidates import TokenCounter
from test_workflow import authenticate,done

DESCRIPTION=dict(description='A binary tree 二叉树 with root A and children B and C.',keywords_zh=['二叉树'],keywords_en=['binary tree'],visible_text='A B C',relationships=['A connects to B and C'],uncertainties=[])

class VisionWire:
    vision=0;embeddings=0;fail_embed=False
    def __call__(self,request):
        data=json.loads(request.content)
        if request.url.path.endswith('embeddings'):
            self.embeddings+=1
            if self.fail_embed:return httpx.Response(503,text='private upstream response')
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,.4]} for i in range(len(data['input']))]})
        self.vision+=1
        content=data['messages'][1]['content']
        assert content[1]['image_url']['url'].startswith('data:image/png;base64,')
        assert 'original_caption' in content[0]['text']
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(DESCRIPTION)}}]})

@pytest.fixture
def rig(tmp_path,monkeypatch):
    async def parse(settings,name,raw,tier):
        assert tier in ('standard','advanced')
        return ParsedDocument(name,'mineru_'+tier,[ParsedPage(1,'Binary trees store nodes. 二叉树。',method='mineru_'+tier,status='good')])
    async def figures(settings,name,raw):
        return [VisualAsset('figure-fixture',1,'figure','image/png',b'fixture image',20,20,
                            {'bbox':[.1,.1,.8,.8],'original_caption':'Figure 1'})],False
    monkeypatch.setattr(document_backends,'parse',parse)
    monkeypatch.setattr(document_backends,'extract_figures',figures)
    settings=Settings(data_dir=tmp_path,document_backend='mineru')
    settings.vision=Endpoint('https://fixture.invalid','not-a-real-key','vision-fixture','/chat/completions')
    settings.embedding=Endpoint('https://fixture.invalid','not-a-real-key','embedding-fixture','/embeddings')
    wire=VisionWire()
    app=create_app(settings,lambda s,db:ApiProviders(s,db,httpx.AsyncClient(transport=httpx.MockTransport(wire))))
    with TestClient(app) as client:
        authenticate(client)
        yield client,app,wire


def await_job(client,jid):
    for _ in range(400):
        job=client.get('/api/jobs/'+jid).json()
        if job['status'] not in ('queued','running'):return job
        time.sleep(.005)
    pytest.fail('job timeout')


def upload(client,images=True,tier='advanced'):
    cid=client.post('/api/courses',json={'name':'CS diagrams'}).json()['id']
    raw=io.BytesIO();w=PdfWriter();w.add_blank_page(200,200);w.write(raw)
    response=client.post('/api/documents',data={'course_id':cid,'tier':tier,'images':str(images).lower()},files={'file':('tree.pdf',raw.getvalue())})
    assert response.status_code==201,response.text
    result=response.json();job=await_job(client,result['job_id']);assert job['status']=='succeeded',job
    return cid,result['id'],job


def test_parse_enrich_retrieve_and_cache(rig):
    c,app,wire=rig
    cid,did,parse=upload(c)
    figures=await_job(c,parse['result']['figures_job']['job_id'])
    assert figures['status']=='succeeded',figures
    assert wire.vision==1
    report=c.get(f'/api/documents/{did}/parsing').json()
    assert report['options']=={'tier':'advanced','images':True}
    assert report['pages'][0]['text']=='Binary trees store nodes. 二叉树。'
    assert report['assets'][0]['bbox']==[.1,.1,.8,.8]
    assert report['figures'][0]['annotation']['verified'] is False
    assert 'storage_name' not in report['assets'][0]
    assert done(c,c.post(f'/api/documents/{did}/index'))['status']=='succeeded'
    result=c.portal.call(app.state.pipeline.retrieve,GenerateRequest(course_id=cid,topic='binary tree',material='lesson',request_key='fixture-query'),'fixture-query')
    figure=next(r for r in result if r['metadata'].get('source_kind')=='figure')
    assert figure['metadata']['source_asset_ids']==['figure-fixture']
    assert '二叉树' in figure['text']
    retried=done(c,c.post(f'/api/documents/{did}/figures/retry'))
    assert retried['result']['reused']==1 and wire.vision==1
    row=app.state.store.one('SELECT course_id FROM documents WHERE id=?',(did,))
    assert row['course_id']==cid


def test_embedding_failure_retains_description_for_retry_and_manual_edit(rig):
    c,app,wire=rig;wire.fail_embed=True
    cid,did,job=upload(c)
    failed=await_job(c,job['result']['figures_job']['job_id'])
    assert failed['result']['failed']==1
    states=c.get(f'/api/documents/{did}/figures').json()
    assert states[0]['status']=='failed' and states[0]['annotation']
    wire.fail_embed=False
    assert done(c,c.post(f'/api/documents/{did}/figures/retry'))['result']['succeeded']==1
    assert wire.vision==1
    edit=c.patch(f'/api/documents/{did}/figures/figure-fixture',json={'description':'Corrected tree explanation'})
    assert edit.status_code==200
    assert app.state.store.one('SELECT vector FROM figure_semantics WHERE document_id=?',(did,))['vector'] is None
    assert done(c,c.post(f'/api/documents/{did}/figures/retry'))['result']['succeeded']==1
    assert wire.vision==1
    assert c.get(f'/api/documents/{did}/figures').json()[0]['annotation']['origin']=='user'


def test_disabled_images_no_vision_calls_and_reparse_removes_index(rig):
    c,app,wire=rig;cid,did,job=upload(c)
    await_job(c,job['result']['figures_job']['job_id'])
    old=c.get(f'/api/documents/{did}/parsing').json()['assets'][0]['url']
    response=c.post(f'/api/documents/{did}/parse',json={'tier':'standard','images':False})
    assert await_job(c,response.json()['job_id'])['status']=='succeeded'
    assert c.get(f'/api/documents/{did}/figures').json()==[]
    assert c.get(old).status_code==200 # retained historical citation
    assert wire.vision==1
    cid,disabled,job=upload(c,False)
    assert 'figures_job' not in job['result']
    assert wire.vision==1


def test_foreign_account_cannot_access_or_trigger_figures(rig):
    c,app,wire=rig;cid,did,job=upload(c)
    await_job(c,job['result']['figures_job']['job_id'])
    other=TestClient(app);other.portal=c.portal
    try:
        authenticate(other,'other@example.test')
        before=wire.vision
        for method,path,kwargs in [('GET','figures',{}),('POST','figures/retry',{}),
            ('PATCH','figures/figure-fixture',{'json':{'description':'bad edit'}}),('GET','assets/figure-fixture',{})]:
            assert other.request(method,f'/api/documents/{did}/{path}',**kwargs).status_code==404
        assert wire.vision==before
        assert other.get('/api/jobs').json()==[]
    finally:other.close();other.portal=None


def test_parse_failure_keeps_previous_text_and_index(rig,monkeypatch):
    c,app,wire=rig;cid,did,job=upload(c,False)
    assert done(c,c.post(f'/api/documents/{did}/index'))['status']=='succeeded'
    before=app.state.store.document_chunks(did)
    async def fail(*args):raise ValueError('Fixture parser failure')
    monkeypatch.setattr(document_backends,'parse',fail)
    job=c.post(f'/api/documents/{did}/parse',json={'tier':'advanced','images':False}).json()
    assert await_job(c,job['job_id'])['status']=='failed'
    assert app.state.store.document_chunks(did)==before
    assert app.state.store.one('SELECT status FROM documents WHERE id=?',(did,))['status']=='ready'


def test_figure_context_uses_description_coordinates():
    from app.config import ROOT
    counter=TokenCounter(ROOT/'.rag-models/embedding-tokenizer/tokenizer.json')
    ranked=[dict(id='figure_f',document_id='d',document_name='tree.pdf',page=1,text=DESCRIPTION['description'],score=1,
        retrieval={},metadata={'source_kind':'figure','source_asset_ids':['figure-fixture']})]
    result,tokens=runtime_context(ranked,{'d':['UNRELATED original OCR']},counter,1024,'parent',5)
    assert result[0]['text']==DESCRIPTION['description']
    assert result[0]['metadata']['coordinate_system']=='image_description_chars_v1'
    assert result[0]['metadata']['source_asset_ids']==['figure-fixture']
    assert tokens<=1024


def test_json_transport_marker_is_not_a_semantic_field():
    from app.figure_schema import validate_description
    assert validate_description(DESCRIPTION|{'type':'json_object'}).model_dump()==DESCRIPTION
    with pytest.raises(ValueError):validate_description(DESCRIPTION|{'unexpected':'field'})
    with pytest.raises(ValueError):validate_description(DESCRIPTION|{'type':'invented'})


def test_caption_binding_requires_same_page_and_overlap():
    from app.document_jobs import bind_captions
    asset=VisualAsset('figure-a',1,'figure','image/png',b'data',provenance={'bbox':[.1,.1,.8,.8],'original_caption':''})
    report={'pages':[{'number':1,'blocks':[{'type':'image','bbox':[.1,.1,.8,.8],'text':'Figure 1. A binary tree'}]},
                     {'number':2,'blocks':[{'type':'image','bbox':[.1,.1,.8,.8],'text':'Wrong page'}]}]}
    bind_captions([asset],report)
    assert asset.provenance['original_caption']=='Figure 1. A binary tree'
    assert asset.provenance['caption_match_iou']==1
    unmatched=VisualAsset('figure-b',3,'figure','image/png',b'data',provenance={'bbox':[.1,.1,.8,.8],'original_caption':''})
    bind_captions([unmatched],report)
    assert unmatched.provenance['original_caption']==''
