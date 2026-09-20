import sqlite3
import json
import uuid
import hashlib
from datetime import datetime, timezone
from contextlib import contextmanager


def uid(): return uuid.uuid4().hex

def now(): return datetime.now(timezone.utc).isoformat()

def dumps(obj): return json.dumps(obj, ensure_ascii=False, allow_nan=False)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,course_id TEXT NOT NULL REFERENCES courses(id),name TEXT NOT NULL,sha256 TEXT NOT NULL,status TEXT NOT NULL,pages INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(course_id,sha256));
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),page INTEGER NOT NULL,text TEXT NOT NULL,vector TEXT,embedding_signature TEXT);
CREATE TABLE IF NOT EXISTS chunk_metadata(chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_chunking(document_id TEXT PRIMARY KEY REFERENCES documents(id),configuration TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_parsing(document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,report TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_visual_assets(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,asset_id TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(document_id,asset_id));
CREATE TABLE IF NOT EXISTS document_lifecycle(document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),deleted_at TEXT);
CREATE TABLE IF NOT EXISTS document_options(document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE, options TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS figure_semantics(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,asset_id TEXT NOT NULL,status TEXT NOT NULL,cache_key TEXT,description TEXT,vector TEXT,embedding_signature TEXT,error TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(document_id,asset_id));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contents(id TEXT PRIMARY KEY,course_id TEXT NOT NULL REFERENCES courses(id),job_id TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL,asset TEXT NOT NULL,sources TEXT NOT NULL,config TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(content_id TEXT NOT NULL,version INTEGER NOT NULL,asset TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(content_id,version));
CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY,content_id TEXT NOT NULL REFERENCES contents(id),version INTEGER NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,mime TEXT NOT NULL,status TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY,content_id TEXT NOT NULL,version INTEGER NOT NULL,metrics TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS current_evaluations(content_id TEXT NOT NULL REFERENCES contents(id),version INTEGER NOT NULL,evaluation_id TEXT NOT NULL REFERENCES evaluations(id),updated_at TEXT NOT NULL,PRIMARY KEY(content_id,version));
CREATE TABLE IF NOT EXISTS evaluation_history(id TEXT PRIMARY KEY,evaluation_id TEXT NOT NULL REFERENCES evaluations(id),metrics TEXT NOT NULL,saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,job_id TEXT,capability TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,usage TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_evidence(job_id TEXT PRIMARY KEY REFERENCES jobs(id),evidence TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_timelines(job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,timeline TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS query_fusion_decisions(job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,request_sha256 TEXT NOT NULL,decision TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exploration_runs(job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exploration_candidate_indexes(job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,candidate_sha256 TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(job_id,candidate_sha256));
CREATE TABLE IF NOT EXISTS external_document_sources(document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,metadata TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,display_name TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS auth_throttles(bucket TEXT PRIMARY KEY,attempts INTEGER NOT NULL,window_start INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS course_owners(course_id TEXT PRIMARY KEY REFERENCES courses(id) ON DELETE CASCADE,user_id TEXT NOT NULL REFERENCES users(id));
CREATE INDEX IF NOT EXISTS ix_course_owners_user ON course_owners(user_id);
CREATE TABLE IF NOT EXISTS job_owners(job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,user_id TEXT NOT NULL REFERENCES users(id));
CREATE INDEX IF NOT EXISTS ix_job_owners_user ON job_owners(user_id);
CREATE TABLE IF NOT EXISTS preparation_uses(preparation_id TEXT PRIMARY KEY REFERENCES jobs(id),job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id));
'''

class Conflict(Exception): pass

class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'studio.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            # Additive migration: retain existing courses, drafts and call history.
            columns={row['name'] for row in db.execute('PRAGMA table_info(calls)')}
            for name,kind in [('duration_ms','INTEGER'),('http_status','INTEGER'),('response_model','TEXT')]:
                if name not in columns:db.execute(f'ALTER TABLE calls ADD COLUMN {name} {kind}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally: db.close()

    def all(self, sql, args=()):
        with self.connect() as db: return [dict(r) for r in db.execute(sql,args).fetchall()]
    def one(self, sql, args=()):
        rows=self.all(sql,args);return rows[0] if rows else None
    def execute(self, sql, args=()):
        with self.connect() as db: return db.execute(sql,args).rowcount

    @staticmethod
    def decode_chunk(row):
        row = dict(row)
        value = row.pop('chunk_metadata_json', None)
        try:
            metadata = json.loads(value) if value else {}
        except (TypeError, ValueError):
            metadata = {}
        row['metadata'] = metadata if isinstance(metadata, dict) else {}
        return row

    def document_chunks(self, document_id):
        return [self.decode_chunk(row) for row in self.all('''
            SELECT c.*,m.metadata AS chunk_metadata_json FROM chunks c
            LEFT JOIN chunk_metadata m ON m.chunk_id=c.id
            WHERE c.document_id=? ORDER BY c.page,c.rowid''', (document_id,))]

    def chunking_configuration(self, document_id):
        row = self.one('SELECT configuration FROM document_chunking WHERE document_id=?', (document_id,))
        if not row: return None
        try: return json.loads(row['configuration'])
        except (TypeError, ValueError): return None

    @staticmethod
    def save_chunks(db, document_id, chunks, configuration, *, signature=None):
        """Caller owns the transaction; side tables keep old six-column rows readable."""
        db.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?)',
            [(c['id'], document_id, c['page'], c['text'],
              dumps(c['vector']) if 'vector' in c else None, signature) for c in chunks])
        db.executemany('INSERT INTO chunk_metadata VALUES(?,?)',
            [(c['id'], dumps(c.get('metadata', {}))) for c in chunks])
        db.execute('''INSERT INTO document_chunking VALUES(?,?,?)
            ON CONFLICT(document_id) DO UPDATE SET configuration=excluded.configuration,updated_at=excluded.updated_at''',
            (document_id, dumps(configuration), now()))

    def save_job_evidence(self, job_id, evidence):
        self.execute('INSERT INTO job_evidence VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET evidence=excluded.evidence,updated_at=excluded.updated_at',
                     (job_id,dumps(evidence),now()))

    def calls_for_job(self, job_id):
        from .providers import safe_usage
        rows=self.all('SELECT capability,model,status,usage,created_at,duration_ms,http_status,response_model FROM calls WHERE job_id=? ORDER BY created_at',(job_id,))
        for row in rows:
            # Older databases may contain unfiltered upstream usage metadata.
            try:row['usage']=safe_usage(json.loads(row['usage']))
            except (TypeError,ValueError):row['usage']={}
        return rows

    def job(self, key, kind, payload, *, owner_id=None, preparation_id=None):
        # Keep historical positional inserts valid and legacy rows unassigned.
        # User-controlled keys cannot collide across accounts or with legacy jobs.
        if owner_id is not None:
            key = 'account:' + hashlib.sha256(dumps([owner_id, key]).encode()).hexdigest()
        encoded=dumps(payload)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM jobs WHERE request_key=?',(key,)).fetchone()
            if old:
                if owner_id is not None:
                    owner = db.execute('SELECT user_id FROM job_owners WHERE job_id=?', (old['id'],)).fetchone()
                    if not owner or owner['user_id'] != owner_id:
                        raise Conflict('请求标识不可用，请使用新的请求标识。')
                if old['kind']!=kind or old['payload']!=encoded: raise Conflict('重复请求标识对应不同参数')
                return dict(old),False
            if preparation_id and db.execute('SELECT 1 FROM preparation_uses WHERE preparation_id=?',(preparation_id,)).fetchone():
                raise Conflict('此次理解检查已用于生成，请重新检查。')
            row=dict(id=uid(),request_key=key,kind=kind,payload=encoded,status='queued',result=None,error=None,created_at=now(),updated_at=now())
            db.execute('INSERT INTO jobs VALUES(:id,:request_key,:kind,:payload,:status,:result,:error,:created_at,:updated_at)',row)
            if owner_id is not None:
                db.execute('INSERT INTO job_owners(job_id,user_id) VALUES(?,?)', (row['id'], owner_id))
            if preparation_id:
                db.execute('INSERT INTO preparation_uses(preparation_id,job_id) VALUES(?,?)',(preparation_id,row['id']))
            return row,True

    def reserve_call(self, job_id, capability, model, limit):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            agent=db.execute('''SELECT json_extract(evidence,'$.configuration.max_calls') AS cap
                FROM job_evidence WHERE job_id=? AND json_extract(evidence,'$.schema_version')='education-agent-v1' ''',(job_id,)).fetchone()
            if agent and type(agent['cap']) is int:
                total=db.execute('SELECT count(*) FROM calls WHERE job_id=?',(job_id,)).fetchone()[0]
                if total>=agent['cap']:raise ValueError('已达到本任务 API 调用上限；检索、失败请求及跨日续做均计入上限。')
            date=now()[:10]
            owner = db.execute('SELECT user_id FROM job_owners WHERE job_id=?', (job_id,)).fetchone()
            if owner:
                count = db.execute('''SELECT count(*) FROM calls c JOIN job_owners o ON o.job_id=c.job_id
                    WHERE o.user_id=? AND substr(c.created_at,1,10)=?''', (owner['user_id'], date)).fetchone()[0]
            else:
                # Offline scripts retain their old global accounting semantics.
                count=db.execute('SELECT count(*) FROM calls WHERE substr(created_at,1,10)=?',(date,)).fetchone()[0]
            if count>=limit: raise ValueError('今日 API 调用次数已达到本地上限；次日 UTC 重置或调整配置。')
            call_id=uid()
            db.execute('INSERT INTO calls(id,job_id,capability,model,status,usage,created_at) VALUES(?,?,?,?,?,?,?)',(call_id,job_id,capability,model,'started','{}',now()))
            return call_id
