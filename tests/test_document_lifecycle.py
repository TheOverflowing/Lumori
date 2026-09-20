"""Owner-scoped, reversible document participation independent of index state."""
import json
import pytest
from fastapi.testclient import TestClient
from app.models import GenerateRequest
from app import document_jobs
from test_workflow import rig,seed,done,generated,authenticate


def test_disabled_document_keeps_index_and_is_excluded(rig):
    c,app,wire=rig;cid,did=seed(c);before=app.state.store.document_chunks(did)
    assert c.patch('/api/documents/'+did,json={'enabled':False}).status_code==200
    row=c.get('/api/documents',params={'course_id':cid}).json()[0]
    assert row['enabled'] is False and row['index_current']
    assert app.state.store.document_chunks(did)==before
    calls=wire.calls
    for explicit in [False,True]:
        request=GenerateRequest(course_id=cid,topic='课程检索',request_key='test-disabled',document_ids=[did] if explicit else [])
        with pytest.raises(ValueError):c.portal.call(app.state.pipeline.retrieve,request,'test-query')
    assert wire.calls==calls
    c.patch('/api/documents/'+did,json={'enabled':True})
    rows=c.portal.call(app.state.pipeline.retrieve,GenerateRequest(course_id=cid,topic='课程检索',request_key='test-enabled'),'test-query')
    assert rows and rows[0]['document_id']==did


def test_trash_restore_preserves_old_content_citations_and_index(rig):
    c,app,wire=rig;cid,did=seed(c);content=generated(c,cid)
    chunks=app.state.store.document_chunks(did)
    assert c.delete('/api/documents/'+did).json()['deleted_at']
    assert c.get('/api/documents',params={'course_id':cid}).json()==[]
    deleted=c.get('/api/documents',params={'course_id':cid,'include_deleted':True}).json()
    assert len(deleted)==1 and not deleted[0]['enabled']
    assert c.get('/api/documents/'+did+'/source').status_code==200
    assert c.get('/api/contents/'+content).status_code==200
    assert c.patch('/api/documents/'+did,json={'enabled':True}).status_code==409
    assert c.post('/api/documents/'+did+'/index').status_code==409
    restored=c.post('/api/documents/'+did+'/restore').json()
    assert restored['deleted_at'] is None and not restored['enabled']
    assert app.state.store.document_chunks(did)==chunks
    c.patch('/api/documents/'+did,json={'enabled':True})
    assert done(c,c.post('/api/documents/'+did+'/index'))['result']['reused']


def test_foreign_accounts_cannot_change_or_restore(rig):
    c,app,wire=rig;cid,did=seed(c)
    other=TestClient(app);other.portal=c.portal
    try:
        authenticate(other,'different@example.test')
        for method,path,kwargs in [('PATCH','',{'json':{'enabled':False}}),('DELETE','',{}),('POST','/restore',{})]:
            assert other.request(method,'/api/documents/'+did+path,**kwargs).status_code==404
        assert other.get('/api/documents',params={'course_id':cid,'include_deleted':True}).status_code==404
        assert c.get('/api/documents',params={'course_id':cid}).json()[0]['enabled']
    finally:other.close();other.portal=None


def test_image_retrieval_also_excludes_disabled_and_deleted(rig):
    c,app,wire=rig;cid,did=seed(c);store=app.state.store
    report=json.loads(store.one('SELECT report FROM document_parsing WHERE document_id=?',(did,))['report'])
    report['assets']=[{'id':'image','kind':'figure','page':1}]
    store.execute('UPDATE document_parsing SET report=? WHERE document_id=?',(json.dumps(report),did))
    desc={'data':dict(description='二叉树图',keywords_zh=[],keywords_en=[],visible_text='',relationships=[],uncertainties=[]),'origin':'model'}
    store.execute('INSERT INTO figure_semantics(document_id,asset_id,status,description,vector,embedding_signature,updated_at) VALUES(?,?,?,?,?,?,?)',
        (did,'image','ready',json.dumps(desc),'[1,.5,.25]'.replace('.5','0.5').replace('.25','0.25'),app.state.pipeline.embedding_signature(),'test'))
    assert len(document_jobs.retrieval_rows(app.state.pipeline,cid))==1
    c.patch('/api/documents/'+did,json={'enabled':False})
    assert document_jobs.retrieval_rows(app.state.pipeline,cid)==[]
    c.patch('/api/documents/'+did,json={'enabled':True});c.delete('/api/documents/'+did)
    assert document_jobs.retrieval_rows(app.state.pipeline,cid)==[]


def test_duplicate_upload_reports_trash_without_silent_restore(rig):
    c,app,wire=rig;cid,did=seed(c);c.delete('/api/documents/'+did)
    response=c.post('/api/documents',data={'course_id':cid},files={'file':('lecture.md','检索为生成提供课程参考资料。'.encode())})
    assert response.json()['duplicate'] and response.json()['deleted_at']
