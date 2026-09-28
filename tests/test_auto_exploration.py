"""Isolated orchestration contracts. Fixtures are not measured search accuracy."""
import asyncio
import copy
import hashlib
import json

import pytest

from app import auto_exploration as exploration
from app.config import Endpoint, Settings
from app.models import GenerateRequest
from app.pipeline import Pipeline, document_chunk_ids, parse_document
from app.providers import ProviderError
from app.store import Store, dumps, now, uid

QUOTE = 'Priority boosting periodically moves every waiting process back to the highest priority queue.'
BODY = QUOTE + '\nThis mechanism helps avoid starvation in a multi-level feedback queue scheduler. Rules and assumptions must be stated.'
URL = 'https://example.edu/mlfq.html'


def missing(status='missing_knowledge'):
    return {'status': status, 'requirements': [{'id': 'g1', 'need': 'MLFQ priority boosting',
        'covered': False, 'supports': [], 'queries': [] if status == 'needs_user_input' else [
            {'query': 'MLFQ priority boost starvation', 'language': 'en'},
            {'query': '多级反馈队列 优先级提升 饥饿', 'language': 'zh'}]}]}


def covered(source):
    return {'status': 'sufficient', 'requirements': [{'id': 'g1', 'need': 'MLFQ priority boosting',
        'covered': True, 'supports': [{'source_id': source['id'], 'quote': QUOTE}], 'queries': []}]}


class Providers:
    def __init__(self, settings, store):
        self.settings = settings; self.store = store; self.calls = []; self.coverage = []; self.selection = None
        self.error = None; self.events = []

    async def generate(self, messages, job_id):
        data = json.loads(messages[1]['content']); self.calls.append(data)
        self.events.append({'kind': 'model', 'task': data['task']})
        call_id = self.store.reserve_call(job_id, 'text', 'fixture', self.settings.max_daily_calls)
        self.store.execute("UPDATE calls SET status='succeeded' WHERE id=?", (call_id,))
        if self.error: raise self.error
        if data['task'] == 'exploration_coverage':
            if self.coverage:
                result = self.coverage.pop(0)
                return result(data) if callable(result) else result
            if not data['sources']:
                return missing()
            source = next(source for source in data['sources'] if QUOTE in source['text'])
            value = covered(source)
            value['requirements'][0]['supports'] = [{'passage_id': source['id']}]
            return value
        return self.selection or {'accept': True, 'reason_code': 'supports_gap',
            'supports': [{'gap_id': 'g1', 'passage_id': next(part['id'] for part in data['passages'] if QUOTE in part['text'])}]}

    async def embed(self, texts, job_id):
        self.events.append({'kind': 'embedding', 'texts': list(texts)})
        call_id = self.store.reserve_call(job_id, 'embedding', 'fixture', self.settings.max_daily_calls)
        self.store.execute("UPDATE calls SET status='succeeded' WHERE id=?", (call_id,))
        return [[1., .2] for _ in texts]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    for capability in ('text', 'embedding'):
        setattr(settings, capability, Endpoint('https://fixture.invalid/v1', 'fixture-key', 'fixture-model', '/test'))
    store = Store(tmp_path)
    store.execute('INSERT INTO courses VALUES(?,?,?)', ('course-a', 'Operating systems', now()))
    store.execute('INSERT INTO courses VALUES(?,?,?)', ('course-b', 'Other course', now()))
    providers = Providers(settings, store)
    pipeline = Pipeline(settings, store, providers)
    request = GenerateRequest(course_id='course-a', topic='MLFQ priority boost', request_key='explore-fixture', auto_explore=True)
    job, _ = store.job(request.request_key, 'generate', request.model_dump())
    seen = {'search': [], 'fetch': []}
    candidates = [{'url': URL, 'title': 'MLFQ reference', 'snippet': 'Untrusted search hint',
        'provider': 'curated', 'storage_policy': 'open_license', 'license': 'CC BY 4.0',
        'license_url': 'https://example.edu/license', 'attribution': 'Fixture author'}]

    async def search(settings, query, language='en', limit=8):
        seen['search'].append((query, language)); return copy.deepcopy(candidates)

    async def fetch(settings, url):
        seen['fetch'].append(url)
        return {'url': url, 'original_url': url, 'content_type': 'text/html', 'raw': ('<main><p>' + BODY + '</p></main>').encode(),
            'storage_policy': 'open_license'}

    monkeypatch.setattr(exploration.exploration_sources, 'search', search)
    monkeypatch.setattr(exploration.exploration_sources, 'fetch_source', fetch)
    return pipeline, request, job['id'], seen, candidates


def run(rig):
    pipeline, request, job_id, _, _ = rig
    return asyncio.run(pipeline.retrieve(request, job_id, with_trace=True))


def seed(pipeline, course='course-a', *, enabled=True):
    raw = BODY.encode(); did = uid()
    configuration = pipeline.chunking_configuration()
    pages, chunks = parse_document('local.md', raw, **configuration)
    path = pipeline.settings.data_dir / 'documents'; path.mkdir(exist_ok=True)
    (path / (did + '.md')).write_bytes(raw)
    with pipeline.store.connect() as db:
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)', (did, course, 'local.md', hashlib.sha256(raw).hexdigest(), 'parsed', len(pages), now()))
        pipeline.store.save_chunks(db, did, document_chunk_ids(chunks, did), configuration)
    asyncio.run(pipeline.index(did, rig_job(pipeline)))
    if not enabled: pipeline.store.execute('INSERT INTO document_lifecycle(document_id,enabled) VALUES(?,0)', (did,))
    return did


