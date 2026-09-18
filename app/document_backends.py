"""Bounded subprocess adapters, separate from immutable parsing experiments."""
import asyncio
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from pypdf import PdfReader
from .config import ROOT
from .document_parsing import ParsedDocument, ParsedPage, VisualAsset


class _HTML(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)


def content_text(value):
    if isinstance(value,str):return value
    if isinstance(value,list):return ''.join(content_text(v) for v in value)
    if isinstance(value,dict):
        text=content_text(value.get('content',''))
        if value.get('type')=='table_body':
            parser=_HTML();parser.feed(text);return ' '.join(parser.parts)
        # Generated image semantics must not masquerade as original OCR.
        if value.get('type') in ('image_description','image_body'):return ''
        return text
    return ''


def validate_source(settings,name,raw):
    suffix=Path(name).suffix.lower()
    if suffix not in ('.pdf','.png','.jpg','.jpeg'):raise ValueError('不支持该文件格式。')
    if not raw or len(raw)>settings.max_upload_bytes:raise ValueError('文件为空或超过大小限制。')
    if suffix=='.pdf':
        try:
            reader=PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:raise ValueError('暂不支持加密 PDF。')
            count=len(reader.pages)
            if not 0<count<=settings.max_pages:raise ValueError('PDF 页数超过限制或为空。')
            return count
        except ValueError:raise
        except Exception:raise ValueError('PDF 无法解析，请检查文件是否完整。') from None
    if not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8')):
        raise ValueError('图片格式无效。')
    return 1


def capabilities(settings):
    return dict(backend=settings.document_backend,tiers=['standard','advanced'],
        mineru_available=settings.mineru_python.is_file() and (settings.mineru_runtime/'config.yaml').is_file(),
        docling_available=settings.docling_python.is_file() and settings.docling_models.is_dir(),
        max_figures=settings.document_max_figures)


async def worker(settings,mode,source,output,*,tier='standard'):
    python=settings.mineru_python if mode=='mineru' else settings.docling_python
    if not python.is_file():raise ValueError('本机缺少所需解析组件，请检查配置。')
    allowed=('PATH','TMPDIR','LANG','LC_ALL','SYSTEMROOT')
    env={k:os.environ[k] for k in allowed if k in os.environ}
    runtime=settings.mineru_runtime;models=settings.docling_models
    env.update(MINERU_HOME=str(runtime),MINERU_CONFIG=str(runtime/'config.yaml'),
        MINERU_MODEL_SOURCE='local',MINERU_MODEL_SMALL_BACKEND='onnx',MINERU_MODEL_VLM_ENGINE='llama-cpp',
        MINERU_INTRA_OP_NUM_THREADS='4',MINERU_INTER_OP_NUM_THREADS='1',
        HF_HOME=str(runtime/'hf-cache' if mode=='mineru' else models/'hf-cache'),
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
        HF_HUB_DISABLE_IMPLICIT_TOKEN='1',DO_NOT_TRACK='1',OMP_NUM_THREADS='4',
        OPENBLAS_NUM_THREADS='4',TOKENIZERS_PARALLELISM='false',
        DOCLING_CACHE_DIR=str(models/'docling-cache'),XDG_CACHE_HOME=str(models/'cache'))
    command=[str(python),str(ROOT/'scripts/document_worker.py'),mode,str(source),str(output),
             '--tier',tier,'--models',str(models),'--limit',str(settings.document_max_figures)]
    process=await asyncio.create_subprocess_exec(*command,env=env,start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        code=await asyncio.wait_for(process.wait(),settings.document_worker_timeout)
        if code:raise ValueError('本地解析组件执行失败，请检查模型与运行环境后重试。')
    except (asyncio.TimeoutError,asyncio.CancelledError):
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        await process.wait()
        if asyncio.current_task().cancelling():raise
        raise ValueError('解析时间已达到上限，请拆分文件后重试。') from None


async def parse(settings,name,raw,tier):
    count=validate_source(settings,name,raw)
    started=time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='fyp-mineru-') as folder:
        source=Path(folder)/('source'+Path(name).suffix.lower());source.write_bytes(raw)
        output=Path(folder)/'output'
        await worker(settings,'mineru',source,output,tier=tier)
        file=output/'middle_json.json'
        if not file.is_file() or file.stat().st_size>32*1024*1024:raise ValueError('解析输出无效或过大。')
        data=json.loads(file.read_text())
        if data.get('schema')!='docvortex.middle' or data.get('schema_version')!='2.0':
            raise ValueError('MinerU 输出版本不兼容。')
        pages=[]
        for index,page in enumerate(data['pages']):
            if page.get('page_idx')!=index:raise ValueError('解析页码不连续。')
            text='\n'.join(content_text(block) for block in page['blocks'])
            pages.append(ParsedPage(index+1,text,method='mineru_'+tier,
                status='good' if text.strip() else 'unreadable',
                blocks=[{'type':b['type'],'bbox':b.get('bbox'),'text':content_text(b)} for b in page['blocks']]))
        if len(pages)!=count or sum(len(p.text) for p in pages)>4_000_000:raise ValueError('解析页数或输出长度异常。')
        return ParsedDocument(name,'mineru_'+tier,pages,tools={'mineru':'4.0.2','image_analysis':False},
            elapsed_seconds=time.perf_counter()-started)


async def extract_figures(settings,name,raw):
    validate_source(settings,name,raw)
    with tempfile.TemporaryDirectory(prefix='fyp-docling-') as folder:
        source=Path(folder)/('source'+Path(name).suffix.lower());source.write_bytes(raw)
        output=Path(folder)/'output'
        await worker(settings,'figures',source,output)
        manifest=json.loads((output/'figures.json').read_text())
        assets=[];total=0
        for info in manifest['assets'][:settings.document_max_figures]:
            file=(output/info['file']).resolve()
            if not file.is_relative_to(output.resolve()) or file.stat().st_size>8*1024*1024:
                raise ValueError('图片裁切输出无效。')
            data=file.read_bytes();total+=len(data)
            if total>32*1024*1024:raise ValueError('图片裁切输出超过限制。')
            provenance={k:info[k] for k in ('bbox','original_caption')}
            provenance.update(coordinate_basis='normalized_top_left',extractor='docling-'+manifest['version'])
            identity=hashlib.sha256(data+json.dumps([info['page'],provenance],sort_keys=True).encode()).hexdigest()[:32]
            assets.append(VisualAsset('figure-'+identity,info['page'],'figure','image/png',data,
                                      info['width'],info['height'],provenance))
        return assets,manifest.get('truncated',False)
