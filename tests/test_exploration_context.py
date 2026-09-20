"""Offline exact-source context regressions; no model or search requests."""
import hashlib
from types import SimpleNamespace

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import WhitespaceSplit

from app import exploration_context as context
from app.auto_exploration import validate_frozen_sources
from app.rag_runtime import runtime_context, tokenizer_identity, verified_counter
from app.store import Store, dumps, now


@pytest.fixture
def rig(tmp_path):
    tokenizer = Tokenizer(WordLevel({'[UNK]': 0}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = WhitespaceSplit()
    path = tmp_path / 'test-tokenizer.json'
    tokenizer.save(str(path))
    _, digest = tokenizer_identity(path)
    store = Store(tmp_path)
    for course in ('mine', 'other'):
        store.execute('INSERT INTO courses VALUES(?,?,?)', (course, course, now()))
    pipeline = SimpleNamespace(store=store, settings=SimpleNamespace(data_dir=tmp_path, rag_tokenizer_path=path),
        retrieval_configuration=lambda _: {'context_tokenizer_sha256': digest},
        chunking_configuration=lambda: {'strategy': 'test_exact_chunks'}, embedding_signature=lambda: 'test-embedding')
    request = SimpleNamespace(course_id='mine', document_ids=[])
    return pipeline, request, verified_counter(path, digest)


def seed(rig, pages, *, did='reference', course='mine', enabled=True):
    pipeline, _, _ = rig
    store = pipeline.store
    raw = '\n\n'.join(pages).encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory = pipeline.settings.data_dir / 'documents'; directory.mkdir(exist_ok=True)
    (directory / (did + '.md')).write_bytes(raw)
    leaves = [{'id': did + '_' + str(i), 'document_id': did, 'page': i + 1, 'text': text, 'vector': [1., 0.],
               'metadata': {'page_char_start': 0, 'page_char_end': len(text), 'source_document_sha256': digest},
               'document_name': did + '.md', 'score': .8, 'retrieval': {'strategy': 'test_query'}}
              for i, text in enumerate(pages)]
    with store.connect() as db:
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)', (did, course, did + '.md', digest, 'ready', len(pages), now()))
        store.save_chunks(db, did, leaves, pipeline.chunking_configuration(), signature=pipeline.embedding_signature())
        db.execute('INSERT INTO document_parsing VALUES(?,?,?)', (did, dumps({'source_document_sha256': digest,
                   'pages': [{'text': page} for page in pages]}), now()))
        db.execute('INSERT INTO document_lifecycle(document_id,enabled) VALUES(?,?)', (did, int(enabled)))
        db.execute('INSERT INTO external_document_sources VALUES(?,?)', (did, dumps({
            'url': 'https://example.edu/' + did, 'storage_policy': 'open_license', 'license': 'CC BY 4.0',
            'attribution': 'Test author'})))
    return leaves, {'document_id': did, 'sha256': digest, 'supports': []}


def support(item, row, gap, *, quote=None):
    quote = row['text'] if quote is None else quote
    start = row['text'].index(quote)
    item['supports'].append({'gap_id': gap, 'quote': quote, 'page': row['page'],
                             'start_char': start, 'end_char': start + len(quote)})


def test_broad_query_context_restores_distinct_subtopics_and_preserves_normal_hit(rig):
    pipeline, request, counter = rig
    pages = ['Machine learning systems learn patterns from examples.',
             'Supervised learning uses labels while unsupervised learning discovers structure.',
             'A training workflow includes held out evaluation before model deployment.']
    leaves, item = seed(rig, pages)
    support(item, leaves[1], 'categories'); support(item, leaves[2], 'workflow')
    selected, _ = runtime_context(leaves[:1], {'reference': pages}, counter, 1024, 'none', 5)
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert audit['status'] == 'applied' and audit['covered_anchors_before'] == 0 and audit['covered_anchors_after'] == 2
    assert audit['source_tokens'] == counter.count('\n\n'.join(row['text'] for row in result))
    assert set(audit['requirements_with_preserved_anchors']) == {'categories', 'workflow'}
    assert {cid for row in result for cid in row['metadata']['retrieved_chunk_ids']} == {row['id'] for row in leaves}
    assert all(row['id'].startswith('context_') for row in result)
    assert all(row['external_source']['attribution'] == 'Test author' for row in result)
    validate_frozen_sources(pipeline, request, result)


def test_fabricated_quote_and_incorrect_coordinates_never_add_source_text(rig):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['This real source describes the exact original material.'])
    support(item, leaves[0], 'g1')
    item['supports'][0]['quote'] = 'Fabricated source evidence must never appear in the result.'
    result, audit = context.supplement_context(pipeline, request, [], [item])
    assert result == [] and audit['valid_anchors'] == 0 and audit['ignored_anchors'] == 1
    item['supports'][0]['quote'] = leaves[0]['text']; item['supports'][0]['start_char'] = 1
    assert context.supplement_context(pipeline, request, [], [item])[0] == []