def rig_job(pipeline):
    return pipeline.store.one('SELECT id FROM jobs LIMIT 1')['id']


def test_no_documents_discovers_indexes_and_freezes_sources(rig):
    pipeline, request, job_id, seen, _ = rig
    selected, trace = run(rig)
    assert len(selected) == 1 and trace['exploration']['reason'] == 'sources_sufficient'
    assert len(seen['fetch']) == 1 and {language for _, language in seen['search']} == {'en', 'zh'}
    document = pipeline.store.one('SELECT * FROM documents WHERE id=?', (selected[0]['document_id'],))
    assert document['course_id'] == request.course_id and document['status'] == 'ready'
    provenance = json.loads(pipeline.store.one('SELECT metadata FROM external_document_sources')['metadata'])
    assert provenance['url'] == URL and provenance['verification'] == 'ai_selected_not_human_verified'
    assert provenance['acquired_at'] and provenance['license'] == 'CC BY 4.0'
    assert selected[0]['metadata']['source_url'] == URL
    assert pipeline.store.one("SELECT count(*) AS n FROM calls WHERE capability='search'")['n'] == 2
    before = copy.deepcopy(seen); model_calls = len(pipeline.providers.calls)
    assert run(rig) == (selected, trace)
    assert seen == before and len(pipeline.providers.calls) == model_calls


def test_harness_exploration_searches_through_scoped_agent_tool(rig, monkeypatch):
    from app import harness_bridge
    pipeline, request, job_id, seen, _ = rig
    pipeline.settings.agent_runtime = 'deepseek_harness'
    phases = []

    async def harness_phase(pipeline, request, job_id, messages, phase, sources, report,
                            *, search_callback=None, max_requests=None):
        phases.append(phase)
        report['model_calls'] = [{}]
        if phase == 'exploration_search':
            assert search_callback is not None and not sources
            query = json.loads(messages[1]['content'])['queries'][0]
            await search_callback(query['query'], query['language'])
            report['model_calls'] = [{}, {}]
            return {'searched': True}
        return await pipeline.providers.generate(messages, job_id)

    monkeypatch.setattr(harness_bridge, 'run_phase', harness_phase)
    selected, trace = run(rig)
    assert selected and trace['exploration']['reason'] == 'sources_sufficient'
    assert phases == ['exploration_coverage', 'exploration_search',
                      'exploration_selection', 'exploration_coverage']
    assert seen['search'] == [('MLFQ priority boost starvation', 'en')]
    assert len(trace['exploration']['harness_phases']) == 4


def test_newly_indexed_source_gets_frozen_gap_recheck_before_first_job_stops(rig, monkeypatch, tmp_path):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import WhitespaceSplit

    pipeline, request, job_id, seen, _ = rig
    tokenizer = Tokenizer(WordLevel({'[UNK]': 0}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = WhitespaceSplit()
    tokenizer_path = tmp_path / 'tokenizer.json'; tokenizer.save(str(tokenizer_path))
    pipeline.settings.rag_tokenizer_path = tokenizer_path
    pipeline.settings.retrieval_context_tokens = 256
    pipeline.settings.chunk_max_chars = 130
    pipeline.settings.chunk_overlap_chars = 0
    request = request.model_copy(update={'topic': 'machine learning introduction'})
    pipeline.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(request.model_dump()), job_id))
    intro = 'Machine learning uses examples and experience to improve a prediction task.'
    categories = [
        'Supervised learning trains from labeled examples to predict known targets.',
        'Unsupervised learning finds patterns in data without provided labels.',
        'Reinforcement learning trains agents using actions and rewards from an environment.',
    ]
    body = '<main>' + ''.join('<p>' + text + '</p>' for text in [intro, *categories]) + '</main>'

    async def fetch(settings, url):
        seen['fetch'].append(url)
        return {'url': url, 'original_url': url, 'content_type': 'text/html',
                'raw': body.encode(), 'storage_policy': 'open_license'}
    monkeypatch.setattr(exploration.exploration_sources, 'fetch_source', fetch)

    async def sparse_local(local_request, local_job_id, *, with_trace=False, _progress_stage='retrieval'):
        document = pipeline.store.one("SELECT id FROM documents WHERE course_id=? AND status='ready'",
                                      (local_request.course_id,))
        if document:
            rows = pipeline.store.document_chunks(document['id'])
            source = next(row for row in rows if intro in row['text'])
            return ([source], {'configuration': pipeline.retrieval_configuration(local_request),
                               'selected_count': 1, 'selected': [{'id': source['id']}]})
        return ([], {'configuration': pipeline.retrieval_configuration(local_request),
                     'selected_count': 0, 'selected': []})
    monkeypatch.setattr(pipeline, '_retrieve_local', sparse_local)

    async def scripted(messages, current_job_id):
        data = json.loads(messages[1]['content']); pipeline.providers.calls.append(data)
        call_id = pipeline.store.reserve_call(current_job_id, 'text', 'fixture', pipeline.settings.max_daily_calls)
        pipeline.store.execute("UPDATE calls SET status='succeeded' WHERE id=?", (call_id,))
        if data['task'] == 'exploration_selection':
            passage = next(part for part in data['passages'] if intro in part['text'])
            return {'accept': True, 'reason_code': 'supports_gap',
                    'supports': [{'gap_id': 'g1', 'passage_id': passage['id']}]}
        passages = data['sources']
        first = next((part for part in passages if intro in part['text']), None)
        full = '\n'.join(part['text'] for part in passages)
        complete = all(term in full for term in ('Supervised learning', 'Unsupervised learning',
                                                  'Reinforcement learning'))
        supports = ([{'passage_id': part['id']} for part in passages
                     if any(term in part['text'] for term in ('Supervised learning', 'Unsupervised learning',
                                                              'Reinforcement learning'))][:3] if complete else [])
        return {'status': 'sufficient' if complete else 'missing_knowledge', 'requirements': [
            {'id': 'g1', 'need': 'a basic machine learning definition', 'covered': first is not None,
             'supports': [{'passage_id': first['id']}] if first else [],
             'queries': [] if first else [{'query': 'machine learning examples definition', 'language': 'en'},
                                          {'query': '机器学习 定义 例子', 'language': 'zh'}]},
            {'id': 'g2', 'need': 'supervised unsupervised and reinforcement learning categories',
             'covered': complete, 'supports': supports,
             'queries': [] if complete else [{'query': 'supervised unsupervised reinforcement learning', 'language': 'en'},
                                              {'query': '监督学习 无监督学习 强化学习', 'language': 'zh'}]},
        ]}
    monkeypatch.setattr(pipeline.providers, 'generate', scripted)

    selected, trace = asyncio.run(pipeline.retrieve(request, job_id, with_trace=True))

    assert selected and trace['exploration']['reason'] == 'sources_sufficient'
    assert len(seen['fetch']) == 1
    assert trace['gap_context']['targeted_included_leaf_count'] >= 2
    assert len(trace['exploration']['assessments']) == 3
    assert [entry['status'] for entry in trace['exploration']['assessments']] == [
        'missing_knowledge', 'missing_knowledge', 'sufficient']


