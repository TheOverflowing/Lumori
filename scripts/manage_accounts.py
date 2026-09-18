"""Local account migration tools. Never print restoration/session credentials."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_migration import prepare_legacy_claim
from app.config import Settings
from app.store import Store


def fingerprint(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result = {}
        for table in tables:
            if table in ('users', 'auth_sessions', 'auth_throttles', 'course_owners', 'job_owners', 'legacy_workspace_claims'):
                continue
            rows = db.execute('SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid').fetchall()
            result[table] = {'rows': len(rows), 'sha256': hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()}
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Read only: count owned/unowned records')
    prepare = sub.add_parser('prepare-legacy', help='Back up and issue one local, email-bound recovery link; stop the application first')
    prepare.add_argument('--email', required=True)
    prepare.add_argument('--output', type=Path, required=True, help='New private HTML file; contains a secret and must not be committed')
    prepare.add_argument('--ttl-hours', type=int, default=168)
    args = parser.parse_args()
    directory = Settings.from_env().data_dir.resolve()
    database = directory / 'studio.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        report = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in ('courses', 'documents', 'contents', 'jobs')}
        report['assigned_courses'] = db.execute('SELECT count(*) FROM course_owners').fetchone()[0] if 'course_owners' in tables else 0
    if args.command == 'status':
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    # Resolve parent directories only. Resolving the leaf could follow an
    # existing dangling symlink and write a private credential to its target.
    output = args.output.parent.resolve() / args.output.name
    if os.path.lexists(output):
        parser.error('--output must be a new file; existing recovery links are never overwritten.')
    if not 1 <= args.ttl_hours <= 168:
        parser.error('--ttl-hours must be between 1 and 168.')
    # Reserve a private output before invalidating earlier capabilities.
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    issued = False
    try:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = directory.parent / 'tmp' / 'account-backups' / stamp
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup, 0o700)
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(backup / 'studio.sqlite3') as destination:
            source.backup(destination)
        os.chmod(backup / 'studio.sqlite3', 0o600)
        for folder in ('documents', 'media', 'document_assets'):
            if (directory / folder).is_dir():
                shutil.copytree(directory / folder, backup / folder)
        before = fingerprint(backup / 'studio.sqlite3')
        store = Store(directory)
        after = fingerprint(database)
        if before != after:
            raise RuntimeError('Legacy data changed during preparation; inspect backup before proceeding.')
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys = db.execute('PRAGMA foreign_key_check').fetchall()
        if integrity != 'ok' or foreign_keys:
            raise RuntimeError('Database integrity check failed; inspect backup before proceeding.')
        def publish(claim):
            nonlocal descriptor
            url = 'http://127.0.0.1:8765/#restore-workspace?' + urlencode({'token': claim['token'], 'email': claim['email']})
            expiry = datetime.fromtimestamp(claim['expires_at'], timezone.utc).isoformat()
            page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>恢复原有工作区</title>
<style>body{font:17px/1.7 system-ui;max-width:650px;margin:10vh auto;padding:24px;background:#fafaf9;color:#242523}a{display:inline-block;background:#283f34;color:white;padding:12px 22px;border-radius:12px;text-decoration:none}small{color:#666}</style>
<h1>恢复原有工作区</h1><p>使用 <strong>EMAIL</strong> 注册或登录，然后领取之前的课程、资料和生成内容。</p>
<p><a href="RESTORE_URL" rel="noreferrer">打开登录与恢复页面</a></p><p>课程：COURSES · 历史任务：JOBS</p>
<small>一次性链接，有效期至 EXPIRY。此文件含恢复凭证，请保存在本机。应用须已在 8765 端口启动。</small></html>'''
            replacements = {key: html.escape(str(value), quote=True) for key, value in {
                'EMAIL': claim['email'], 'RESTORE_URL': url, 'COURSES': len(claim['courses']),
                'JOBS': len(claim['jobs']), 'EXPIRY': expiry}.items()}
            # Substitute only original placeholders, never matching inside a
            # newly inserted random token that happens to contain e.g. "JOBS".
            page = re.sub(r'EMAIL|RESTORE_URL|COURSES|JOBS|EXPIRY', lambda match: replacements[match[0]], page)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                descriptor = None
                handle.write(page)
                handle.flush()
                os.fsync(handle.fileno())
            evidence = {'before': before, 'after': after, 'unchanged': True, 'integrity_check': integrity,
                'foreign_key_violations': foreign_keys, 'email': claim['email'], 'expires_at': expiry,
                'claim_courses': len(claim['courses']), 'claim_jobs': len(claim['jobs'])}
            (backup / 'verification.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        claim = prepare_legacy_claim(store, args.email, ttl_seconds=args.ttl_hours * 3600, publish=publish)
        issued = True
        expiry = datetime.fromtimestamp(claim['expires_at'], timezone.utc).isoformat()
        print(json.dumps({'recovery_file': str(output), 'backup': str(backup), 'legacy': report,
            'claim_courses': len(claim['courses']), 'claim_jobs': len(claim['jobs']), 'expires_at': expiry,
            'original_rows_unchanged': True, 'integrity_check': integrity}, ensure_ascii=False, indent=2))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not issued:
            output.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
