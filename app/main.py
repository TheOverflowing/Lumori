from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import csv
import hashlib
import io
import json
import sqlite3
import shutil
from urllib.parse import urlparse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import Settings
from .store import Store, uid, now, dumps, Conflict
from .providers import ApiProviders
from .pipeline import Pipeline, parse_document, validate_asset, document_chunk_ids
from .models import CourseCreate, GenerateRequest, ReviewRequest, MediaRequest, MediaReview, EvaluationRequest, LearningAsset
from .auth import AuthService, AuthError
from . import document_storage, document_jobs, document_backends
from .document_parsing import parser_capabilities

STATIC=Path(__file__).parent/'static'

class ParseOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tier: str = Field(default='standard',pattern='^(standard|advanced)$')
    images: bool = False

class FigureEdit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    description: str = Field(min_length=1,max_length=2400)

class RegisterRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    email:str=Field(min_length=3,max_length=254)
    password:str=Field(min_length=15,max_length=128)
    display_name:str=Field(min_length=1,max_length=80)

class LoginRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    email:str=Field(min_length=1,max_length=254)
    password:str=Field(min_length=1,max_length=128)

class LegacyClaimRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token:str=Field(min_length=20,max_length=256)

def create_app(settings=None,providers_factory=ApiProviders):
    settings=settings or Settings.from_env()
    store=Store(settings.data_dir)
    providers=providers_factory(settings,store)
    pipeline=Pipeline(settings,store,providers,enforce_account_ownership=True)
    auth=AuthService(store,settings)
    @asynccontextmanager
    async def lifespan(app):
        await pipeline.start()
        yield
        await pipeline.stop()
    app=FastAPI(title='Lumori',version='0.1.0',lifespan=lifespan)
    app.state.store=store;app.state.pipeline=pipeline;app.state.settings=settings;app.state.auth=auth
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=settings.allowed_hosts)
    @app.get('/healthz',include_in_schema=False)
    async def health():
        return {'status':'ok'}

    @app.middleware('http')
    async def origin_guard(request,call_next):
        def guarded_response(body,status_code):
            return JSONResponse(body,status_code,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY'})
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            if (origin and origin.rstrip('/')!=str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site')=='cross-site':
                return guarded_response({'detail':'仅允许同源页面操作应用。','code':'origin_forbidden'},403)
        if request.url.path.startswith('/api/'):
            session=auth.authenticate(request.cookies.get(settings.auth_cookie_name))
            request.state.auth_session=session
            public=request.url.path in ('/api/auth/session','/api/auth/register','/api/auth/login')
            if not public and session is None:
                return guarded_response({'detail':'请先登录。','code':'authentication_required'},401)
            expected=request.headers.get('x-account-id')
            if expected and (session is None or expected!=session.user['id']):
                return guarded_response({'detail':'账号已切换，请重新载入当前工作区。','code':'account_changed'},401)
            if session and request.method not in ('GET','HEAD','OPTIONS') and not auth.check_csrf(session,request.headers.get('x-csrf-token')):
                return guarded_response({'detail':'登录状态已更新，请刷新页面后重试。','code':'csrf_invalid'},403)
        result=await call_next(request)
        result.headers['X-Content-Type-Options']='nosniff'
        result.headers['X-Frame-Options']='DENY'
        result.headers['Cache-Control']='no-store'
        result.headers['Referrer-Policy']='same-origin'
        return result
    @app.exception_handler(AuthError)
    async def auth_error(request,exc):
        headers={'Retry-After':str(exc.retry_after)} if exc.retry_after else {}
        return JSONResponse({'detail':exc.message,'code':exc.code},exc.status_code,headers=headers)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        # Never echo submitted passwords or request bodies in error responses.
        return JSONResponse({'detail':[{'loc':e['loc'],'msg':e['msg'],'type':e['type']} for e in exc.errors()]},422)
    app.mount('/static',StaticFiles(directory=STATIC),name='static')

    def account_id(request):return request.state.auth_session.user['id']
    def need_course(cid,request):
        if not store.one('SELECT course_id FROM course_owners WHERE course_id=? AND user_id=?',(cid,account_id(request))):raise HTTPException(404,'课程不存在')
    def need_document(did,request):
        row=store.one('''SELECT d.* FROM documents d JOIN course_owners o ON o.course_id=d.course_id
            WHERE d.id=? AND o.user_id=?''',(did,account_id(request)))
        if not row:raise HTTPException(404,'资料不存在')
        return row
    def need_content(cid,request):
        row=store.one('''SELECT c.* FROM contents c JOIN course_owners o ON o.course_id=c.course_id
            WHERE c.id=? AND o.user_id=?''',(cid,account_id(request)))
        if not row:raise HTTPException(404,'材料不存在')
        return row
    def difficulty_assessment_for(row):
        config=json.loads(row['config']) if isinstance(row['config'],str) else row['config']
        asset=json.loads(row['asset']) if isinstance(row['asset'],str) else row['asset']
        result={'status':'legacy_unverified','version':None,'items':[],
                'limitation':'模型按认知要求复核，不等于教师确认或学生实际作答难度。'}
        if 'difficulty_plan' not in config:return result
        if not asset['questions']:return result|{'status':'not_applicable'}
        saved=store.one('SELECT evidence FROM job_evidence WHERE job_id=?',(row['job_id'],))
        attempts=json.loads(saved['evidence']).get('attempts',[]) if saved else []
        for attempt in reversed(attempts):
            assessment=attempt.get('assessment')
            if attempt.get('status')=='valid' and isinstance(assessment,dict):
                response=assessment.get('response',assessment)
                result.update(version=1,items=response.get('items',[]))
                break
        result['status']='model_checked' if row['version']==1 and result['items'] else 'needs_review'
        return result
    def require(*capabilities):
        for cap in capabilities:
            try:getattr(settings,cap).require(cap)
            except ValueError as exc:raise HTTPException(503,str(exc)) from None
    def submit(key,kind,payload,request):
        try:job,new=store.job(key,kind,payload,owner_id=account_id(request))
        except Conflict as exc:raise HTTPException(409,str(exc)) from None
        if new:pipeline.enqueue(job['id'])
        return {'job_id':job['id'],'status':job['status'],'reused':not new}

    def save_source(source,raw):
        source.parent.mkdir(exist_ok=True)
        temporary=source.with_name(source.name+'.'+uid()+'.tmp')
        try:
            temporary.write_bytes(raw)
            temporary.replace(source)
        finally:temporary.unlink(missing_ok=True)

    @app.get('/')
    async def index():return FileResponse(STATIC/'index.html')
    @app.get('/api/auth/session')
    async def auth_session(request:Request):
        session=request.state.auth_session
        response=JSONResponse(session.response() if session else {'user':None,'csrf_token':None})
        if session is None and request.cookies.get(settings.auth_cookie_name):auth.clear_cookie(response)
        return response
    @app.post('/api/auth/register',status_code=201)
    async def register(payload:RegisterRequest,request:Request):
        session=await run_in_threadpool(auth.register,payload.email,payload.password,payload.display_name,
            client_ip=request.client.host if request.client else 'unknown')
        auth.logout(request.cookies.get(settings.auth_cookie_name))
        response=JSONResponse(session.response(),201);auth.set_cookie(response,session);return response
    @app.post('/api/auth/login')
    async def login(payload:LoginRequest,request:Request):
        session=await run_in_threadpool(auth.login,payload.email,payload.password,
            client_ip=request.client.host if request.client else 'unknown')
        auth.logout(request.cookies.get(settings.auth_cookie_name))
        response=JSONResponse(session.response());auth.set_cookie(response,session);return response
    @app.post('/api/auth/logout')
    async def logout(request:Request):
        auth.logout(request.cookies.get(settings.auth_cookie_name))
        response=JSONResponse({'user':None,'csrf_token':None});auth.clear_cookie(response);return response
    @app.post('/api/auth/claim-legacy')
    async def claim_legacy(payload:LegacyClaimRequest,request:Request):
        from .account_migration import claim_legacy_workspace
        return claim_legacy_workspace(store,request.state.auth_session.user,payload.token)
    @app.get('/api/status')
    async def status(request:Request):
        try:retrieval=pipeline.retrieval_configuration()
        except ValueError as exc:raise HTTPException(503,'检索配置无效：'+str(exc)) from None
        try:chunking=pipeline.chunking_configuration()
        except ValueError as exc:raise HTTPException(503,'切分配置无效：'+str(exc)) from None
        caps={name:{'configured':getattr(settings,name).configured,'model':getattr(settings,name).model} for name in ('text','embedding','speech','image','vision')}
        used=store.one('''SELECT count(*) AS n FROM calls c JOIN job_owners o ON o.job_id=c.job_id
            WHERE o.user_id=? AND substr(c.created_at,1,10)=?''',(account_id(request),now()[:10]))['n']
        return {'version':'0.1.0','capabilities':caps,'calls_today':used,'daily_call_limit':settings.max_daily_calls,'mode':'local-development',
                'document_parser':parser_capabilities()|document_backends.capabilities(settings)|{'ocr_engine':settings.document_ocr_engine},
                'retrieval':retrieval,'chunking':chunking}
    @app.get('/api/courses')
    async def courses(request:Request):return store.all('''SELECT c.* FROM courses c JOIN course_owners o ON o.course_id=c.id
        WHERE o.user_id=? ORDER BY c.created_at''',(account_id(request),))
    @app.post('/api/courses',status_code=201)
    async def course_create(payload:CourseCreate,request:Request):
        row={'id':uid(),'name':payload.name,'created_at':now()}
        with store.connect() as db:
            db.execute('INSERT INTO courses VALUES(?,?,?)',tuple(row.values()))
            db.execute('INSERT INTO course_owners(course_id,user_id) VALUES(?,?)',(row['id'],account_id(request)))
        return row
    @app.get('/api/documents')
    async def documents(course_id:str,request:Request):
        need_course(course_id,request)
        rows=store.all('SELECT d.*,count(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON c.document_id=d.id WHERE course_id=? GROUP BY d.id ORDER BY d.created_at DESC',(course_id,))
        for row in rows:
            incompatible=store.one('SELECT count(*) AS n FROM chunks WHERE document_id=? AND (embedding_signature IS NULL OR embedding_signature!=?)',(row['id'],pipeline.embedding_signature()))['n']
            row['index_current']=not incompatible and row['status']=='ready'
            row['chunking']=store.chunking_configuration(row['id'])
            row['chunking_current']=row['chunking']==pipeline.chunking_configuration()
            row['parsing']=document_storage.summary(document_storage.report_for(store,row['id']))
            row['parser_options']=document_jobs.options_for(store,row['id'])
            row['figures']=document_jobs.public_figures(store,row['id'])
            row['active_jobs']=store.all("SELECT id,kind,status FROM jobs WHERE kind IN ('parse','figures') AND json_extract(payload,'$.document_id')=? AND status IN ('queued','running')",(row['id'],))
        return rows
    @app.post('/api/documents',status_code=201)
    async def upload(request:Request,course_id:str=Form(...),file:UploadFile=File(...),tier:str|None=Form(None),images:bool=Form(False)):
        need_course(course_id,request)
        raw=await file.read(settings.max_upload_bytes+1)
        if len(raw)>settings.max_upload_bytes:raise HTTPException(413,'单个文件最多 10 MB')
        name=Path(file.filename or 'document.txt').name[:180]
        digest=hashlib.sha256(raw).hexdigest()
        previous=store.one('SELECT * FROM documents WHERE course_id=? AND sha256=?',(course_id,digest))
        if previous:
            source=settings.data_dir/'documents'/(previous['id']+Path(previous['name']).suffix.lower())
            restored=not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=digest
            if restored:await asyncio.to_thread(save_source,source,raw)
            return previous|{'duplicate':True,'source_restored':restored}
        if tier is not None and tier not in ('standard','advanced'):raise HTTPException(422,'解析档位无效。')
        if Path(name).suffix.lower() not in ('.txt','.md') and (settings.document_backend=='mineru' or tier is not None):
            try:pages=document_backends.validate_source(settings,name,raw)
            except ValueError as exc:raise HTTPException(400,str(exc)) from None
            if images:require('vision','embedding')
            did=uid();source=settings.data_dir/'documents'/(did+Path(name).suffix.lower())
            row={'id':did,'course_id':course_id,'name':name,'sha256':digest,'status':'pending','pages':pages,'created_at':now()}
            options={'tier':tier or 'standard','images':images}
            await asyncio.to_thread(save_source,source,raw)
            try:
                with store.connect() as db:
                    db.execute('INSERT INTO documents VALUES(:id,:course_id,:name,:sha256,:status,:pages,:created_at)',row)
                    db.execute('INSERT INTO document_options VALUES(?,?)',(did,dumps(options)))
                job=document_jobs.enqueue(pipeline,'parse',did,account_id(request),**options)
            except sqlite3.IntegrityError:
                source.unlink(missing_ok=True)
                raise HTTPException(409,'该资料已被同时导入，请刷新列表') from None
            return row|job|{'status':'pending','parser_options':options}
        try:configuration=pipeline.chunking_configuration()
        except ValueError as exc:raise HTTPException(503,'切分配置无效：'+str(exc)) from None
        try:
            parsed=await asyncio.to_thread(document_storage.parse_upload,settings,name,raw)
            chunks=document_storage.parsed_chunks(parsed.report(),configuration,digest,settings.max_chunks)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        did=uid();row={'id':did,'course_id':course_id,'name':name,'sha256':digest,'status':'parsed','pages':len(parsed.pages),'created_at':now()}
        chunks=document_chunk_ids(chunks,did)
        folder=settings.data_dir/'documents';folder.mkdir(exist_ok=True)
        source=folder/(did+Path(name).suffix.lower())
        await asyncio.to_thread(save_source,source,raw)
        asset_directory=None
        try:
            report,asset_directory=await asyncio.to_thread(document_storage.store_assets,settings,did,parsed)
            report['source_document_sha256']=digest
            with store.connect() as db:
                db.execute('INSERT INTO documents VALUES(:id,:course_id,:name,:sha256,:status,:pages,:created_at)',row)
                store.save_chunks(db,did,chunks,configuration)
                document_storage.save_report(db,did,report)
        except sqlite3.IntegrityError:
            source.unlink(missing_ok=True)
            if asset_directory:shutil.rmtree(asset_directory,ignore_errors=True)
            raise HTTPException(409,'该资料已被同时导入，请刷新列表') from None
        except Exception:
            source.unlink(missing_ok=True)
            if asset_directory:shutil.rmtree(asset_directory,ignore_errors=True)
            raise
        return row|{'chunks':len(chunks),'chunking':configuration,'parsing':document_storage.summary(report)}
    @app.get('/api/documents/{did}/parsing')
    async def document_parsing(did:str,request:Request):
        need_document(did,request)
        report=document_storage.report_for(store,did)
        if not report:return {'document_id':did,'available':False,'status':'legacy_unverified','options':document_jobs.options_for(store,did)}
        return document_storage.public_report(report,did)|{'available':True,'figures':document_jobs.public_figures(store,did),'options':document_jobs.options_for(store,did)}
    @app.get('/api/documents/{did}/source')
    async def document_source(did:str,request:Request):
        doc=need_document(did,request)
        source=settings.data_dir/'documents'/(did+Path(doc['name']).suffix.lower())
        if not source.is_file():raise HTTPException(404,'原始资料不存在')
        return FileResponse(source,filename=doc['name'])
    @app.get('/api/documents/{did}/assets/{aid}')
    async def document_asset(did:str,aid:str,request:Request):
        need_document(did,request)
        asset=document_storage.asset_for(store,did,aid)
        if not asset:raise HTTPException(404,'页面图片不存在')
        base=(settings.data_dir/'document_assets'/did).resolve()
        path=(base/asset['storage_name']).resolve()
        if not path.is_relative_to(base) or not path.is_file():raise HTTPException(404,'页面图片不存在')
        return FileResponse(path,media_type=asset['mime_type'])
    @app.post('/api/documents/{did}/parse')
    async def reparse_document(did:str,request:Request,options:ParseOptions|None=None):
        doc=need_document(did,request)
        if Path(doc['name']).suffix.lower() not in ('.txt','.md') and (settings.document_backend=='mineru' or options is not None):
            chosen=options.model_dump() if options else document_jobs.options_for(store,did)
            if chosen['images']:require('vision','embedding')
            active=store.one("SELECT id FROM jobs WHERE kind IN ('parse','figures') AND json_extract(payload,'$.document_id')=? AND status IN ('queued','running')",(did,))
            if active:raise HTTPException(409,'资料正在处理，请等待任务完成。')
            return document_jobs.enqueue(pipeline,'parse',did,account_id(request),**chosen)
        async with pipeline.index_locks.setdefault(did,asyncio.Lock()):
            source=settings.data_dir/'documents'/(did+Path(doc['name']).suffix.lower())
            if not source.is_file():raise HTTPException(400,'原始资料不存在，请重新导入。')
            raw=await asyncio.to_thread(source.read_bytes)
            if hashlib.sha256(raw).hexdigest()!=doc['sha256']:raise HTTPException(400,'原始资料校验失败，请重新导入。')
            try:
                configuration=pipeline.chunking_configuration()
                parsed=await asyncio.to_thread(document_storage.parse_upload,settings,doc['name'],raw)
                chunks=document_chunk_ids(document_storage.parsed_chunks(parsed.report(),configuration,doc['sha256'],settings.max_chunks),did)
            except ValueError as exc:raise HTTPException(400,str(exc)) from None
            report,directory=await asyncio.to_thread(document_storage.store_assets,settings,did,parsed)
            report['source_document_sha256']=doc['sha256']
            try:
                with store.connect() as db:
                    db.execute('DELETE FROM chunks WHERE document_id=?',(did,))
                    store.save_chunks(db,did,chunks,configuration)
                    document_storage.save_report(db,did,report)
                    db.execute("UPDATE documents SET status='parsed',pages=? WHERE id=?",(len(parsed.pages),did))
            except Exception:
                shutil.rmtree(directory,ignore_errors=True)
                raise
        return {'document_id':did,'chunks':len(chunks),'parsing':document_storage.summary(report),'index_rebuild_required':True}
    @app.get('/api/documents/{did}/figures')
    async def figures(did:str,request:Request):
        need_document(did,request)
        return document_jobs.public_figures(store,did)
    @app.post('/api/documents/{did}/figures/retry',status_code=202)
    async def retry_figures(did:str,request:Request,asset_id:str|None=None):
        need_document(did,request);require('vision','embedding')
        if asset_id and not any(a['id']==asset_id for a in document_jobs.active_figures(store,did)):
            raise HTTPException(404,'图片不存在。')
        active=store.one("SELECT id FROM jobs WHERE kind IN ('parse','figures') AND json_extract(payload,'$.document_id')=? AND status IN ('queued','running')",(did,))
        if active:raise HTTPException(409,'资料正在处理，请等待任务完成。')
        return document_jobs.enqueue(pipeline,'figures',did,account_id(request),asset_id=asset_id)
    @app.patch('/api/documents/{did}/figures/{aid}')
    async def edit_figure(did:str,aid:str,payload:FigureEdit,request:Request):
        need_document(did,request)
        async with pipeline.index_locks.setdefault(did,asyncio.Lock()):
            if not any(a['id']==aid for a in document_jobs.active_figures(store,did)):raise HTTPException(404,'图片不存在。')
            data=dict(keywords_zh=[],keywords_en=[],visible_text='',relationships=[],uncertainties=[])
            data['description']=payload.description
            annotation={'data':data,'origin':'user','verified':False,'prompt_version':'manual-v1'}
            store.execute("""INSERT INTO figure_semantics(document_id,asset_id,status,cache_key,description,updated_at) VALUES(?,?,'pending','manual',?,?)
                ON CONFLICT(document_id,asset_id) DO UPDATE SET status='pending',cache_key='manual',description=excluded.description,vector=NULL,embedding_signature=NULL,error=NULL,updated_at=excluded.updated_at""",
                (did,aid,dumps(annotation),now()))
        return {'asset_id':aid,'status':'pending','index_rebuild_required':True}
    @app.get('/api/documents/{did}/chunks')
    async def chunks(did:str,request:Request):
        need_document(did,request)
        return [{k:row[k] for k in ('id','page','text','metadata')} for row in store.document_chunks(did)]
    @app.post('/api/documents/{did}/index',status_code=202)
    async def index_document(did:str,request:Request):
        need_document(did,request)
        require('embedding')
        return submit(uid(),'index',{'document_id':did},request)
    @app.post('/api/generations',status_code=202)
    async def generation(payload:GenerateRequest,request:Request):
        need_course(payload.course_id,request)
        if payload.document_ids:
            for did in payload.document_ids:need_document(did,request)
            own={r['id'] for r in store.all('SELECT id FROM documents WHERE course_id=?',(payload.course_id,))}
            if not set(payload.document_ids)<=own:raise HTTPException(400,'所选资料不属于该课程')
        require('text','embedding')
        return submit(payload.request_key,'generate',payload.model_dump(),request)
    # Expose only navigation metadata; job payloads can include private prompts.
    job_context_sql='''
        SELECT j.id,j.kind,j.status,j.result,j.error,j.created_at,j.updated_at,o.user_id AS owner_id,
            CASE j.kind
                WHEN 'generate' THEN json_extract(j.payload,'$.course_id')
                WHEN 'index' THEN d.course_id
                WHEN 'parse' THEN d.course_id
                WHEN 'figures' THEN d.course_id
                WHEN 'media' THEN c.course_id
            END AS course_id,
            COALESCE(
                CASE WHEN j.status='succeeded' THEN json_extract(j.result,'$.content_id') END,
                CASE WHEN j.kind='media' THEN json_extract(j.payload,'$.content_id') END
            ) AS content_id
        FROM jobs j
        JOIN job_owners o ON o.job_id=j.id
        LEFT JOIN documents d ON j.kind IN ('index','parse','figures') AND d.id=json_extract(j.payload,'$.document_id')
        LEFT JOIN contents c ON j.kind='media' AND c.id=json_extract(j.payload,'$.content_id')
    '''
    @app.get('/api/jobs')
    async def jobs(request:Request,course_id:str|None=None):
        if course_id is not None:need_course(course_id,request)
        where=' WHERE owner_id=?'+(' AND course_id=?' if course_id is not None else '')
        return store.all('SELECT id,kind,status,error,created_at,course_id,content_id FROM ('+
            job_context_sql+')'+where+' ORDER BY created_at DESC LIMIT 30',
            (account_id(request),course_id) if course_id is not None else (account_id(request),))
    @app.get('/api/jobs/{jid}')
    async def job(jid:str,request:Request):
        row=store.one('SELECT * FROM ('+job_context_sql+') WHERE id=? AND owner_id=?',(jid,account_id(request)))
        if not row:raise HTTPException(404,'任务不存在')
        row.pop('owner_id',None)
        row['result']=json.loads(row['result']) if row['result'] else None;return row
    @app.get('/api/jobs/{jid}/evidence')
    async def job_evidence(jid:str,request:Request):
        row=await job(jid,request)
        evidence=store.one('SELECT evidence,updated_at FROM job_evidence WHERE job_id=?',(jid,))
        return {'schema_version':'ca1-job-evidence-v1','exported_at':now(),'job':row,
                'evidence':json.loads(evidence['evidence']) if evidence else None,
                'calls':store.calls_for_job(jid),
                'limitations':['Retrieved passages and citation identifiers do not establish factual correctness.',
                               'Call success describes the provider response contract; inspect job and attempt validation separately.']}
    @app.get('/api/contents')
    async def contents(request:Request,course_id:str|None=None):
        if course_id is not None:need_course(course_id,request)
        where=' WHERE o.user_id=?'+(' AND c.course_id=?' if course_id is not None else '')
        return store.all('''SELECT c.id,c.version,c.status,c.created_at,c.course_id,
            courses.name AS course_name,json_extract(c.asset,'$.title') AS title,
            COALESCE(json_extract(c.config,'$.material'),'quiz') AS material
            FROM contents c JOIN courses ON courses.id=c.course_id JOIN course_owners o ON o.course_id=c.course_id'''+where+
            ' ORDER BY c.created_at DESC',(account_id(request),course_id) if course_id is not None else (account_id(request),))
    @app.get('/api/contents/{cid}')
    async def content(cid:str,request:Request):
        row=need_content(cid,request)
        for key in ('asset','sources','config'):row[key]=json.loads(row[key])
        row['media']=store.all('SELECT id,version,kind,mime,status,metadata FROM media WHERE content_id=? AND version=?',(cid,row['version']))
        for item in row['media']:item['metadata']=json.loads(item['metadata'])
        row['difficulty_assessment']=difficulty_assessment_for(row)
        return row
    @app.post('/api/contents/{cid}/review')
    async def review(cid:str,payload:ReviewRequest,request:Request):
        row=need_content(cid,request)
        if payload.version!=row['version']:raise HTTPException(409,'内容已更新，请刷新后重试')
        if payload.action=='save' and payload.asset is None:raise HTTPException(400,'保存时必须提供材料')
        if payload.action=='approve' and payload.asset is not None:raise HTTPException(400,'审核不能同时修改内容，请先保存新版本')
        asset=payload.asset or LearningAsset.model_validate_json(row['asset'])
        config=json.loads(row['config'])
        try:validate_asset(asset,GenerateRequest(**({'request_key':'review-validation'}|{k:v for k,v in config.items() if k in GenerateRequest.model_fields})),json.loads(row['sources']),enforce_difficulty='difficulty_plan' in config)
        except ValueError as exc:raise HTTPException(400,str(exc)) from None
        if not asset.evidence_sufficient:raise HTTPException(400,'证据不足的材料不能保存或审核')
        version=row['version']+(payload.action=='save')
        with store.connect() as db:
            changed=db.execute('UPDATE contents SET asset=?,version=?,status=? WHERE id=? AND version=?',
                (dumps(asset.model_dump()),version,'approved' if payload.action=='approve' else 'draft',cid,row['version'])).rowcount
            if not changed:raise HTTPException(409,'内容发生并发更新，请刷新')
            if payload.action=='save':db.execute('INSERT INTO revisions VALUES(?,?,?,?)',(cid,version,dumps(asset.model_dump()),now()))
        return {'id':cid,'version':version,'status':'approved' if payload.action=='approve' else 'draft'}
    @app.post('/api/contents/{cid}/media',status_code=202)
    async def make_media(cid:str,payload:MediaRequest,request:Request):
        row=need_content(cid,request)
        if row['version']!=payload.version or row['status']!='approved':raise HTTPException(409,'请先审核当前文本版本')
        require('speech' if payload.kind=='audio' else 'image')
        return submit(payload.request_key,'media',payload.model_dump()|{'content_id':cid},request)
    @app.get('/api/media/{mid}/file')
    async def media_file(mid:str,request:Request):
        row=store.one('SELECT * FROM media WHERE id=?',(mid,))
        if not row:raise HTTPException(404,'媒体不存在')
        need_content(row['content_id'],request)
        return FileResponse(settings.data_dir/'media'/row['path'],media_type=row['mime'])
    @app.post('/api/media/{mid}/approve')
    async def approve_media(mid:str,payload:MediaReview,request:Request):
        row=store.one('SELECT * FROM media WHERE id=?',(mid,))
        if not row:raise HTTPException(404,'媒体不存在')
        content=need_content(row['content_id'],request)
        if content['version']!=payload.version or row['version']!=payload.version or content['status']!='approved':raise HTTPException(409,'媒体与审核文本版本不一致')
        store.execute("UPDATE media SET status='approved' WHERE id=?",(mid,));return {'status':'approved'}
    @app.get('/api/learn/{cid}')
    async def learn(cid:str,request:Request,reveal_answers:bool=False):
        row=need_content(cid,request)
        if row['status']!='approved':raise HTTPException(404,'材料尚未发布')
        asset=json.loads(row['asset'])
        for q in asset['questions']:
            for key in ('slot_id','difficulty','difficulty_design'):q.pop(key,None)
        if not reveal_answers:
            for q in asset['questions']:
                q.pop('answer',None);q.pop('explanation',None);q.pop('difficulty_reason',None)
        asset.pop('visual_prompt',None)
        media=store.all("SELECT id,kind FROM media WHERE content_id=? AND version=? AND status='approved'",(cid,row['version']))
        return {'id':cid,'version':row['version'],'asset':asset,'media':media}
    @app.post('/api/contents/{cid}/evaluations',status_code=201)
    async def evaluation(cid:str,payload:EvaluationRequest,request:Request):
        row=need_content(cid,request)
        if row['version']!=payload.version:raise HTTPException(409,'请评价当前版本')
        metrics=payload.model_dump()
        if payload.question_difficulties:
            asset=json.loads(row['asset']);config=json.loads(row['config'])
            questions={q.get('slot_id') or f'q{i}':q for i,q in enumerate(asset['questions'],1)}
            ratings={r.slot_id:r.assessed_difficulty for r in payload.question_difficulties}
            if len(ratings)!=len(payload.question_difficulties) or set(ratings)!=set(questions):
                raise HTTPException(400,'逐题难度评价须覆盖当前版本的全部题目且不能重复；不能判断时请选择“无法判断”。')
            plan={slot['slot_id']:slot['difficulty'] for slot in config.get('difficulty_plan',[])}
            checked=difficulty_assessment_for(row)
            model_levels={item['slot_id']:item['assessed_difficulty'] for item in checked['items']} if checked['status']=='model_checked' else {}
            details=[]
            for slot,level in ratings.items():
                target=plan.get(slot)
                details.append({'slot_id':slot,'assessed_difficulty':level,'target_difficulty':target,
                    'generated_difficulty':questions[slot].get('difficulty'),
                    'model_assessed_difficulty':model_levels.get(slot),
                    'matches_target':level==target if target and level!='uncertain' else None})
            metrics['question_difficulties']=details
            compared=[r for r in details if r['matches_target'] is not None]
            metrics['difficulty_agreement']={'rated':len(details),'compared':len(compared),
                'matched':sum(r['matches_target'] for r in compared),
                'uncertain':sum(r['assessed_difficulty']=='uncertain' for r in details),
                'rate':sum(r['matches_target'] for r in compared)/len(compared) if compared else None,
                'interpretation':'教师判断与目标难度的一致率，不是学生答对率。'}
        eid=uid();store.execute('INSERT INTO evaluations VALUES(?,?,?,?,?)',(eid,cid,payload.version,dumps(metrics),now()))
        return {'id':eid}
    @app.get('/api/difficulty/evaluations/export')
    async def export_difficulty_evaluations(request:Request):
        out=io.StringIO();writer=csv.writer(out)
        writer.writerow(['evaluation_id','content_id','version','slot_id','target_difficulty','generated_difficulty',
                         'model_assessed_difficulty','teacher_assessed_difficulty','matches_target','created_at'])
        for row in store.all('''SELECT e.* FROM evaluations e JOIN contents c ON c.id=e.content_id
            JOIN course_owners o ON o.course_id=c.course_id WHERE o.user_id=? ORDER BY e.created_at''',(account_id(request),)):
            for rating in json.loads(row['metrics']).get('question_difficulties',[]):
                writer.writerow([row['id'],row['content_id'],row['version'],rating['slot_id'],rating.get('target_difficulty'),
                    rating.get('generated_difficulty'),rating.get('model_assessed_difficulty'),rating['assessed_difficulty'],
                    rating.get('matches_target'),row['created_at']])
        return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="difficulty-evaluations.csv"'})
    @app.get('/api/evaluations/export')
    async def export_evaluations(request:Request):
        rows=store.all('''SELECT e.* FROM evaluations e JOIN contents c ON c.id=e.content_id
            JOIN course_owners o ON o.course_id=c.course_id WHERE o.user_id=? ORDER BY e.created_at''',(account_id(request),));out=io.StringIO()
        writer=csv.writer(out);writer.writerow(['content_id','version','correctness','groundedness','difficulty_match','notes','created_at'])
        for row in rows:
            m=json.loads(row['metrics']);note=m['notes']
            if note.startswith(('=','+','-','@')):note="'"+note
            writer.writerow([row['content_id'],row['version'],m['correctness'],m['groundedness'],m['difficulty_match'],note,row['created_at']])
        return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="evaluations.csv"'})
    @app.get('/api/contents/{cid}/evidence')
    async def export_evidence(cid:str,request:Request):
        row=need_content(cid,request)
        config=json.loads(row['config'])
        sources=json.loads(row['sources'])
        config.pop('request_key',None)
        calls=store.calls_for_job(row['job_id'])
        bundle={'schema_version':'ca1-evidence-v1','exported_at':now(),
            'content_id':cid,'version':row['version'],'status':row['status'],
            'asset':json.loads(row['asset']),'sources':sources,'configuration':config,
            'difficulty_assessment':difficulty_assessment_for(row),
            'generation_calls':calls,
            'revisions':[dict(version=r['version'],asset=json.loads(r['asset']),created_at=r['created_at']) for r in store.all('SELECT * FROM revisions WHERE content_id=? ORDER BY version',(cid,))],
            'limitations':['Citation identifiers establish provenance, not factual correctness.',
                'Call records describe initial generation; content may have been edited afterwards.']}
        return Response(dumps(bundle),media_type='application/json',headers={'Content-Disposition':'attachment; filename="ca1-evidence.json"'})
    @app.get('/api/contents/{cid}/export')
    async def export_content(cid:str,request:Request):
        row=need_content(cid,request);asset=json.loads(row['asset'])
        text='# '+asset['title']+'\n\n状态：'+row['status']+'\n\n'
        for s in asset['sections']:text+='## '+s['heading']+'\n\n'+s['text']+'\n\n引用片段：'+', '.join(s['citation_ids'])+'\n\n'
        for i,q in enumerate(asset['questions'],1):
            text+=f"## {i}. {q['stem']}\n\n"
            if q.get('difficulty'):
                text+='目标难度：'+{'easy':'简单','medium':'中等','hard':'困难'}[q['difficulty']]+'\n\n'
            text+='\n'.join(chr(65+j)+'. '+s for j,s in enumerate(q['options']))+'\n\n'
            text+='参考答案：'+q['answer']+'\n\n解析：'+q['explanation']+'\n\n引用片段：'+', '.join(q['citation_ids'])+'\n\n'
        text+='## 资料依据\n\n'
        for source in json.loads(row['sources']):text+=f"- {source['document_name']}，第 {source['page']} 页（片段 {source['id']}）\n"
        return Response(text,media_type='text/markdown',headers={'Content-Disposition':'attachment; filename="learning-material.md"'})
    return app