def test_local_sufficient_never_searches(rig):
    did = seed(rig[0])
    selected, trace = run(rig)
    assert selected[0]['document_id'] == did
    assert trace['exploration']['reason'] == 'local_sufficient'
    assert rig[3]['search'] == rig[3]['fetch'] == []
    assert trace['exploration']['search_audit'] == []


def test_empty_hybrid_results_have_scoped_failure_and_query_audit(rig):
    pipeline, _, _, seen, candidates = rig
    candidates.clear()
    selected, trace = run(rig)
    audit = trace['exploration']['search_audit']
    assert selected == [] and trace['exploration']['reason'] == 'no_search_results'
    assert '课程目录与百科搜索' in exploration.failure_message(trace)
    assert '并不代表网上没有相关资料' in exploration.failure_message(trace)
    assert audit == [{'round': 1, **query, 'provider': 'hybrid', 'status': 'succeeded',
        'candidate_count': 0, 'candidates': []} for query in missing()['requirements'][0]['queries']]
    state = json.loads(pipeline.store.one('SELECT state FROM exploration_runs')['state'])
    assert state['search_audit'] == audit
    assert seen['fetch'] == [] and len(seen['search']) == 2
    assert trace['exploration']['rounds'] == 1  # Empty searches do not repeat paid calls automatically.


def test_irrelevant_candidates_are_not_misreported_as_empty_search(rig):
    rig[0].providers.selection = {'accept': False, 'reason_code': 'irrelevant', 'supports': []}
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'no_usable_sources'
    assert trace['exploration']['rejected'] == [{'url': URL, 'reason': 'irrelevant'}]
    assert all(row['candidate_count'] == 1 for row in trace['exploration']['search_audit'])
    assert '找到了候选资料' in exploration.failure_message(trace)
    assert trace['exploration']['rounds'] == 1


@pytest.mark.parametrize('code', ['dns_failed', 'fetch_timeout', 'unsupported_type', 'too_large', 'invalid_response'])
def test_source_failure_retains_fixed_diagnostic_code(rig, monkeypatch, code):
    async def rejected(settings, url):
        raise exploration.exploration_sources.SourceRejected(code)
    monkeypatch.setattr(exploration.exploration_sources, 'fetch_source', rejected)
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'no_usable_sources'
    assert trace['exploration']['rejected'] == [{'url': URL, 'reason': 'source_unavailable', 'error_code': code}]
    assert len(rig[0].providers.calls) == 1


def test_search_audit_bounds_results_and_redacts_credentials_and_extra_fields(rig):
    pipeline, _, _, _, candidates = rig
    candidates[:] = [candidates[0] | {'url': f'https://example.edu/source-{i}',
        'title': 'Fixture fixture-key sk-secret\n' + 'long title ' * 100,
        'snippet': 'DO_NOT_PERSIST_SEARCH_SNIPPET', 'reasoning': 'DO_NOT_PERSIST_REASONING'}
        for i in range(100)]
    pipeline.settings.exploration_max_search_calls = 1
    pipeline.settings.exploration_max_candidates = 1
    pipeline.providers.selection = {'accept': False, 'reason_code': 'irrelevant', 'supports': []}
    _, trace = run(rig)
    audit = trace['exploration']['search_audit']
    assert len(audit) == 1 and audit[0]['candidate_count'] == len(audit[0]['candidates']) == 8
    assert all(set(row) == {'url', 'title'} and len(row['title']) <= 180 for row in audit[0]['candidates'])
    encoded = json.dumps(audit)
    assert all(secret not in encoded for secret in ('fixture-key', 'sk-secret', 'DO_NOT_PERSIST', '\\n'))


@pytest.mark.parametrize('response', [None, {}, [None], [{'url': 42}]])
def test_malformed_search_response_is_recorded_without_fetching(rig, monkeypatch, response):
    async def malformed(*args, **kwargs): return response
    monkeypatch.setattr(exploration.exploration_sources, 'search', malformed)
    selected, trace = run(rig)
    assert not selected and trace['exploration']['reason'] == 'search_unavailable'
    audit = trace['exploration']['search_audit']
    assert len(audit) == 1 and audit[0]['status'] == 'failed'
    assert audit[0]['error_code'] == 'search_invalid_response'
    assert audit[0]['candidate_count'] == 0 and rig[3]['fetch'] == []
    assert rig[0].store.one("SELECT status FROM calls WHERE capability='search'")['status'] == 'failed'


