"""Retain verified discovery evidence in bounded, original-source context.

This is deterministic context selection, not another relevance or sufficiency
judgment. Every anchor is checked against the current parsed original, and every
selected leaf is an existing index row. The caller must freeze/validate the
returned sources before passing them to the normal evidence coverage check.
"""
from collections import OrderedDict
import hashlib
from pathlib import Path

from . import document_storage, rag_runtime
from .document_lifecycle import state as document_state
from .exploration_policy import document_source


VERSION = 'verified_anchor_context_v1'
MAX_LEAF_HITS = 24
MAX_CONTEXT_TOKENS = 4096
MAX_ANCHORS = 60


def configuration():
    return {'version': VERSION, 'max_leaf_hits': MAX_LEAF_HITS,
            'max_source_tokens': MAX_CONTEXT_TOKENS, 'max_anchors': MAX_ANCHORS,
            'expansion': 'none', 'budget_unit': 'qwen_source_serialization_tokens_v1'}


def _span(row):
    metadata = row.get('metadata', {})
    return (metadata.get('original_page_char_start', metadata.get('page_char_start')),
            metadata.get('original_page_char_end', metadata.get('page_char_end')))


def _covered(anchor, sources):
    return any(source.get('document_id') == anchor['document_id']
               and source.get('page') == anchor['page']
               and anchor['quote'] in source.get('text', '') for source in sources)


