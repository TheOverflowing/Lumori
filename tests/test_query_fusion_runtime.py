"""Opt-in runtime contracts with isolated HTTP fixtures, never live providers."""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Endpoint
from app.generation_preparation import fingerprint
from app.models import GenerateRequest, IntentGenerateRequest
from app.query_fusion import (PROMPT_VERSION, SYSTEM_PROMPT, _validate, configuration,
                              fuse_rankings, prepare_query)
from app.store import Store, dumps
from test_workflow import rig, done, request, seed
from test_generation_agent import agent_case


def install_rewriter(monkeypatch, wire, result=None, *, rewrite_embedding_fails=False):
    result = result if result is not None else {'query': '检索增强生成中的课程参考资料召回', 'needs_clarification': False, 'reason': 'Normalize wording.'}
    original = type(wire).__call__
    seen = {'rewrite': [], 'embeddings': [], 'rerank': [], 'author': []}
    def handle(self, http_request):
        body = json.loads(http_request.content)
        if http_request.url.path.endswith('/embeddings'):
            seen['embeddings'].append(body['input'])
            if rewrite_embedding_fails and body['input'] == [result['query']]:
                self.calls += 1
                return httpx.Response(503, text='private upstream text test-key-not-real')
        if http_request.url.path.endswith('/rerank'):
            seen['rerank'].append(body)
            self.calls += 1
            return httpx.Response(200,json={'results':[{'index':i,'relevance_score':len(body['documents'])-i} for i in range(len(body['documents']))]})
        if http_request.url.path.endswith('/chat/completions'):
            if body['messages'][0]['content'] == SYSTEM_PROMPT:
                seen['rewrite'].append(body)
                self.calls += 1
                if isinstance(result, int):
                    return httpx.Response(result, text='private upstream text test-key-not-real')
                return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':dumps(result)}}]})
            seen['author'].append(body)
        return original(self, http_request)
    monkeypatch.setattr(type(wire), '__call__', handle)
    return seen


def test_default_and_false_preserve_historical_payload_and_fingerprint():
    old = GenerateRequest(**request('course'))
    explicit = GenerateRequest(**request('course'), query_fusion=False)
    assert old.model_dump() == explicit.model_dump()
    assert 'query_fusion' not in old.model_dump()
    assert fingerprint(old) == fingerprint(explicit)
    enabled = GenerateRequest(**request('course'), query_fusion=True)
    assert enabled.model_dump()['query_fusion'] is True
    assert fingerprint(enabled) != fingerprint(old)


def test_agent_minimum_budget_includes_both_optional_calls(agent_case):
    fixture = agent_case(count=1, settings_options={'agent_max_calls': 5})
    payload = json.loads(fixture.job()['payload']) | {'query_fusion': True}
    fixture.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), fixture.job_id))
    assert fixture.run()['status'] == 'failed'
    assert fixture.retrieval_calls == [] and fixture.wire.contracts == []
    assert fixture.store.one('SELECT count(*) AS n FROM calls')['n'] == 0
    assert fixture.evidence()['configuration']['retrieval']['query_fusion'] == configuration()


@pytest.mark.parametrize('value', [0, 1, 'false', 'true', None, [], {}])
def test_fusion_setting_requires_real_boolean(value):
    with pytest.raises(ValidationError):
        GenerateRequest(**request('course'), query_fusion=value)


def test_disabled_keeps_original_calls_and_config(rig, monkeypatch):
    client, app, wire = rig
    course, _ = seed(client)
    seen = install_rewriter(monkeypatch, wire)
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course) | {'query_fusion': False}))
    assert job['status'] == 'succeeded', job
    assert wire.calls - before == 3
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    assert not seen['rewrite'] and len(seen['embeddings']) == 1
    assert 'query_fusion' not in evidence['retrieval']
    assert 'query_fusion' not in evidence['configuration']['retrieval']
    assert app.state.store.one('SELECT count(*) AS n FROM query_fusion_decisions')['n'] == 0