def test_cancelled_search_audit_does_not_retry_uncertain_upstream_call(rig, monkeypatch):
    calls = []
    async def cancelled(*args, **kwargs):
        calls.append(True)
        raise asyncio.CancelledError()
    monkeypatch.setattr(exploration.exploration_sources, 'search', cancelled)
    with pytest.raises(asyncio.CancelledError): run(rig)
    state = json.loads(rig[0].store.one('SELECT state FROM exploration_runs')['state'])
    assert state['search_audit'][0]['status'] == 'interrupted'
    assert state['status'] == 'interrupted'
    with pytest.raises(ValueError, match='中断'): run(rig)
    assert len(calls) == 1


def test_historical_no_candidates_message_remains_readable():
    assert exploration.failure_message({'exploration': {'reason': 'no_candidates'}}) == exploration.FAILURE_MESSAGES['no_candidates']
    assert exploration.failure_message({'exploration': {'reason': 'no_search_results',
        'configuration': {'provider': 'brave'}}}) == exploration.FAILURE_MESSAGES['no_search_results']


def test_download_failure_is_distinct_from_empty_search_or_rejected_evidence():
    failure = {'reason': 'no_usable_sources', 'rejected': [
        {'reason': 'source_unavailable', 'error_code': 'private_address'},
        {'reason': 'source_unavailable', 'error_code': 'fetch_timeout'}]}
    assert '已找到参考网址' in exploration.failure_message({'exploration': failure})
    assert '网络' in exploration.failure_message({'exploration': failure})
    for row in ({'reason': 'insufficient'}, {'reason': 'source_unavailable', 'error_code': 'too_large'}):
        mixed = dict(failure, rejected=failure['rejected'] + [row])
        assert exploration.failure_message({'exploration': mixed}) == exploration.FAILURE_MESSAGES['no_usable_sources']


def test_missing_user_specific_image_does_not_search_or_generate(rig):
    rig[0].providers.coverage = [missing('needs_user_input')]
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'needs_user_input'
    assert '图片' in exploration.failure_message(trace) and rig[3]['search'] == []


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(status=[]),
    lambda value: value['requirements'][0].update(covered=True),
    lambda value: value['requirements'][0]['queries'][0].update(language=[]),
    lambda value: value['requirements'][0]['queries'][0].update(query='contact student@example.test for MLFQ'),
    lambda value: value['requirements'][0]['queries'][0].update(query='MLFQ sk-1234567890abcdef0123456789'),
    lambda value: value['requirements'][0]['queries'][0].update(query='https://private.example/course'),
])
def test_invalid_or_sensitive_assessment_never_searches(rig, mutation):
    value = missing(); mutation(value); rig[0].providers.coverage = [value]
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'invalid_assessment'
    assert rig[3]['search'] == []


@pytest.mark.parametrize('quote', ['This claim was not actually in the source document.', 'MLFQ'])
def test_invented_or_keyword_only_quote_is_not_ingested(rig, quote):
    rig[0].providers.selection = {'accept': True, 'reason_code': 'supports_gap', 'supports': [{'gap_id': 'g1', 'quote': quote}]}
    selected, trace = run(rig)
    assert selected == []
    assert trace['exploration']['rejected'][0]['reason'] == 'invalid_selection'
    assert rig[0].store.all('SELECT * FROM documents') == []


def test_source_selection_cannot_mark_unknown_gap_as_supported(rig):
    rig[0].providers.selection = {'accept': True, 'reason_code': 'supports_gap', 'supports': [{'gap_id': 'g9', 'quote': QUOTE}]}
    assert run(rig)[0] == []
    assert rig[0].store.all('SELECT * FROM documents') == []


def test_link_only_candidates_are_not_downloaded(rig):
    rig[4][0]['storage_policy'] = 'link_only'
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['rejected'][0]['reason'] == 'storage_not_permitted'
    assert rig[3]['fetch'] == []


def test_other_courses_never_supply_evidence(rig):
    other = seed(rig[0], 'course-b')
    selected, _ = run(rig)
    assert other not in {source['document_id'] for source in selected}
    assert all(rig[0].store.one('SELECT course_id FROM documents WHERE id=?', (s['document_id'],))['course_id'] == 'course-a' for s in selected)


def test_only_selected_documents_and_new_sources_are_retrieved(rig):
    pipeline, request, jid, _, _ = rig
    excluded = seed(pipeline)
    # A distinct selected file is initially relevant but insufficient.
    raw = b'A course summary that lacks the priority boosting mechanism and its role in starvation avoidance.'
    did = uid(); config = pipeline.chunking_configuration()
    pages, chunks = parse_document('short.md', raw, **config)
    (pipeline.settings.data_dir / 'documents' / (did + '.md')).write_bytes(raw)
    with pipeline.store.connect() as db:
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)', (did, request.course_id, 'short.md', hashlib.sha256(raw).hexdigest(), 'parsed', 1, now()))
        pipeline.store.save_chunks(db, did, document_chunk_ids(chunks, did), config)
    asyncio.run(pipeline.index(did, jid))
    scoped = request.model_copy(update={'document_ids': [did]})
    pipeline.providers.coverage = [missing(), lambda data: covered(next(s for s in data['sources'] if QUOTE in s['text']))]
    selected, _ = asyncio.run(pipeline.retrieve(scoped, jid, with_trace=True))
    assert excluded not in {s['document_id'] for s in selected}
    assert did in {s['document_id'] for s in selected}