@pytest.mark.parametrize('change', ['other_course', 'disabled', 'deleted', 'raw_changed', 'digest_changed', 'chunking_changed', 'embedding_changed'])
def test_ineligible_or_changed_sources_are_not_exposed(rig, change):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['Private or stale source evidence must remain outside the generated context.'],
                        course='other' if change == 'other_course' else 'mine', enabled=change != 'disabled')
    support(item, leaves[0], 'g1')
    if change == 'deleted': pipeline.store.execute("UPDATE document_lifecycle SET deleted_at='removed'")
    if change == 'raw_changed': (pipeline.settings.data_dir / 'documents/reference.md').write_text('Changed raw file')
    if change == 'digest_changed': item['sha256'] = 'not-the-document-sha'
    if change == 'chunking_changed': pipeline.chunking_configuration = lambda: {'strategy': 'another'}
    if change == 'embedding_changed': pipeline.embedding_signature = lambda: 'another'
    result, audit = context.supplement_context(pipeline, request, [], [item])
    assert result == [] and audit['valid_anchors'] == 0


def test_existing_complete_context_is_not_broadened(rig):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['The selected source already contains every verified quoted fact.'])
    support(item, leaves[0], 'g1')
    selected = leaves[:1]
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert result is selected and audit['reason'] == 'already_covered'


def test_missing_or_changed_tokenizer_preserves_originals(rig):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['Verified source text that requires a verified context tokenizer.'])
    support(item, leaves[0], 'g1')
    selected = []
    pipeline.settings.rag_tokenizer_path.write_text('changed tokenizer')
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert result is selected and audit['reason'] == 'no_verified_tokenizer'


def test_round_robin_requirement_reservation_under_leaf_cap(rig, monkeypatch):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['First category evidence describes the supervised learning setup.',
        'Additional category evidence describes the unsupervised learning setup.',
        'Workflow evidence describes training and held out model evaluation.',
        'Additional workflow evidence explains model deployment and monitoring.'])
    for leaf in leaves[:2]: support(item, leaf, 'categories')
    for leaf in leaves[2:]: support(item, leaf, 'workflow')
    monkeypatch.setattr(context, 'MAX_LEAF_HITS', 2)
    result, audit = context.supplement_context(pipeline, request, [], [item])
    assert audit['leaf_limit_reached'] and audit['selected_leaf_count'] == 2
    assert set(audit['requirements_with_preserved_anchors']) == {'categories', 'workflow'}
    assert len(result) == 2


def test_actual_token_serialization_stays_inside_context_cap(rig, monkeypatch):
    pipeline, request, counter = rig
    leaves, item = seed(rig, ['Every source paragraph has exactly nine separate words here.',
        'Each additional paragraph also uses nine separate words here.',
        'A third paragraph must remain outside this bounded context.'])
    for i, leaf in enumerate(leaves): support(item, leaf, 'gap-' + str(i))
    monkeypatch.setattr(context, 'MAX_CONTEXT_TOKENS', 18)
    result, audit = context.supplement_context(pipeline, request, [], [item])
    assert audit['source_tokens'] == counter.count('\n\n'.join(row['text'] for row in result)) <= 18
    assert audit['covered_anchors_after'] == 2 and audit['requirements_with_missing_anchors'] == ['gap-2']


def test_context_rejects_leaf_coordinates_that_do_not_match_current_report(rig):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['This source report is correct but a leaf was corrupted.'])
    support(item, leaves[0], 'g1')
    pipeline.store.execute("UPDATE chunks SET text='Unrelated injected chunk text'")
    result, audit = context.supplement_context(pipeline, request, [], [item])
    assert not result and audit['valid_anchors'] == 0


def test_mixed_visual_context_is_preserved_without_silently_dropping_images(rig):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['Verified textual evidence also accompanies a selected relevant image.'])
    support(item, leaves[0], 'g1')
    selected = [{'id': 'context_existing_figure', 'document_id': 'reference',
                 'text': 'An existing indexed image description.', 'metadata': {'source_kind': 'figure'}}]
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert result is selected and audit['status'] == 'skipped' and audit['reason'] == 'mixed_visual_sources'


def test_legacy_local_source_without_report_is_preserved_with_new_web_anchors(rig):
    pipeline, request, _ = rig
    local, _ = seed(rig, ['This old local reference has a usable index but no structured parsing report.'], did='legacy')
    pipeline.store.execute('DELETE FROM document_parsing WHERE document_id=?', ('legacy',))
    web, item = seed(rig, ['This newly discovered reference supports a missing subject requirement.'])
    support(item, web[0], 'g1')
    selected = local[:1]
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert result is selected and audit['reason'] == 'original_sources_unavailable'


def test_incomplete_original_context_leaf_ids_do_not_silently_drop_an_original(rig):
    pipeline, request, counter = rig
    leaves, item = seed(rig, ['Existing selected source material must remain traceable to all its indexed leaves.',
                             'New verified discovery evidence adds a second source requirement.'])
    support(item, leaves[1], 'g1')
    selected, _ = runtime_context(leaves[:1], {'reference': [row['text'] for row in leaves]}, counter, 1024, 'none', 5)
    selected[0]['metadata']['retrieved_chunk_ids'].append('missing-leaf')
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert result is selected and audit['reason'] == 'original_sources_unavailable'


def test_invalid_tokenizer_format_also_preserves_originals(rig):
    pipeline, request, _ = rig
    leaves, item = seed(rig, ['The evidence exists but the tokenizer is not a usable tokenizer file.'])
    support(item, leaves[0], 'g1')
    pipeline.settings.rag_tokenizer_path.write_text('{"not": "a tokenizer"}')
    _, digest = tokenizer_identity(pipeline.settings.rag_tokenizer_path)
    pipeline.retrieval_configuration = lambda _: {'context_tokenizer_sha256': digest}
    selected = []
    result, audit = context.supplement_context(pipeline, request, selected, [item])
    assert result is selected and audit['reason'] == 'no_verified_tokenizer'
