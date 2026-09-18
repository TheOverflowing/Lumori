"""Explicit local recovery of a frozen, previously unowned workspace.

Registration never calls this module. A trusted local operator issues a short-lived
capability for a specific email; the signed-in account must present it with CSRF.
Only the digest is stored, and ownership transfer is atomic and single-use.
"""
import hashlib
import json
import re
import secrets
import time

from .auth import AuthError, normalize_email
from .store import dumps


_SCHEMA = '''CREATE TABLE IF NOT EXISTS legacy_workspace_claims(
    token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, courses TEXT NOT NULL,
    jobs TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    used_by TEXT REFERENCES users(id), used_at INTEGER)'''


def _job_course(db, job):
    try:
        payload = json.loads(job['payload'])
        if job['kind'] == 'generate':
            course = payload.get('course_id')
            for document_id in payload.get('document_ids') or []:
                document = db.execute('SELECT course_id FROM documents WHERE id=?', (document_id,)).fetchone()
                if not document or document['course_id'] != course:
                    return None
            return course
        if job['kind'] == 'index':
            row = db.execute('SELECT course_id FROM documents WHERE id=?', (payload.get('document_id'),)).fetchone()
        elif job['kind'] == 'media':
            row = db.execute('SELECT course_id FROM contents WHERE id=?', (payload.get('content_id'),)).fetchone()
        else:
            return None
        return row['course_id'] if row else None
    except (TypeError, ValueError, AttributeError):
        return None


def prepare_legacy_claim(store, email, ttl_seconds=86400, *, publish=None):
    """Local-only operation; return a secret once, never log this result.

    An optional publisher runs within the issuance transaction. If delivering the
    private capability fails, the previous capability remains valid.
    """
    email = normalize_email(email)
    if not 60 <= ttl_seconds <= 7 * 86400:
        raise ValueError('Recovery link lifetime must be between one minute and seven days.')
    stamp = int(time.time())
    token = secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute(_SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        courses = [row['id'] for row in db.execute('''SELECT c.id FROM courses c
            LEFT JOIN course_owners o ON o.course_id=c.id WHERE o.course_id IS NULL ORDER BY c.id''')]
        if not courses:
            raise ValueError('No unassigned legacy courses remain.')
        jobs = {}
        for row in db.execute('''SELECT j.* FROM jobs j LEFT JOIN job_owners o ON o.job_id=j.id
                WHERE o.job_id IS NULL ORDER BY j.id''').fetchall():
            course = _job_course(db, row)
            if course in courses:
                jobs[row['id']] = course
        # A new locally issued capability supersedes previous unused ones.
        db.execute('UPDATE legacy_workspace_claims SET expires_at=? WHERE used_at IS NULL', (stamp,))
        db.execute('INSERT INTO legacy_workspace_claims VALUES(?,?,?,?,?,?,NULL,NULL)',
            (hashlib.sha256(token.encode('ascii')).hexdigest(), email, dumps(courses), dumps(jobs), stamp, stamp + ttl_seconds))
        claim = {'token': token, 'email': email, 'courses': courses, 'jobs': list(jobs), 'expires_at': stamp + ttl_seconds}
        if publish is not None:
            publish(claim)
    return claim


def claim_legacy_workspace(store, user, token):
    invalid = lambda: AuthError(400, 'invalid_recovery_token', '恢复链接无效或已过期，请重新从本机生成。')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise invalid()
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_workspace_claims'").fetchone():
            raise invalid()
        db.execute('BEGIN IMMEDIATE')
        claim = db.execute('SELECT * FROM legacy_workspace_claims WHERE token_hash=?',
            (hashlib.sha256(token.encode('ascii')).hexdigest(),)).fetchone()
        if not claim or claim['expires_at'] <= int(time.time()):
            raise invalid()
        if claim['used_at'] is not None:
            raise AuthError(409, 'recovery_already_used', '此恢复链接已使用。')
        if user['email'] != claim['email']:
            raise AuthError(403, 'recovery_account_mismatch', '请登录恢复链接指定的邮箱账号。')
        courses, jobs = json.loads(claim['courses']), json.loads(claim['jobs'])
        conflict = lambda: AuthError(409, 'recovery_conflict', '旧工作区的归属已变化，请重新从本机核对并生成恢复链接。')
        # Reject all conflicts before any writes; never overwrite an owner.
        for course in courses:
            if (not db.execute('SELECT 1 FROM courses WHERE id=?', (course,)).fetchone()
                    or db.execute('SELECT 1 FROM course_owners WHERE course_id=?', (course,)).fetchone()):
                raise conflict()
            for content in db.execute('SELECT job_id FROM contents WHERE course_id=?', (course,)).fetchall():
                if content['job_id'] not in jobs:
                    raise conflict()
        for job_id, course in jobs.items():
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if (not row or _job_course(db, row) != course
                    or db.execute('SELECT 1 FROM job_owners WHERE job_id=?', (job_id,)).fetchone()):
                raise conflict()
        db.executemany('INSERT INTO course_owners VALUES(?,?)', [(course, user['id']) for course in courses])
        db.executemany('INSERT INTO job_owners VALUES(?,?)', [(job_id, user['id']) for job_id in jobs])
        db.execute('UPDATE legacy_workspace_claims SET used_by=?,used_at=? WHERE token_hash=?',
            (user['id'], int(time.time()), claim['token_hash']))
        counts = {'courses': len(courses), 'jobs': len(jobs)}
        for table in ('documents', 'contents'):
            counts[table] = sum(db.execute('SELECT count(*) FROM ' + table + ' WHERE course_id=?', (course,)).fetchone()[0]
                for course in courses)
        return counts