def test_duplicate_search_urls_fetch_once_and_complete_gap_stops_other_candidates(rig):
    pipeline, _, _, seen, candidates = rig
    candidates.append(candidates[0] | {'url': 'https://example.edu/duplicate.html'})
    selected, trace = run(rig)
    assert seen['fetch'] == [URL] and len(trace['exploration']['accepted']) == 1
    assert len(pipeline.store.all('SELECT * FROM documents')) == 1
    # Both query routes returned both candidates, but a completed selection gate
    # triggers final evidence assessment before another candidate is downloaded.
    assert [row['candidate_count'] for row in trace['exploration']['search_audit']] == [2, 2]
    assert pipeline.providers.calls[-1]['task'] == 'exploration_coverage'


@pytest.mark.parametrize('change', ['disabled', 'deleted', 'hash', 'embedding', 'configuration'])
def test_terminal_cache_rejects_changed_sources_without_new_calls(rig, change):
    pipeline = rig[0]; selected, _ = run(rig); did = selected[0]['document_id']
    if change == 'disabled': pipeline.store.execute('UPDATE document_lifecycle SET enabled=0 WHERE document_id=?', (did,))
    elif change == 'deleted': pipeline.store.execute('UPDATE document_lifecycle SET deleted_at=? WHERE document_id=?', (now(), did))
    elif change == 'hash': (pipeline.settings.data_dir / 'documents' / (did + '.html')).write_text('changed')
    elif change == 'embedding': pipeline.store.execute("UPDATE chunks SET embedding_signature='changed' WHERE document_id=?", (did,))
    elif change == 'configuration': pipeline.settings.exploration_max_documents = 1
    before = len(pipeline.providers.calls)
    with pytest.raises(ValueError): run(rig)
    assert len(pipeline.providers.calls) == before


def test_call_failure_is_not_repeated_on_resume(rig):
    rig[0].providers.error = ProviderError('fixture failure')
    with pytest.raises(ProviderError): run(rig)
    assert len(rig[0].providers.calls) == 1
    with pytest.raises(ValueError, match='中断'): run(rig)
    assert len(rig[0].providers.calls) == 1


def test_cancelled_model_call_preserves_interruption_and_no_automatic_retry(rig, monkeypatch):
    calls = []
    async def cancelled(messages, jid):
        calls.append(jid); raise asyncio.CancelledError()
    monkeypatch.setattr(rig[0].providers, 'generate', cancelled)
    with pytest.raises(asyncio.CancelledError): run(rig)
    state = json.loads(rig[0].store.one('SELECT state FROM exploration_runs')['state'])
    assert state['status'] == 'interrupted' and state['in_flight'] == 'exploration_coverage'
    with pytest.raises(ValueError, match='中断'): run(rig)
    assert len(calls) == 1


def test_model_budget_is_bounded_and_preserves_unfilled_evidence(rig):
    rig[0].settings.exploration_max_model_calls = 1
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'model_budget'
    assert len(rig[0].providers.calls) == 1 and rig[3]['fetch'] == []


def test_search_limit_counts_ledger_and_does_not_block_examining_found_sources(rig):
    rig[0].settings.exploration_max_search_calls = 1
    selected, trace = run(rig)
    assert selected and len(rig[3]['search']) == 1
    assert trace['exploration']['search_calls'] == 1


def test_candidate_and_document_limits_apply_across_rounds(rig):
    pipeline, _, _, seen, candidates = rig
    pipeline.settings.exploration_max_candidates = 2
    candidates[:] = [candidates[0] | {'url': f'https://example.edu/source-{i}.html'} for i in range(10)]
    pipeline.providers.selection = {'accept': False, 'reason_code': 'irrelevant', 'supports': []}
    selected, trace = run(rig)
    assert selected == [] and len(seen['fetch']) == 2
    assert len(trace['exploration']['rejected']) == 2


def test_partial_coverage_does_not_generate_from_some_sources(rig):
    pipeline = rig[0]; pipeline.providers.coverage = [missing(), missing()]
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'coverage_incomplete'
    assert len(trace['exploration']['accepted']) == 1
    assert len(pipeline.store.all("SELECT * FROM documents WHERE status='ready'")) == 1


def test_html_strips_commands_navigation_hidden_text_and_keeps_structure():
    text, title = exploration.clean_html(b'<title>Course</title><nav>menu</nav><main><p>Visible paragraph.</p><script>steal secrets()</script><div hidden>private</div><p>Second paragraph.</p></main>')
    assert title == 'Course' and text == 'Visible paragraph.\n\nSecond paragraph.'
    assert not any(value in text for value in ('menu', 'steal', 'private'))


def test_source_above_chunk_limit_is_rejected_before_selection(rig, monkeypatch):
    pipeline = rig[0]; pipeline.settings.exploration_max_source_chunks = 1
    async def large(settings, url):
        return {'url': url, 'content_type': 'text/html', 'raw': ('<main>' + BODY * 40 + '</main>').encode(), 'storage_policy': 'open_license'}
    monkeypatch.setattr(exploration.exploration_sources, 'fetch_source', large)
    selected, trace = run(rig)
    assert not selected and trace['exploration']['rejected'][0]['reason'] == 'source_too_broad'
    assert len(pipeline.providers.calls) == 1 and pipeline.store.all('SELECT * FROM documents') == []