def test_enabled_is_metered_two_route_capped_fusion_and_original_author(rig, monkeypatch):
    client, app, wire = rig
    course, did = seed(client)
    other_course, other_did = seed(client)
    seen = install_rewriter(monkeypatch, wire)
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course) | {'query_fusion': True}))
    assert job['status'] == 'succeeded', job
    assert wire.calls - before == 5  # original embedding + rewrite + rewrite embedding + author + review
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    trace = evidence['retrieval']
    assert trace['query_fusion']['status'] == 'applied'
    assert trace['query_fusion']['version'] == PROMPT_VERSION
    assert trace['configuration']['query_fusion'] == configuration()
    assert trace['query_fusion']['original_query'] == request(course)['topic']
    assert len(seen['rewrite']) == 1 and len(seen['embeddings']) == 2
    assert {source['document_id'] for source in evidence['sources']} == {did}
    assert other_did not in dumps(evidence)
    assert len(trace['selected']) == 1
    assert {trace['selected'][0]['retrieval']['query_fusion'][name] for name in ('original_rank', 'rewritten_rank')} == {1}
    assert json.loads(seen['author'][0]['messages'][1]['content'])['request']['topic'] == request(course)['topic']
    assert all(row['job_id'] == job['id'] for row in app.state.store.all('SELECT job_id FROM calls WHERE job_id=?', (job['id'],)))
    assert app.state.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (job['id'],))['n'] == 5
    assert app.state.store.one('SELECT user_id FROM job_owners WHERE job_id=?', (job['id'],))


@pytest.mark.parametrize('result,reason', [
    ({'query': '检索的作用', 'needs_clarification': False, 'reason': ''}, 'unchanged_query'),
    ({'query': '', 'needs_clarification': True, 'reason': 'Ambiguous.'}, 'ambiguous_input'),
    ({'query': 'wrong schema'}, 'invalid_output'),
    ({'query': 'test-key-not-real', 'needs_clarification': False, 'reason': ''}, 'invalid_output'),
    (503, 'rewrite_failed'),
])
def test_rewrite_fallback_keeps_original_route_without_retry(rig, monkeypatch, result, reason):
    client, app, wire = rig
    course, _ = seed(client)
    seen = install_rewriter(monkeypatch, wire, result)
    job = done(client, client.post('/api/generations', json=request(course) | {'query_fusion': True}))
    assert job['status'] == 'succeeded', job
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    trace = evidence['retrieval']['query_fusion']
    assert trace['status'] == 'fallback' and trace['reason'] == reason
    assert len(seen['rewrite']) == len(seen['embeddings']) == 1
    assert trace['rewritten_query'] is None
    assert 'private upstream text' not in dumps(evidence)
    assert 'test-key-not-real' not in dumps(evidence)


def test_second_embedding_failure_preserves_first_route_and_no_retry(rig, monkeypatch):
    client, app, wire = rig
    course, _ = seed(client)
    seen = install_rewriter(monkeypatch, wire, rewrite_embedding_fails=True)
    job = done(client, client.post('/api/generations', json=request(course) | {'query_fusion': True}))
    assert job['status'] == 'succeeded', job
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    trace = evidence['retrieval']['query_fusion']
    assert trace['status'] == 'fallback' and trace['reason'] == 'rewrite_retrieval_failed'
    assert len(seen['rewrite']) == 1 and len(seen['embeddings']) == 2
    assert len(evidence['sources']) == 1
    assert 'private upstream text' not in dumps(evidence)


def test_reranker_receives_original_query_and_deduplicated_pool(rig, monkeypatch):
    client, app, wire = rig
    course, _ = seed(client)
    app.state.settings.rerank = Endpoint('https://test.invalid/v1', 'test-key-not-real', 'fixture-rerank', '/rerank')
    seen = install_rewriter(monkeypatch, wire)
    job = done(client, client.post('/api/generations', json=request(course) | {'query_fusion': True}))
    assert job['status'] == 'succeeded', job
    assert len(seen['rerank']) == 1
    assert seen['rerank'][0]['query'] == request(course)['topic']
    assert len(seen['rerank'][0]['documents']) == 1


@pytest.mark.parametrize('action', ['unknown', 'skip'])
def test_unknown_does_not_send_offered_options_or_guess(tmp_path, action):
    store = Store(tmp_path)
    req = GenerateRequest(**request('course'), query_fusion=True, preparation_id='a'*32, clarification_action=action)
    job_id = store.job('pending-unknown', 'generate', req.model_dump())[0]['id']
    class Provider:
        async def generate(self, *args):
            pytest.fail('Unknown user intent must not call a rewrite model.')
    decision = asyncio.run(prepare_query(Provider(), store, req, job_id))
    assert decision['reason'] == 'clarification_unresolved' and decision['rewritten_query'] is None


