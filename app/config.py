from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import os
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class Endpoint:
    base_url: str = ''
    api_key: str = field(default='', repr=False)
    model: str = ''
    path: str = ''
    extra: dict = field(default_factory=dict)

    @property
    def configured(self):
        return bool(self.base_url and self.model and self.api_key)

    @property
    def signature(self):
        # Credentials are deliberately excluded. Endpoint/model changes require reindexing.
        value = json.dumps([self.base_url, self.path, self.model, self.extra], sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest()[:20]

    def require(self, capability):
        if not self.configured:
            raise ValueError(f'{capability} API 尚未配置。请在本地 .env 中设置地址、模型和密钥后重启。')
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
            raise ValueError(f'{capability} API 地址格式无效。')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('远程 API 必须使用 HTTPS。')
        if '://' in self.path or not self.path.startswith('/'):
            raise ValueError('API path 必须是相对路径，以 / 开始。')

@dataclass
class Settings:
    allowed_hosts: list[str] = field(default_factory=lambda: ['127.0.0.1','localhost','[::1]','testserver'])
    data_dir: Path = ROOT / 'data'
    text: Endpoint = field(default_factory=lambda: Endpoint(path='/chat/completions'))
    embedding: Endpoint = field(default_factory=lambda: Endpoint(path='/embeddings'))
    rerank: Endpoint = field(default_factory=lambda: Endpoint(path='/rerank'))
    speech: Endpoint = field(default_factory=lambda: Endpoint(path='/audio/speech'))
    image: Endpoint = field(default_factory=lambda: Endpoint(path='/images/generations'))
    vision: Endpoint = field(default_factory=lambda: Endpoint(path='/chat/completions'))
    document_backend: str = 'legacy'
    mineru_python: Path = ROOT / '.venv-mineru/bin/python'
    mineru_runtime: Path = ROOT / 'tmp/mineru-runtime'
    docling_python: Path = ROOT / '.venv-docling/bin/python'
    docling_models: Path = ROOT / 'tmp/p4-models'
    document_worker_timeout: float = 600
    document_max_figures: int = 24
    voice: str = 'alloy'
    timeout: float = 120
    text_json_mode: bool = False
    top_k: int = 5
    retrieval_strategy: str = 'dense_v1'
    retrieval_candidate_k: int = 20
    retrieval_rrf_k: int = 60
    retrieval_mmr_lambda: float = 0.7
    retrieval_metadata_weight: float = 0.0
    retrieval_document_encoding: str = 'raw_text_v1'
    retrieval_query_encoding: str = 'raw_topic_v1'
    retrieval_context_tokens: int = 0
    retrieval_expansion: str = 'none'
    rag_tokenizer_path: Path = ROOT / '.rag-models/embedding-tokenizer/tokenizer.json'
    chunk_strategy: str = 'recursive_v1'
    chunk_max_tokens: int = 256
    chunk_max_chars: int = 1200
    chunk_overlap_chars: int = 120
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 300
    max_chunks: int = 400
    document_ocr_engine: str = 'tesseract'
    document_ocr_languages: str = 'chi_sim+eng'
    document_ocr_max_pages: int = 30
    document_visual_max_pages: int = 30
    document_parse_timeout: float = 120
    max_daily_calls: int = 100
    exploration_enabled: bool = True
    exploration_search_provider: str = 'curated'
    exploration_dns_mode: str = 'auto'
    exploration_search_api_key: str = field(default='', repr=False)
    exploration_max_rounds: int = 2
    exploration_max_documents: int = 5
    exploration_max_candidates: int = 12
    exploration_max_source_pages: int = 20
    exploration_max_source_chunks: int = 100
    exploration_max_search_calls: int = 8
    exploration_max_seconds: float = 180
    exploration_max_model_calls: int = 10
    exploration_max_bytes: int = 10 * 1024 * 1024
    exploration_fetch_timeout: float = 20
    # Direct Settings() retains the frozen offline baseline; the web/env default is agent_v1.
    generation_workflow: str = 'legacy_v3'
    agent_max_calls: int = 180
    agent_max_repairs: int = 1
    agent_context_chars: int = 48000
    # Optional candidate runtime. Native remains the measured/default workflow.
    agent_runtime: str = 'native'
    # Experimental bounded supervisor planning; sequential_v1 preserves existing behavior.
    agent_orchestration: str = 'sequential_v1'
    agent_plan_batch_size: int = 8
    # Explicit experimental specialization; never infer a course from keywords.
    agent_question_spec: str = 'none'
    harness_python: Path = ROOT / '.venv-harness/bin/python'
    harness_executable: Path = ROOT / '.venv-harness/bin/dsh'
    harness_timeout: float = 240
    harness_max_requests: int = 6
    harness_max_output_tokens: int = 4096
    harness_reasoning_effort: str = 'off'
    harness_response_format: str = 'text'
    harness_schema_repairs: int = 0
    workers: int = 2
    auth_cookie_name: str = 'fyp_session'
    auth_cookie_secure: bool = False
    auth_session_hours: int = 168
    auth_throttle_window_seconds: int = 900
    auth_login_limit: int = 10
    auth_register_limit: int = 10

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
        def ep(prefix, path):
            extra = json.loads(os.getenv(prefix + '_EXTRA_JSON', '{}'))
            if not isinstance(extra, dict): raise ValueError(prefix + '_EXTRA_JSON must be an object')
            base=os.getenv(prefix+'_BASE_URL', '').rstrip('/')
            key=os.getenv(prefix+'_API_KEY', '')
            if prefix=='RERANK' and not key and os.getenv('RERANK_USE_EMBEDDING_KEY','false').lower()=='true':
                if base!=os.getenv('EMBEDDING_BASE_URL','').rstrip('/'):
                    raise ValueError('复用嵌入密钥的重排接口必须使用相同网关地址。')
                key=os.getenv('EMBEDDING_API_KEY','')
            return Endpoint(base, key, os.getenv(prefix+'_MODEL', ''),
                            os.getenv(prefix+'_PATH', path), extra)
        return cls(allowed_hosts=[host.strip() for host in os.getenv('ALLOWED_HOSTS','127.0.0.1,localhost,[::1]').split(',') if host.strip()],
                   data_dir=Path(os.getenv('DATA_DIR', str(ROOT/'data'))).resolve(),
                   text=ep('TEXT', '/chat/completions'), embedding=ep('EMBEDDING', '/embeddings'),
                   rerank=ep('RERANK', '/rerank'),
                   vision=ep('VISION','/chat/completions') if os.getenv('VISION_BASE_URL') else ep('TEXT','/chat/completions'),
                   document_backend=os.getenv('DOCUMENT_BACKEND','mineru'),
                   mineru_python=Path(os.getenv('MINERU_PYTHON',str(ROOT/'.venv-mineru/bin/python'))),
                   mineru_runtime=Path(os.getenv('MINERU_RUNTIME',str(ROOT/'tmp/mineru-runtime'))),
                   docling_python=Path(os.getenv('DOCLING_PYTHON',str(ROOT/'.venv-docling/bin/python'))),
                   docling_models=Path(os.getenv('DOCLING_MODELS',str(ROOT/'tmp/p4-models'))),
                   document_worker_timeout=max(1,float(os.getenv('DOCUMENT_WORKER_TIMEOUT','600'))),
                   document_max_figures=max(1,min(100,int(os.getenv('DOCUMENT_MAX_FIGURES','24')))),
                   speech=ep('SPEECH', '/audio/speech'), image=ep('IMAGE', '/images/generations'),
                   voice=os.getenv('SPEECH_VOICE', 'alloy'),
                   text_json_mode=os.getenv('TEXT_JSON_MODE', 'false').lower() in ('true', '1', 'yes'),
                   top_k=int(os.getenv('RETRIEVAL_TOP_K', '5')),
                   retrieval_strategy=os.getenv('RETRIEVAL_STRATEGY', 'dense_v1'),
                   retrieval_candidate_k=int(os.getenv('RETRIEVAL_CANDIDATE_K', '20')),
                   retrieval_rrf_k=int(os.getenv('RETRIEVAL_RRF_K', '60')),
                   retrieval_mmr_lambda=float(os.getenv('RETRIEVAL_MMR_LAMBDA', '0.7')),
                   retrieval_metadata_weight=float(os.getenv('RETRIEVAL_METADATA_WEIGHT', '0')),
                   retrieval_document_encoding=os.getenv('RETRIEVAL_DOCUMENT_ENCODING', 'raw_text_v1'),
                   retrieval_query_encoding=os.getenv('RETRIEVAL_QUERY_ENCODING', 'raw_topic_v1'),
                   retrieval_context_tokens=int(os.getenv('RETRIEVAL_CONTEXT_TOKENS', '0')),
                   retrieval_expansion=os.getenv('RETRIEVAL_EXPANSION', 'none'),
                   rag_tokenizer_path=Path(os.getenv('RAG_TOKENIZER_PATH', str(ROOT/'.rag-models/embedding-tokenizer/tokenizer.json'))),
                   chunk_strategy=os.getenv('CHUNK_STRATEGY', 'recursive_v1'),
                   chunk_max_tokens=int(os.getenv('CHUNK_MAX_TOKENS', '256')),
                   chunk_max_chars=int(os.getenv('CHUNK_MAX_CHARS', '1200')),
                   chunk_overlap_chars=int(os.getenv('CHUNK_OVERLAP_CHARS', '120')),
                   max_daily_calls=max(1, int(os.getenv('MAX_DAILY_API_CALLS', '100'))),
                   exploration_enabled=os.getenv('EXPLORATION_ENABLED','true').lower() in ('true','1','yes'),
                   exploration_search_provider=os.getenv('EXPLORATION_SEARCH_PROVIDER','curated'),
                   exploration_dns_mode=os.getenv('EXPLORATION_DNS_MODE','auto'),
                   exploration_search_api_key=os.getenv('EXPLORATION_SEARCH_API_KEY',''),
                   exploration_max_rounds=max(1,min(3,int(os.getenv('EXPLORATION_MAX_ROUNDS','2')))),
                   exploration_max_documents=max(1,min(5,int(os.getenv('EXPLORATION_MAX_DOCUMENTS','5')))),
                   exploration_max_candidates=max(1,min(20,int(os.getenv('EXPLORATION_MAX_CANDIDATES','12')))),
                   exploration_max_source_pages=max(1,min(50,int(os.getenv('EXPLORATION_MAX_SOURCE_PAGES','20')))),
                   exploration_max_source_chunks=max(1,min(100,int(os.getenv('EXPLORATION_MAX_SOURCE_CHUNKS','100')))),
                   exploration_max_search_calls=max(1,min(12,int(os.getenv('EXPLORATION_MAX_SEARCH_CALLS','8')))),
                   exploration_max_seconds=max(10,min(600,float(os.getenv('EXPLORATION_MAX_SECONDS','180')))),
                   exploration_max_model_calls=max(1,min(20,int(os.getenv('EXPLORATION_MAX_MODEL_CALLS','10')))),
                   exploration_max_bytes=max(1024,min(10*1024*1024,int(os.getenv('EXPLORATION_MAX_BYTES',str(10*1024*1024))))),
                   exploration_fetch_timeout=max(1,min(30,float(os.getenv('EXPLORATION_FETCH_TIMEOUT','20')))),
                   generation_workflow=os.getenv('GENERATION_WORKFLOW', 'agent_v1'),
                   agent_max_calls=max(1, min(300, int(os.getenv('AGENT_MAX_CALLS', '180')))),
                   agent_max_repairs=max(0, min(2, int(os.getenv('AGENT_MAX_REPAIRS', '1')))),
                   agent_context_chars=max(4000, min(100000, int(os.getenv('AGENT_CONTEXT_CHARS', '48000')))),
                   agent_runtime=os.getenv('AGENT_RUNTIME', 'native'),
                   agent_orchestration=os.getenv('AGENT_ORCHESTRATION', 'sequential_v1'),
                   agent_plan_batch_size=max(1, min(10, int(os.getenv('AGENT_PLAN_BATCH_SIZE', '8')))),
                   agent_question_spec=os.getenv('AGENT_QUESTION_SPEC', 'none'),
                   harness_python=Path(os.getenv('HARNESS_PYTHON', str(ROOT/'.venv-harness/bin/python'))),
                   harness_executable=Path(os.getenv('HARNESS_EXECUTABLE', str(ROOT/'.venv-harness/bin/dsh'))),
                   harness_timeout=max(10, min(600, float(os.getenv('HARNESS_TIMEOUT', '240')))),
                   harness_max_requests=max(1, min(12, int(os.getenv('HARNESS_MAX_REQUESTS', '6')))),
                   harness_max_output_tokens=max(256, min(32768, int(os.getenv('HARNESS_MAX_OUTPUT_TOKENS', '4096')))),
                   harness_reasoning_effort=os.getenv('HARNESS_REASONING_EFFORT', 'off'),
                   harness_response_format=os.getenv('HARNESS_RESPONSE_FORMAT', 'text'),
                   harness_schema_repairs=max(0, min(1, int(os.getenv('HARNESS_SCHEMA_REPAIRS', '0')))),
                   document_ocr_engine=os.getenv('DOCUMENT_OCR_ENGINE', 'tesseract'),
                   document_ocr_languages=os.getenv('DOCUMENT_OCR_LANGUAGES', 'chi_sim+eng'),
                   document_ocr_max_pages=max(0, int(os.getenv('DOCUMENT_OCR_MAX_PAGES', '30'))),
                   document_visual_max_pages=max(0, int(os.getenv('DOCUMENT_VISUAL_MAX_PAGES', '30'))),
                   document_parse_timeout=max(1, float(os.getenv('DOCUMENT_PARSE_TIMEOUT', '120'))),
                   auth_cookie_name=os.getenv('AUTH_COOKIE_NAME', 'fyp_session'),
                   auth_cookie_secure=os.getenv('AUTH_COOKIE_SECURE', 'false').lower() in ('true', '1', 'yes'),
                   auth_session_hours=max(1, int(os.getenv('AUTH_SESSION_HOURS', '168'))),
                   auth_throttle_window_seconds=max(1, int(os.getenv('AUTH_THROTTLE_WINDOW_SECONDS', '900'))),
                   auth_login_limit=max(1, int(os.getenv('AUTH_LOGIN_LIMIT', '10'))),
                   auth_register_limit=max(1, int(os.getenv('AUTH_REGISTER_LIMIT', '10'))))