def test_coverage_claim_with_nonexistent_source_quote_is_rejected():
    source = {'id': 's1', 'text': BODY}
    value = covered(source); value['requirements'][0]['supports'][0]['source_id'] = 'fabricated-id'
    assert exploration._coverage(value, [source]) is None
    value = covered(source); value['requirements'][0]['supports'][0]['quote'] = 'A sufficiently long but invented claim about source support.'
    assert exploration._coverage(value, [source]) is None


def test_daily_call_limit_applies_to_exploration_search(rig):
    rig[0].settings.max_daily_calls = 1
    with pytest.raises(ValueError, match='上限'): run(rig)
    assert len(rig[3]['search']) == 0
    assert len(rig[0].store.all('SELECT * FROM calls')) == 1


def test_unknown_search_candidate_with_publisher_license_can_be_ingested(rig):
    rig[4][0]['storage_policy'] = 'unknown'
    selected, trace = run(rig)
    assert selected and trace['exploration']['reason'] == 'sources_sufficient'
    assert len(rig[3]['fetch']) == 1


def test_final_unknown_license_after_redirect_never_uses_candidate_permission(rig, monkeypatch):
    async def unknown(settings, url):
        return {'url': 'https://other.example/document', 'original_url': url, 'content_type': 'text/html',
            'raw': BODY.encode(), 'storage_policy': 'unknown'}
    monkeypatch.setattr(exploration.exploration_sources, 'fetch_source', unknown)
    selected, trace = run(rig)
    assert not selected and trace['exploration']['rejected'][0]['reason'] == 'storage_not_permitted'
    assert len(rig[0].providers.calls) == 1 and not rig[0].store.all('SELECT * FROM documents')


def test_reassessment_cannot_drop_or_rename_initial_requirement(rig):
    second = lambda data: covered(data['sources'][0])
    def changed(data):
        value = second(data)
        value['requirements'][0]['need'] = 'Simpler unrelated goal'
        return value
    rig[0].providers.coverage = [missing(), changed]
    selected, trace = run(rig)
    assert not selected and trace['exploration']['reason'] == 'invalid_assessment'
    assert rig[0].providers.calls[-1]['fixed_requirements'] == [{'id': 'g1', 'need': 'MLFQ priority boosting'}]


def test_reassessment_cannot_drop_second_uncovered_goal(rig):
    first = missing()
    first['requirements'].append(dict(first['requirements'][0], id='g2', need='MLFQ allotment accounting'))
    rig[0].providers.coverage = [first, lambda data: covered(data['sources'][0])]
    selected, trace = run(rig)
    assert not selected and trace['exploration']['reason'] == 'invalid_assessment'


def test_html_does_not_merge_adjacent_table_numbers_or_powers():
    text, _ = exploration.clean_html(b'<table><tr><th>A</th><th>B</th></tr><tr><td>10</td><td>20</td></tr></table><p>x<sup>2</sup>+y<sub>i</sub></p>')
    assert '1020' not in text and '10 |' in text and '20 |' in text
    assert 'x^{2}+y_{i}' in text
    parsed = exploration.parse_web_html(('<main>' + BODY + '<table><tr><td>10</td><td>20</td></tr></table><img src="figure.png"></main>').encode())
    assert parsed.status == 'needs_review'
    assert 'web_table_structure_unverified' in parsed.pages[0].warnings
    assert 'web_visual_or_formula_content_not_interpreted' in parsed.pages[0].warnings


def test_html_reparse_does_not_retain_visually_hidden_inline_styles():
    text, _ = exploration.clean_html(b'<main><p style="display: none">secret</p><p>visible</p></main>')
    assert text == 'visible'


def test_html_boolean_style_attribute_does_not_break_visible_body_parsing():
    text, _ = exploration.clean_html(b'<main><p style>Visible course definition.</p><p style="visibility: hidden">hidden</p></main>')
    assert text == 'Visible course definition.'


def test_account_owner_mismatch_stops_before_any_external_call(rig):
    pipeline, request, jid, seen, _ = rig
    for identity in ('owner-a', 'owner-b'):
        pipeline.store.execute('INSERT INTO users VALUES(?,?,?,?,?)', (identity, identity + '@example.test', 'unused', identity, now()))
    pipeline.store.execute('INSERT INTO course_owners VALUES(?,?)', (request.course_id, 'owner-a'))
    pipeline.store.execute('INSERT INTO job_owners VALUES(?,?)', (jid, 'owner-b'))
    pipeline.enforce_account_ownership = True
    with pytest.raises(ValueError, match='账号'): run(rig)
    assert not pipeline.providers.calls and seen['fetch'] == seen['search'] == []


def test_selection_quote_offsets_identify_exact_original_page_text(rig):
    selected, trace = run(rig)
    support = trace['exploration']['accepted'][0]['supports'][0]
    report = json.loads(rig[0].store.one('SELECT report FROM document_parsing WHERE document_id=?', (selected[0]['document_id'],))['report'])
    text = report['pages'][support['page'] - 1]['text']
    selection_call = next(call for call in rig[0].providers.calls if call['task'] == 'exploration_selection')
    chosen = next(part for part in selection_call['passages'] if QUOTE in part['text'])
    assert text[support['start_char']:support['end_char']] == support['quote'] == chosen['text']
    assert support['start_char'] == text.index(chosen['text'])
    assert support['end_char'] == support['start_char'] + len(chosen['text'])
    assert support['page'] == 1