def supplement_context(pipeline, request, selected, accepted):
    """Return ``(sources, audit)`` without provider calls or database mutations."""
    audit = configuration() | {'status': 'skipped', 'reason': 'no_valid_anchors',
        'valid_anchors': 0, 'ignored_anchors': 0, 'covered_anchors_before': 0,
        'covered_anchors_after': 0, 'selected_leaf_count': 0, 'included_leaf_count': 0,
        'source_tokens': None, 'leaf_limit_reached': False}
    if not isinstance(accepted, list) or not accepted:
        return selected, audit
    if any(source.get('metadata', {}).get('source_kind') == 'figure' for source in selected):
        # Image descriptions are indexed separately from document text. Until
        # this supplement has a dedicated visual merge policy, preserve the
        # existing mixed context rather than silently dropping a selected image.
        audit['reason'] = 'mixed_visual_sources'
        return selected, audit
    try:
        retrieval = pipeline.retrieval_configuration(request)
        digest = retrieval.get('context_tokenizer_sha256')
        if not isinstance(digest, str) or not digest:
            audit['reason'] = 'no_verified_tokenizer'
            return selected, audit
        counter = rag_runtime.verified_counter(pipeline.settings.rag_tokenizer_path, digest)
    except Exception:
        # The optional native tokenizer loader raises plain Exception for a
        # malformed tokenizer JSON. Keep the already retrieved context intact.
        audit['reason'] = 'no_verified_tokenizer'
        return selected, audit
    audit['tokenizer_sha256'] = digest
    store = pipeline.store
    documents, pages_by_document = {}, {}

    def load_document(did):
        if not isinstance(did, str):
            return None
        if did in documents:
            return documents[did]
        documents[did] = None
        document = store.one('SELECT * FROM documents WHERE id=? AND course_id=?', (did, request.course_id))
        lifecycle = document_state(store, did)
        if (not document or document['status'] != 'ready' or not lifecycle['enabled'] or lifecycle['deleted_at']
                or store.chunking_configuration(did) != pipeline.chunking_configuration()):
            return None
        path = pipeline.settings.data_dir / 'documents' / (did + Path(document['name']).suffix.lower())
        try:
            report = document_storage.report_for(store, did)
            if (not report or report.get('source_document_sha256') != document['sha256']
                    or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != document['sha256']):
                return None
            pages = [page['text'] for page in report['pages']]
            if not all(isinstance(page, str) for page in pages):
                return None
        except (OSError, ValueError, TypeError, KeyError):
            return None
        chunks = store.document_chunks(did)
        signature = pipeline.embedding_signature()
        if any(row.get('embedding_signature') != signature or not row.get('vector') for row in chunks):
            return None
        valid = []
        for row in chunks:
            start, end = _span(row)
            page = row.get('page')
            if (type(page) is not int or not 1 <= page <= len(pages)
                    or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(pages[page - 1])
                    or pages[page - 1][start:end] != row.get('text')
                    or row.get('metadata', {}).get('source_document_sha256') != document['sha256']):
                continue
            leaf = {key: row[key] for key in ('id', 'document_id', 'page', 'text', 'metadata')}
            leaf.update(document_name=document['name'], score=0.0,
                        retrieval={'strategy': VERSION, 'selection': 'verified_discovery_quote'})
            provenance = document_source(store, did)
            if provenance:
                leaf['external_source'] = provenance
            valid.append(leaf)
        pages_by_document[did] = pages
        documents[did] = {'document': document, 'leaves': valid,
                          'by_id': {row['id']: row for row in valid}, 'pages': pages}
        return documents[did]

    anchors = []
    for item in accepted[:5]:
        if not isinstance(item, dict) or not isinstance(item.get('supports'), list):
            continue
        info = load_document(item.get('document_id'))
        for support in item['supports'][:12]:
            if len(anchors) >= MAX_ANCHORS:
                break
            if not info or item.get('sha256') != info['document']['sha256'] or not isinstance(support, dict):
                audit['ignored_anchors'] += 1
                continue
            quote, gap = support.get('quote'), support.get('gap_id')
            page, start, end = support.get('page'), support.get('start_char'), support.get('end_char')
            if (not isinstance(quote, str) or not 12 <= len(quote) <= 1000
                    or not isinstance(gap, str) or not gap or len(gap) > 120
                    or type(page) is not int or not 1 <= page <= len(info['pages'])
                    or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(info['pages'][page - 1])
                    or info['pages'][page - 1][start:end] != quote):
                audit['ignored_anchors'] += 1
                continue
            leaves = [row for row in info['leaves'] if row['page'] == page and _span(row)[0] < end and _span(row)[1] > start]
            if not leaves:
                audit['ignored_anchors'] += 1
                continue
            anchors.append({'document_id': item['document_id'], 'page': page, 'quote': quote,
                            'gap_id': gap, 'leaves': sorted(leaves, key=lambda row: _span(row))})
    audit['valid_anchors'] = len(anchors)
    if not anchors:
        return selected, audit
    audit['covered_anchors_before'] = sum(_covered(anchor, selected) for anchor in anchors)
    if audit['covered_anchors_before'] == len(anchors):
        audit.update(status='unchanged', reason='already_covered', covered_anchors_after=len(anchors))
        return selected, audit

    groups = OrderedDict()
    for anchor in anchors:
        group = groups.setdefault(anchor['gap_id'], OrderedDict())
        for row in anchor['leaves']:
            group.setdefault(row['id'], row)
    queues = [list(group.values()) for group in groups.values()]
    ranked, seen = [], set()
    while any(queues):
        for queue in queues:
            if queue:
                row = queue.pop(0)
                if row['id'] not in seen:
                    ranked.append(row); seen.add(row['id'])
    # Retain normal retrieval hits using their actual current leaf IDs. Expanded
    # contexts are reconstructed, never passed off as invented index entries.
    for source in selected:
        info = load_document(source.get('document_id'))
        if not info:
            audit['reason'] = 'original_sources_unavailable'
            return selected, audit
        metadata = source.get('metadata', {})
        identities = metadata.get('retrieved_chunk_ids', []) if source.get('id', '').startswith('context_') else [source.get('id')]
        if (not isinstance(identities, list) or not identities
                or any(not isinstance(identity, str) or identity not in info['by_id'] for identity in identities)):
            audit['reason'] = 'original_sources_unavailable'
            return selected, audit
        for identity in identities:
            if identity in info['by_id'] and identity not in seen:
                row = dict(info['by_id'][identity], score=source.get('score', 0.0),
                           retrieval=source.get('retrieval', {'strategy': 'original_retrieval'}))
                ranked.append(row); seen.add(identity)
    audit['leaf_limit_reached'] = len(ranked) > MAX_LEAF_HITS
    ranked = ranked[:MAX_LEAF_HITS]
    result, tokens = rag_runtime.runtime_context(ranked, pages_by_document, counter,
                                                 MAX_CONTEXT_TOKENS, 'none', MAX_LEAF_HITS)
    if not result:
        audit.update(reason='no_context_fits', selected_leaf_count=len(ranked), source_tokens=tokens)
        return selected, audit
    covered = [anchor for anchor in anchors if _covered(anchor, result)]
    included = {cid for source in result for cid in source['metadata']['retrieved_chunk_ids']}
    audit.update(status='applied', reason='verified_anchors', selected_leaf_count=len(ranked),
                 included_leaf_count=len(included), source_tokens=tokens,
                 covered_anchors_after=len(covered),
                 requirements_with_preserved_anchors=list(dict.fromkeys(anchor['gap_id'] for anchor in covered)),
                 requirements_with_missing_anchors=list(dict.fromkeys(anchor['gap_id'] for anchor in anchors if anchor not in covered)))
    return result, audit
