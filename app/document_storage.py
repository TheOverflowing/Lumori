"""Document parsing provenance and assets, always served through owner-checked routes."""
import hashlib
import json
from pathlib import Path
import re
import shutil

from .chunking import chunk_pages
from .document_parsing import parse_document_structured
from .store import dumps, now, uid


def parse_upload(settings, name, raw):
    return parse_document_structured(name, raw, settings.max_pages,
        strategy='adaptive_local_v1', ocr_engine=settings.document_ocr_engine,
        ocr_languages=settings.document_ocr_languages, max_ocr_pages=settings.document_ocr_max_pages,
        max_asset_pages=settings.document_visual_max_pages, timeout_seconds=settings.document_parse_timeout)


def parsed_chunks(report, configuration, raw_digest, max_chunks):
    pages = report['pages']
    chunks = chunk_pages([page['text'] for page in pages], **configuration)
    for chunk in chunks:
        page = pages[chunk['page'] - 1]
        chunk['metadata'].update(source_format=Path(report['name']).suffix.lower(),
            source_document_sha256=raw_digest, parser_version=report['version'],
            extraction_method=page['method'], extraction_status=page['status'],
            extraction_warnings=page['warnings'], source_asset_ids=page['asset_ids'],
            coordinate_basis='extracted_page_text', ocr_accuracy_verified=False)
    if len(chunks) > max_chunks:
        raise ValueError(f'解析片段超过 {max_chunks} 个，请按章节拆分资料。')
    return chunks


def report_for(store, document_id):
    row = store.one('SELECT report FROM document_parsing WHERE document_id=?', (document_id,))
    return json.loads(row['report']) if row else None


def summary(report):
    if report is None:
        return {'available': False, 'status': 'legacy_unverified'}
    return {'available': True, 'status': report['status'], 'version': report['version'],
        'metrics': report['metrics'], 'visual_assets': len(report['assets']),
        'warnings': report['warnings'], 'elapsed_seconds': report['elapsed_seconds']}


def store_assets(settings, document_id, parsed):
    report = parsed.report()
    parse_id = uid()
    directory = settings.data_dir / 'document_assets' / document_id / parse_id
    report['source_document_sha256'] = None  # Filled by the caller from the original bytes.
    metadata = {asset['id']: asset for asset in report['assets']}
    try:
        for asset in parsed.assets:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', asset.id):
                raise ValueError('Invalid parsed asset identifier.')
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg'}.get(asset.mime_type)
            if suffix is None:
                raise ValueError('Unsupported parsed asset format.')
            directory.mkdir(parents=True, exist_ok=True)
            filename = asset.id + suffix
            (directory / filename).write_bytes(asset.data)
            metadata[asset.id]['storage_name'] = parse_id + '/' + filename
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return report, directory


def save_report(db, document_id, report):
    # Content-addressed visual records remain available to older citations after
    # a document is reparsed. The route still authorizes through document owner.
    previous=db.execute('SELECT report FROM document_parsing WHERE document_id=?',(document_id,)).fetchone()
    for snapshot in ([json.loads(previous['report'])] if previous else [])+[report]:
        # A reparse can regenerate identical bytes into a new directory. Refresh
        # the location so that repairing a missing old file also repairs its URL.
        db.executemany('''INSERT INTO document_visual_assets VALUES(?,?,?,?)
            ON CONFLICT(document_id,asset_id) DO UPDATE SET metadata=excluded.metadata''',
            [(document_id,asset['id'],dumps(asset),now()) for asset in snapshot['assets']])
    db.execute('''INSERT INTO document_parsing VALUES(?,?,?) ON CONFLICT(document_id)
        DO UPDATE SET report=excluded.report,updated_at=excluded.updated_at''', (document_id, dumps(report), now()))


def asset_for(store, document_id, asset_id):
    row=store.one('SELECT metadata FROM document_visual_assets WHERE document_id=? AND asset_id=?',(document_id,asset_id))
    if row:return json.loads(row['metadata'])
    report=report_for(store,document_id)
    return next((asset for asset in report['assets'] if asset['id']==asset_id),None) if report else None


def public_report(report, document_id):
    result = json.loads(dumps(report))
    result['document_id'] = document_id
    result['source_url'] = f'/api/documents/{document_id}/source'
    for asset in result['assets']:
        asset.pop('storage_name', None)
        asset['url'] = f'/api/documents/{document_id}/assets/{asset["id"]}'
    return result