def test_decision_survives_repeated_retrieval_without_another_call(tmp_path):
    store = Store(tmp_path)
    req = GenerateRequest(**request('course'), query_fusion=True)
    job_id = store.job('pending-cache', 'generate', req.model_dump())[0]['id']
    class Provider:
        calls = 0
        async def generate(self, messages, jid):
            self.calls += 1
            return {'query': '检索增强生成中的参考资料召回', 'needs_clarification': False, 'reason': ''}
    provider = Provider()
    first = asyncio.run(prepare_query(provider, store, req, job_id))
    second = asyncio.run(prepare_query(provider, store, req, job_id))
    assert first == second and provider.calls == 1
    req.topic = '另一个主题'
    with pytest.raises(ValueError, match='配置已改变'):
        asyncio.run(prepare_query(provider, store, req, job_id))


def test_interrupted_rewrite_is_not_automatically_repeated(tmp_path):
    store = Store(tmp_path)
    req = GenerateRequest(**request('course'), query_fusion=True)
    job_id = store.job('pending-interrupt', 'generate', req.model_dump())[0]['id']
    class Provider:
        calls = 0
        async def generate(self, *args):
            self.calls += 1
            raise asyncio.CancelledError()
    provider = Provider()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(prepare_query(provider, store, req, job_id))
    decision = asyncio.run(prepare_query(provider, store, req, job_id))
    assert decision['reason'] == 'interrupted_rewrite' and provider.calls == 1


def test_timeout_falls_back_once(tmp_path, monkeypatch):
    import app.query_fusion as fusion
    monkeypatch.setattr(fusion, 'TIMEOUT_SECONDS', .001)
    store = Store(tmp_path)
    req = GenerateRequest(**request('course'), query_fusion=True)
    job_id = store.job('pending-timeout', 'generate', req.model_dump())[0]['id']
    class Provider:
        calls = 0
        async def generate(self, *args):
            self.calls += 1
            await asyncio.sleep(30)
    provider = Provider()
    decision = asyncio.run(prepare_query(provider, store, req, job_id))
    assert decision['reason'] == 'rewrite_timeout' and provider.calls == 1


def test_answer_projection_uses_actual_user_answer_but_not_private_settings(tmp_path):
    store = Store(tmp_path)
    req = IntentGenerateRequest(**(request('course') | {'topic': '原文加真实用户补充'}), query_fusion=True,
        preparation_id='a'*32, clarification_action='answer', clarification_answer='第二个',
        original_topic='这个会不会更费资源', clarification_question='时间还是空间？',
        clarification_options=['时间复杂度', '空间复杂度'])
    job_id = store.job('pending-answer', 'generate', req.model_dump())[0]['id']
    class Provider:
        async def generate(self, messages, jid):
            projected = json.loads(messages[1]['content'])
            assert set(projected) == {'query', 'clarification'}
            assert projected['query'] == req.original_topic
            assert projected['clarification']['answer'] == '第二个'
            assert projected['clarification']['options'] == req.clarification_options
            return {'query': '空间复杂度与资源消耗', 'needs_clarification': False, 'reason': ''}
    assert asyncio.run(prepare_query(Provider(), store, req, job_id))['status'] == 'ready'


@pytest.mark.parametrize('original,rewritten,reason', [
    ('Search over 32 values', 'Search over values', 'literal_changed'),
    ('Compare BFS and DFS', 'Compare traversal methods', 'literal_changed'),
    ('Use `lower_bound`', 'Use upper_bound', 'literal_changed'),
    ('Search values', 'Search 32 values', 'number_added'),
    ('递归的空间开销', 'Recursive memory cost', 'language_changed'),
])
def test_literal_and_language_guards(original, rewritten, reason):
    result, rejected = _validate({'query': rewritten, 'needs_clarification': False, 'reason': ''}, original, original, object())
    assert result is None and rejected == reason


def test_rrf_has_stable_ties_total_cap_and_original_cosine_for_rewrite_only():
    def row(name, score):
        return {'id': name, 'text': name, 'score': score, 'retrieval': {'score': score}}
    corpus = [row('a', .9), row('b', .3), row('c', .1)]
    result = fuse_rankings([row('a', .9), row('b', .3)], [row('c', .99), row('b', .98)], corpus, {'a': .9, 'b': .3, 'c': .1}, 2)
    assert [row['id'] for row in result] == ['b', 'a']
    assert result[0]['retrieval']['query_fusion'] == {'rrf_score': 2 / 62, 'original_rank': 2, 'rewritten_rank': 2}
    full = fuse_rankings([row('a', .9)], [row('c', .99)], corpus, {'a': .9, 'b': .3, 'c': .1}, 3)
    assert full[1]['score'] == .1
    assert full[1]['retrieval']['query_fusion']['original_rank'] is None
    assert len({row['id'] for row in result}) == len(result)