def test_uncertain_candidate_embedding_leaves_no_formal_source_and_does_not_retry(rig, monkeypatch):
    calls = []
    async def fail(texts, job_id):
        calls.append(list(texts))
        call_id = rig[0].store.reserve_call(job_id, 'embedding', 'fixture', rig[0].settings.max_daily_calls)
        rig[0].store.execute("UPDATE calls SET status='failed' WHERE id=?", (call_id,))
        raise ProviderError('index call interrupted')
    monkeypatch.setattr(rig[0].providers, 'embed', fail)
    with pytest.raises(ProviderError): run(rig)
    assert rig[0].store.all('SELECT * FROM documents') == []
    assert rig[0].store.all('SELECT * FROM document_lifecycle') == []
    stage = json.loads(rig[0].store.one('SELECT state FROM exploration_candidate_indexes')['state'])
    assert stage['status'] == 'retired' and stage['previous_status'] == 'interrupted'
    assert 'vectors' not in stage and 'report' not in stage
    with pytest.raises(ValueError, match='中断'): run(rig)
    assert len(calls) == 1


def test_candidate_is_indexed_before_selection_and_promoted_without_reembedding(rig, monkeypatch):
    pipeline = rig[0]
    generate = pipeline.providers.generate
    selection_seen = []; selected_coordinates = []

    async def observe(messages, job_id):
        data = json.loads(messages[1]['content'])
        if data['task'] == 'exploration_selection':
            selection_seen.append(True)
            assert pipeline.store.all('SELECT * FROM documents') == []
            assert pipeline.store.all('SELECT * FROM chunks') == []
            stage = json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes')['state'])
            assert stage['status'] == 'complete'
            assert stage['vectors']
            assert [{'id': part['id'], 'source_id': part['chunk_id'], 'text': part['text']}
                    for part in stage['passages']] == data['passages']
            selected_coordinates.extend(stage['passages'])
            assert stage['audit']['document_embedding_calls'] == 1
            assert stage['audit']['query_embedding_calls'] == 1
            assert [event['kind'] for event in pipeline.providers.events] == ['model', 'embedding', 'embedding']
        return await generate(messages, job_id)

    async def no_duplicate_index(*args, **kwargs):
        raise AssertionError('Promotion must reuse staged vectors rather than index again')

    monkeypatch.setattr(pipeline.providers, 'generate', observe)
    monkeypatch.setattr(pipeline, 'index', no_duplicate_index)
    selected, trace = run(rig)
    assert selected and selection_seen == [True]
    did = selected[0]['document_id']
    assert pipeline.store.one('SELECT status FROM documents WHERE id=?', (did,))['status'] == 'ready'
    assert pipeline.store.one('SELECT enabled FROM document_lifecycle WHERE document_id=?', (did,))['enabled'] == 1
    embeddings = [event for event in pipeline.providers.events if event['kind'] == 'embedding']
    assert sum(any(QUOTE in text for text in event['texts']) for event in embeddings) == 1
    # Candidate document, gap query, then normal retrieval query. Promotion adds none.
    assert len(embeddings) == 3
    assert all(row['vector'] and row['embedding_signature'] == pipeline.embedding_signature()
               for row in pipeline.store.document_chunks(did))
    assert trace['exploration']['candidate_retrievals'][0]['document_embedding_calls'] == 1
    support = trace['exploration']['accepted'][0]['supports'][0]
    chosen = next(part for part in selected_coordinates if QUOTE in part['text'])
    assert (support['page'], support['start_char'], support['end_char'], support['quote']) == (
        chosen['page'], chosen['start_char'], chosen['end_char'], chosen['text'])
    stage = json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes')['state'])
    assert stage['status'] == 'retired' and 'vectors' not in stage and 'passages' not in stage


def test_rejected_staged_source_never_enters_formal_library(rig):
    pipeline = rig[0]
    pipeline.providers.selection = {'accept': False, 'reason_code': 'irrelevant', 'supports': []}
    selected, trace = run(rig)
    assert selected == [] and trace['exploration']['reason'] == 'no_usable_sources'
    assert [event['kind'] for event in pipeline.providers.events] == ['model', 'embedding', 'embedding', 'model']
    for table in ('documents', 'chunks', 'external_document_sources', 'document_lifecycle'):
        assert pipeline.store.all('SELECT * FROM ' + table) == []
    assert not list((pipeline.settings.data_dir / 'documents').glob('*'))
    stage = json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes')['state'])
    assert stage['status'] == 'retired'
    assert not {'vectors', 'chunks', 'report', 'passages'} & stage.keys()
    assert trace['exploration']['rejected'][0]['reason'] == 'irrelevant'


def test_new_job_reuses_same_course_source_by_hash_after_explicit_gap_check(rig):
    pipeline, request, _, _, _ = rig
    first, _ = run(rig)
    next_request = request.model_copy(update={'request_key': 'explore-second-job'})
    job, _ = pipeline.store.job(next_request.request_key, 'generate', next_request.model_dump())
    pipeline.providers.coverage = [missing()]
    second, trace = asyncio.run(pipeline.retrieve(next_request, job['id'], with_trace=True))
    assert second[0]['document_id'] == first[0]['document_id']
    assert trace['exploration']['accepted'][0]['reused'] is True
    assert len(pipeline.store.all('SELECT * FROM documents')) == 1


def test_source_selection_checks_coverage_before_fetching_redundant_candidate(rig):
    candidates = rig[4]
    candidates.append(candidates[0] | {'url': 'https://example.edu/same-gap.html'})
    _, trace = run(rig)
    assert len(trace['exploration']['accepted']) == 1
    assert trace['exploration']['rejected'] == []
    assert rig[3]['fetch'] == [URL]
    assert [call['task'] for call in rig[0].providers.calls] == [
        'exploration_coverage', 'exploration_selection', 'exploration_coverage']


