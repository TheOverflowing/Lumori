"""Transactional parsing and optional image enrichment, always document-scoped."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from . import document_backends, document_storage
from .document_parsing import ParsedDocument
from .figure_schema import validate_description
from .rag_runtime import document_input
from .retrieval import normalize_vectors
from .store import Conflict,dumps,now,uid
from .document_lifecycle import ACTIVE_SQL
from .task_control import JobCancelled

PROMPT_VERSION='figure-semantics-v2'


def options_for(store,did):
    row=store.one('SELECT options FROM document_options WHERE document_id=?',(did,))
    return json.loads(row['options']) if row else {'tier':'standard','images':False}


def enqueue(pipeline,kind,did,owner_id,**extra):
    job,new=pipeline.store.job(uid(),kind,dict(document_id=did,**extra),owner_id=owner_id)
    if new:pipeline.enqueue(job['id'])
    return {'job_id':job['id'],'status':job['status']}


def source_bytes(pipeline,did):
    doc=pipeline.store.one('SELECT * FROM documents WHERE id=?',(did,))
    if not doc:raise ValueError('资料不存在。')
    source=pipeline.settings.data_dir/'documents'/(did+Path(doc['name']).suffix.lower())
    if not source.is_file():raise ValueError('原始资料不存在，请重新导入。')
    raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=doc['sha256']:raise ValueError('原始资料校验失败，请重新导入。')
    return doc,raw


async def parse_document(pipeline,payload,jobid):
    from .pipeline import document_chunk_ids
    from .exploration_policy import document_source
    did=payload['document_id'];settings=pipeline.settings;store=pipeline.store
    store.check_cancelled(jobid)
    async with pipeline.index_locks.setdefault(did,asyncio.Lock()):
        doc,raw=await asyncio.to_thread(source_bytes,pipeline,did)
        options={'tier':payload['tier'],'images':bool(payload['images'])}
        async with pipeline.document_worker_slot:
            store.check_cancelled(jobid)
            parsed=await document_backends.parse(settings,doc['name'],raw,options['tier'])
        store.check_cancelled(jobid)
        configuration=pipeline.chunking_configuration()
        report,directory=document_storage.store_assets(settings,did,parsed,
            external_source=document_source(store,did))
        report.update(source_document_sha256=doc['sha256'],options=options,figure_extraction='pending' if options['images'] else 'disabled')
        try:
            chunks=document_chunk_ids(document_storage.parsed_chunks(report,configuration,doc['sha256'],settings.max_chunks),did)
            with store.connect() as db:
                db.execute('DELETE FROM chunks WHERE document_id=?',(did,))
                store.save_chunks(db,did,chunks,configuration)
                document_storage.save_report(db,did,report)
                db.execute('DELETE FROM figure_semantics WHERE document_id=?',(did,))
                db.execute('INSERT OR REPLACE INTO document_options VALUES(?,?)',(did,dumps(options)))
                db.execute("UPDATE documents SET status='parsed',pages=? WHERE id=?",(len(parsed.pages),did))
        except Exception:
            shutil.rmtree(directory,ignore_errors=True);raise
    result={'document_id':did,'chunks':len(chunks),'index_rebuild_required':True}
    if options['images']:
        owner=store.one('SELECT user_id FROM job_owners WHERE job_id=?',(jobid,))
        try:
            result['figures_job']=enqueue(pipeline,'figures',did,owner['user_id'] if owner else None)
        except Conflict:
            # Parsing is already committed. A full account queue must not turn
            # usable text into a failed parse; image processing can start later.
            result.update(figures_deferred=True,message='文字解析已完成；任务队列已满，可稍后继续处理图片。')
    return result


def bind_captions(assets,report):
    """Match overlapping MinerU image blocks; do not invent missing captions."""
    for asset in assets:
        if asset.provenance.get('original_caption'):continue
        box=asset.provenance.get('bbox')
        if not box:continue
        page=next((p for p in report['pages'] if p['number']==asset.page),{})
        candidates=[]
        for block in page.get('blocks',[]):
            other=block.get('bbox')
            if block.get('type')!='image' or not other or not block.get('text','').strip():continue
            intersection=max(0,min(box[2],other[2])-max(box[0],other[0]))*max(0,min(box[3],other[3])-max(box[1],other[1]))
            union=(box[2]-box[0])*(box[3]-box[1])+(other[2]-other[0])*(other[3]-other[1])-intersection
            iou=intersection/union if union>0 else 0
            if iou>=.5:candidates.append((iou,block['text']))
        if candidates:
            iou,text=max(candidates,key=lambda pair:pair[0])
            asset.provenance.update(original_caption=text[:4000],caption_source='mineru_image_block_overlap',caption_match_iou=round(iou,4))


def active_figures(store,did):
    report=document_storage.report_for(store,did)
    return [a for a in report.get('assets',[]) if a['kind']=='figure'] if report else []


def public_figures(store,did):
    states={r['asset_id']:r for r in store.all('SELECT * FROM figure_semantics WHERE document_id=?',(did,))}
    result=[]
    for asset in active_figures(store,did):
        row=states.get(asset['id'],{})
        result.append(dict(asset_id=asset['id'],page=asset['page'],bbox=asset.get('bbox'),
            status=row.get('status','pending'),error=row.get('error'),
            annotation=json.loads(row['description']) if row.get('description') else None,
            indexed=bool(row.get('vector')),url=f"/api/documents/{did}/assets/{asset['id']}"))
    return result


async def enrich(pipeline,payload,jobid):
    did=payload['document_id'];settings=pipeline.settings;store=pipeline.store
    store.check_cancelled(jobid)
    settings.vision.require('vision');settings.embedding.require('embedding')
    async with pipeline.index_locks.setdefault(did,asyncio.Lock()):
        doc,raw=await asyncio.to_thread(source_bytes,pipeline,did)
        report=document_storage.report_for(store,did)
        if not report or not report.get('options',{}).get('images'):
            raise ValueError('该资料尚未启用图片理解，请重新解析并选择图片处理。')
        if report.get('figure_extraction')!='complete':
            async with pipeline.document_worker_slot:
                store.check_cancelled(jobid)
                assets,truncated=await document_backends.extract_figures(settings,doc['name'],raw)
            store.check_cancelled(jobid)
            # Append crops to the original MinerU text report without changing OCR.
            bind_captions(assets,report)
            wrapper=ParsedDocument(doc['name'],'docling_figures',[],assets)
            saved,directory=document_storage.store_assets(settings,did,wrapper)
            report['assets']=saved['assets']
            for page in report['pages']:
                page['asset_ids']=[a['id'] for a in report['assets'] if a['page']==page['number']]
            report.update(figure_extraction='complete',figure_limit_reached=truncated)
            try:
                with store.connect() as db:document_storage.save_report(db,did,report)
            except Exception:
                shutil.rmtree(directory,ignore_errors=True);raise
        assets=active_figures(store,did)
        target=payload.get('asset_id')
        if target:
            assets=[a for a in assets if a['id']==target]
            if not assets:raise ValueError('图片已不属于当前解析结果。')
        succeeded=failed=reused=0
        for asset in assets:
            store.check_cancelled(jobid)
            nearby=next((p['text'] for p in report['pages'] if p['number']==asset['page']),'')[:6000]
            context={'original_caption':asset.get('original_caption',''),'page_context':nearby}
            cache_key=hashlib.sha256(dumps([asset['sha256'],context,settings.vision.signature,PROMPT_VERSION]).encode()).hexdigest()
            row=store.one('SELECT * FROM figure_semantics WHERE document_id=? AND asset_id=?',(did,asset['id']))
            signature=pipeline.embedding_signature()
            # Teacher edits take precedence; reparsing explicitly clears them.
            annotation=json.loads(row['description']) if row and row['description'] and (row['cache_key']==cache_key or row['cache_key']=='manual') else None
            if annotation and row['status']=='ready' and row['embedding_signature']==signature and row['vector']:
                reused+=1;continue
            try:
                with store.connect() as db:
                    db.execute('''INSERT INTO figure_semantics(document_id,asset_id,status,updated_at) VALUES(?,?,?,?)
                        ON CONFLICT(document_id,asset_id) DO UPDATE SET status=excluded.status,error=NULL,vector=NULL,embedding_signature=NULL,updated_at=excluded.updated_at''',
                        (did,asset['id'],'running',now()))
                if annotation is None:
                    base=(settings.data_dir/'document_assets'/did).resolve()
                    path=(base/asset['storage_name']).resolve()
                    if not path.is_relative_to(base) or not path.is_file():raise ValueError('图片文件不存在。')
                    image=path.read_bytes()
                    if hashlib.sha256(image).hexdigest()!=asset['sha256']:raise ValueError('图片校验失败。')
                    description=await pipeline.providers.describe_image(image,asset['mime_type'],context,jobid)
                    annotation={'data':description,'model':settings.vision.model,'endpoint_signature':settings.vision.signature,
                                'prompt_version':PROMPT_VERSION,'origin':'model','verified':False}
                    store.execute('UPDATE figure_semantics SET description=?,cache_key=?,updated_at=? WHERE document_id=? AND asset_id=?',
                        (dumps(annotation),cache_key,now(),did,asset['id']))
                description=validate_description(annotation['data'])
                text=description.retrieval_text()
                vectors=await pipeline.providers.embed([document_input({'text':text,'metadata':{}},doc['name'],settings.retrieval_document_encoding)],jobid)
                if len(vectors)!=1 or normalize_vectors(vectors).shape[0]!=1:raise ValueError('图片向量无效。')
                if pipeline.embedding_signature()!=signature:raise ValueError('嵌入配置已变化，请重试。')
                store.execute("UPDATE figure_semantics SET status='ready',vector=?,embedding_signature=?,error=NULL,updated_at=? WHERE document_id=? AND asset_id=?",
                    (dumps(vectors[0]),signature,now(),did,asset['id']))
                succeeded+=1
            except JobCancelled:
                store.execute("UPDATE figure_semantics SET status='pending',error=NULL,updated_at=? WHERE document_id=? AND asset_id=?",(now(),did,asset['id']))
                raise
            except asyncio.CancelledError:
                store.execute("UPDATE figure_semantics SET status='failed',error=?,updated_at=? WHERE document_id=? AND asset_id=?",('图片处理已中断，请重试。',now(),did,asset['id']))
                raise
            except Exception:
                # Never store arbitrary provider output or credentials in error messages.
                store.execute("UPDATE figure_semantics SET status='failed',error=?,updated_at=? WHERE document_id=? AND asset_id=?",('图片理解或嵌入失败，请检查模型连接后重试。',now(),did,asset['id']))
                failed+=1
        return dict(document_id=did,succeeded=succeeded,failed=failed,reused=reused,total=len(assets),
            figure_limit_reached=report.get('figure_limit_reached',False),partial_failure=bool(failed),
            message='部分图片处理失败，可在文字与原图页面重试。' if failed else '图片处理完成。')


def retrieval_rows(pipeline,course_id,document_ids=None):
    rows=pipeline.store.all('''SELECT f.*,d.name AS document_name FROM figure_semantics f JOIN documents d ON d.id=f.document_id
        WHERE d.course_id=? AND d.status='ready' AND f.status='ready' AND f.vector IS NOT NULL AND f.embedding_signature=? AND '''+ACTIVE_SQL,
        (course_id,pipeline.embedding_signature()))
    active={}
    result=[]
    for row in rows:
        did=row['document_id']
        if document_ids and did not in document_ids:continue
        if did not in active:active[did]={a['id']:a for a in active_figures(pipeline.store,did)}
        asset=active[did].get(row['asset_id'])
        if not asset:continue
        annotation=json.loads(row['description'])
        text=validate_description(annotation['data']).retrieval_text()
        result.append(dict(id='figure_'+hashlib.sha256((did+row['asset_id']+dumps(annotation)).encode()).hexdigest()[:32],
            document_id=did,document_name=row['document_name'],page=asset['page'],text=text,
            vector=row['vector'],embedding_signature=row['embedding_signature'],
            metadata={'source_kind':'figure','source_asset_ids':[asset['id']],'bbox':asset.get('bbox'),
                'extraction_method':'model_image_description','extraction_status':'needs_review',
                'extraction_warnings':['model_description_not_original_text'],
                'original_caption':asset.get('original_caption',''),'annotation':annotation,
                'heading_path':[],'coordinate_basis':'image_description'}))
    return result