@pytest.mark.parametrize('finally_sufficient', [True, False])
def test_quote_support_requires_reassessment_and_remaining_candidate_can_run_next_round(rig, monkeypatch, finally_sufficient):
    pipeline, _, _, seen, candidates = rig
    second_url = 'https://example.edu/additional-rule.html'
    candidates.append(candidates[0] | {'url': second_url})
    second_assessment = missing()
    second_assessment['requirements'][0]['queries'] = [
        {'query': 'MLFQ priority boosting worked example', 'language': 'en'},
        {'query': '多级反馈队列 优先级提升 完整示例', 'language': 'zh'}]
    pipeline.providers.coverage = [missing(), second_assessment,
        (lambda data: covered(data['sources'][0])) if finally_sufficient else missing()]

    async def distinct_sources(settings, url):
        seen['fetch'].append(url)
        # Different originals exercise acquisition in round two rather than the
        # independent content-hash reuse path. These are synthetic judgments.
        body = BODY + ('\nAdditional source example.' if url == second_url else '')
        return {'url': url, 'original_url': url, 'content_type': 'text/html',
            'raw': ('<main><p>' + body + '</p></main>').encode(), 'storage_policy': 'open_license'}

    monkeypatch.setattr(exploration.exploration_sources, 'fetch_source', distinct_sources)
    selected, trace = run(rig)
    discovery = trace['exploration']
    assert bool(selected) is finally_sufficient
    assert discovery['reason'] == ('sources_sufficient' if finally_sufficient else 'coverage_incomplete')
    assert discovery['rounds'] == 2 and len(discovery['accepted']) == 2
    assert seen['fetch'] == [URL, second_url]
    assert [entry['round'] for entry in discovery['search_audit']] == [1, 1, 2, 2]
    assert [entry['query'] for entry in discovery['search_audit'][2:]] == [
        query['query'] for query in second_assessment['requirements'][0]['queries']]
    assert discovery['search_calls'] == 4 and discovery['model_calls'] == 5
    assessments = [call for call in pipeline.providers.calls if call['task'] == 'exploration_coverage']
    assert len(assessments) == 3
    assert all(call['fixed_requirements'] == [{'id': 'g1', 'need': 'MLFQ priority boosting'}]
               for call in assessments[1:])


def test_external_markdown_code_templates_and_images_remain_unavailable_evidence(rig):
    raw = (BODY + '\n```src\nplaceholder.py\n```\n![Figure](../figure.png)').encode()
    parsed = asyncio.run(exploration._parsed(rig[0], {'content_type': 'text/plain', 'raw': raw}, {'title': 'Raw chapter'}))
    assert parsed.status == 'needs_review'
    assert parsed.pages[0].warnings == ['markdown_external_content_not_fetched']


def test_long_public_article_indexes_relevant_literal_excerpt_with_warning(rig):
    filler = '<p>Background chronology and unrelated names. ' + ('ordinary detail ' * 20) + '</p>'
    target = '<p>Quantum coherence explains the interference of superposed states.</p>'
    raw = ('<html><body><main>' + filler * 150 + target + '</main></body></html>').encode()
    gaps = [{'id': 'g1', 'need': 'quantum coherence',
             'queries': [{'query': 'quantum coherence', 'language': 'en'}]}]
    parsed = asyncio.run(exploration._parsed(rig[0],
        {'content_type': 'text/html', 'raw': raw},
        {'title': 'Quantum article', 'provider': 'wikipedia'}, gaps))
    assert len(parsed.pages[0].text) <= exploration.MAX_BODY_CHARS
    assert 'Quantum coherence explains the interference' in parsed.pages[0].text
    assert parsed.strategy == 'visible_html_relevance_excerpt_v1'
    assert 'web_relevance_excerpt_v1' in parsed.warnings


def test_external_provenance_survives_mineru_reparse_and_reindex(rig, monkeypatch):
    from app import document_jobs, document_storage
    from app.document_parsing import ParsedDocument, ParsedPage
    pipeline, _, job_id, _, _ = rig
    selected, _ = run(rig)
    did = selected[0]['document_id']
    # Exercise the existing MinerU reparse job independently of its subprocess.
    # The stored original remains hash-checked; no network or PDF model runs.
    async def parser(settings, name, raw, tier):
        assert raw and tier == 'advanced'
        return ParsedDocument(name, 'mineru_advanced', [ParsedPage(1, BODY,
            method='mineru_advanced', status='good')], tools={'mineru': 'fixture'})
    monkeypatch.setattr(document_jobs.document_backends, 'parse', parser)
    result = asyncio.run(document_jobs.parse_document(pipeline,
        {'document_id': did, 'tier': 'advanced', 'images': False}, job_id))
    assert result['index_rebuild_required']
    report = document_storage.report_for(pipeline.store, did)
    assert report['external_source']['url'] == URL
    assert report['external_source']['title'] == 'MLFQ reference'
    assert report['external_source']['license'] == 'CC BY 4.0'
    metadata = pipeline.store.document_chunks(did)[0]['metadata']
    assert metadata['source_url'] == URL
    assert metadata['source_title'] == 'MLFQ reference'
    assert metadata['origin'] == 'auto_exploration'
    assert metadata['extraction_method'] == 'mineru_advanced'
    # Rechunking consumes the saved report again rather than the initial import.
    pipeline.settings.chunk_max_chars = 240
    asyncio.run(pipeline.index(did, job_id))
    assert all(chunk['metadata']['source_url'] == URL for chunk in pipeline.store.document_chunks(did))
    assert pipeline.store.one('SELECT course_id FROM documents WHERE id=?', (did,))['course_id'] == 'course-a'
