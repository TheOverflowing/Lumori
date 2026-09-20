"""Harness proxy contract and durable budgets through MockTransport; no live APIs."""
import asyncio
import copy
import json
from dataclasses import dataclass

import httpx
import pytest

from app.config import Endpoint, Settings
from app.harness_relay import create_relay_app, serve_relay
from app.providers import ApiProviders
from app.store import Store, now


TOKEN = 'fixture-relay-bearer-token-only'
REAL_KEY = 'fixture-real-upstream-key-private'
MODEL = 'fixture-flash'
REQUEST = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Explain the supplied rule.'}]}


def chat_response(**changes):
    return {'id': 'chatcmpl-fixture', 'object': 'chat.completion', 'created': 1234, 'model': MODEL,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'A concise answer.'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}, **changes}


class RelayWire:
    def __init__(self, response=None, status=200, delay=0):
        self.response = chat_response() if response is None else response
        self.status = status
        self.delay = delay
        self.calls = []

    async def __call__(self, request):
        self.calls.append({'url': str(request.url), 'headers': dict(request.headers),
                           'body': json.loads(request.content)})
        if self.delay:
            await asyncio.sleep(self.delay)
        return httpx.Response(self.status, json=self.response)


@dataclass
class RelayRig:
    app: object
    providers: ApiProviders
    wire: RelayWire
    store: Store
    settings: Settings
    job_id: str

    async def request(self, body=None, *, token=TOKEN, headers=None, raw=None, path='/v1/chat/completions'):
        request_headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        request_headers.update(headers or {})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://relay.test') as client:
            if raw is not None:
                return await client.post(path, content=raw, headers=request_headers)
            return await client.post(path, json=copy.deepcopy(REQUEST if body is None else body), headers=request_headers)

    def post(self, *args, **kwargs):
        return asyncio.run(self.request(*args, **kwargs))

    def ledger(self):
        return self.store.calls_for_job(self.job_id)


@pytest.fixture
def relay_case(tmp_path):
    providers_to_close = []

    def create(*, response=None, upstream_status=200, delay=0, max_daily_calls=100,
               max_requests=6, max_output_tokens=4096, before_call=None, extra=None, job_cap=None,
               allowed_tool_names=None):
        settings = Settings(data_dir=tmp_path / str(len(providers_to_close)), max_daily_calls=max_daily_calls)
        settings.text = Endpoint('https://upstream.invalid/v1', REAL_KEY, MODEL, '/chat/completions', extra or {})
        store = Store(settings.data_dir)
        store.execute('INSERT INTO users VALUES(?,?,?,?,?)', ('user-1', 'relay@example.test', 'unused', 'Fixture', now()))
        job, _ = store.job('relay-fixture', 'generate', {'course_id': 'fixture'}, owner_id='user-1')
        if job_cap is not None:
            store.save_job_evidence(job['id'], {'schema_version': 'education-agent-v1', 'configuration': {'max_calls': job_cap}})
        wire = RelayWire(response, upstream_status, delay)
        providers = ApiProviders(settings, store, httpx.AsyncClient(transport=httpx.MockTransport(wire)))
        providers_to_close.append(providers)
        app = create_relay_app(providers, job['id'], TOKEN, max_requests=max_requests,
                               max_output_tokens=max_output_tokens, before_call=before_call,
                               allowed_tool_names=allowed_tool_names)
        return RelayRig(app, providers, wire, store, settings, job['id'])

    yield create
    for providers in providers_to_close:
        asyncio.run(providers.close())


def test_nonstream_request_uses_original_provider_model_key_and_ledger(relay_case):
    rig = relay_case()
    response = rig.post(REQUEST | {'max_tokens': 8000})
    assert response.status_code == 200 and response.json() == chat_response()
    assert len(rig.wire.calls) == len(rig.ledger()) == 1
    sent = rig.wire.calls[0]
    assert sent['url'] == 'https://upstream.invalid/v1/chat/completions'
    assert sent['headers']['authorization'] == 'Bearer ' + REAL_KEY
    assert TOKEN not in json.dumps(sent)
    assert sent['body']['model'] == MODEL
    assert sent['body']['stream'] is False and sent['body']['n'] == 1
    assert sent['body']['max_tokens'] == 4096
    assert rig.ledger()[0]['status'] == 'succeeded'
    assert rig.ledger()[0]['usage'] == chat_response()['usage']
    assert rig.app.state.audit[0]['response'] == chat_response()
    assert 'authorization' not in json.dumps(rig.app.state.audit).lower()


@pytest.mark.parametrize('token', [None, '', 'invalid-token'])
def test_invalid_bearer_auth_cannot_reach_provider(relay_case, token):
    rig = relay_case()
    assert rig.post(token=token).status_code == 401
    assert not rig.wire.calls and not rig.ledger() and rig.app.state.request_count == 0


@pytest.mark.parametrize('headers', [{'Cookie': 'session=' + TOKEN}, {'X-API-Key': TOKEN}, {'Api-Key': REAL_KEY}])
def test_cookies_and_alternative_credentials_are_rejected_even_with_valid_bearer(relay_case, headers):
    rig = relay_case()
    assert rig.post(headers=headers).status_code == 400
    assert not rig.wire.calls and not rig.ledger()


def test_query_token_and_documentation_routes_do_not_expose_parent_audit(relay_case):
    rig = relay_case()
    assert rig.post(path='/v1/chat/completions?token=' + TOKEN).status_code == 400

    async def read_routes():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(rig.app), base_url='http://relay.test') as client:
            return [await client.get(path) for path in ('/audit', '/docs', '/openapi.json')]

    assert all(response.status_code == 404 for response in asyncio.run(read_routes()))
    assert not rig.wire.calls


@pytest.mark.parametrize('changes', [
    {'model': 'a-different-model'}, {'n': 2}, {'n': True}, {'stream': 'true'},
    {'base_url': 'https://attacker.invalid'}, {'url': 'http://127.0.0.1/secret'},
    {'headers': {'Authorization': 'Bearer ' + REAL_KEY}}, {'api_key': REAL_KEY},
    {'max_tokens': 0}, {'max_tokens': 3.5}, {'max_tokens': True},
    {'max_tokens': 100, 'max_completion_tokens': 200}, {'temperature': 3},
    {'tools': [{'type': 'web_search'}]}, {'tool_choice': 'required'},
    {'messages': [{'role': 'tool', 'content': 'result without call id'}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://attacker.invalid'}}]}]},
    {'stream_options': {'include_usage': 'yes'}},
])
def test_untrusted_parameters_cannot_override_relay_contract(relay_case, changes):
    rig = relay_case()
    response = rig.post(REQUEST | changes)
    assert response.status_code == 400
    assert not rig.wire.calls and not rig.ledger()
    assert REAL_KEY not in response.text and TOKEN not in response.text


@pytest.mark.parametrize('raw', [b'{"messages": NaN}', b'{"messages":', b'\xff'])
def test_malformed_json_fails_before_provider_call(relay_case, raw):
    rig = relay_case()
    assert rig.post(raw=raw).status_code == 400
    assert not rig.wire.calls


@pytest.mark.parametrize('raw', [b' ' * 300001, json.dumps(REQUEST | {'messages': [{'role': 'user', 'content': 'x' * 100001}]}).encode()])
def test_byte_and_character_budgets_are_enforced(relay_case, raw):
    rig = relay_case()
    assert rig.post(raw=raw).status_code == 413
    assert not rig.wire.calls and not rig.ledger()


def test_message_count_is_bounded(relay_case):
    rig = relay_case()
    assert rig.post(REQUEST | {'messages': [{'role': 'user', 'content': 'x'}] * 201}).status_code == 400
    assert not rig.wire.calls


@pytest.mark.parametrize('field', ['max_tokens', 'max_completion_tokens'])
@pytest.mark.parametrize('requested,expected', [(100, 100), (9000, 1024)])
def test_requested_output_tokens_can_only_lower_configured_cap(relay_case, field, requested, expected):
    rig = relay_case(max_output_tokens=1024)
    assert rig.post(REQUEST | {field: requested}).status_code == 200
    assert rig.wire.calls[0]['body'][field] == expected


@pytest.mark.parametrize('extra', [{'max_tokens': 9999}, {'max_completion_tokens': 9999}])
def test_provider_extras_cannot_bypass_output_token_cap(relay_case, extra):
    rig = relay_case(extra=extra)
    assert rig.post().status_code == 400
    assert not rig.wire.calls and not rig.ledger()


def test_stream_is_buffered_then_reemitted_as_valid_tool_sse_with_usage(relay_case):
    calls = [
        {'id': 'call-a', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"graphs"}'}},
        {'id': 'call-b', 'type': 'function', 'function': {'name': 'check', 'arguments': '{}'}},
    ]
    upstream = chat_response(choices=[{'index': 0, 'message': {'role': 'assistant', 'content': 'Checking.',
                             'reasoning_content': 'A concise fixture rationale.', 'tool_calls': calls},
                             'finish_reason': 'tool_calls'}])
    rig = relay_case(response=upstream)
    response = rig.post(REQUEST | {'stream': True, 'stream_options': {'include_usage': True},
                                  'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}],
                                  'tool_choice': 'auto'})
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
    records = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
    assert records[-1] == '[DONE]'
    chunks = [json.loads(record) for record in records[:-1]]
    deltas = [choice['delta'] for chunk in chunks for choice in chunk['choices']]
    assert deltas[0] == {'role': 'assistant'}
    assert {'reasoning_content': 'A concise fixture rationale.'} in deltas
    assert {'content': 'Checking.'} in deltas
    tool_deltas = [call for delta in deltas for call in delta.get('tool_calls', [])]
    assert tool_deltas == [dict(call, index=index) for index, call in enumerate(calls)]
    assert chunks[-2]['choices'][0]['finish_reason'] == 'tool_calls'
    assert chunks[-1]['choices'] == [] and chunks[-1]['usage'] == upstream['usage']
    assert all(chunk['object'] == 'chat.completion.chunk' for chunk in chunks)
    sent = rig.wire.calls[0]['body']
    assert sent['stream'] is False and 'stream_options' not in sent
    assert rig.app.state.audit[0]['requested_stream'] is True
    assert rig.app.state.audit[0]['response'] == upstream


def test_local_request_cap_is_atomic_across_concurrent_sessions(relay_case):
    rig = relay_case(max_requests=2, delay=.02)

    async def request_all():
        return await asyncio.gather(*(rig.request() for _ in range(8)))

    responses = asyncio.run(request_all())
    assert sorted(response.status_code for response in responses) == [200, 200] + [429] * 6
    assert len(rig.wire.calls) == len(rig.ledger()) == rig.app.state.request_count == 2


@pytest.mark.parametrize('budget', ['daily', 'job'])
def test_existing_daily_and_job_caps_are_respected_without_extra_upstream_calls(relay_case, budget):
    rig = relay_case(max_daily_calls=1 if budget == 'daily' else 100, job_cap=1 if budget == 'job' else None)
    assert rig.post().status_code == 200
    assert rig.post().status_code == 502
    assert len(rig.wire.calls) == len(rig.ledger()) == 1
    assert rig.app.state.audit[-1]['status'] == 'failed'


def test_original_account_scope_is_retained_in_daily_budget(relay_case):
    rig = relay_case(max_daily_calls=1)
    rig.store.execute('INSERT INTO users VALUES(?,?,?,?,?)', ('user-2', 'other@example.test', 'unused', 'Other', now()))
    other_job, _ = rig.store.job('other-owner', 'generate', {}, owner_id='user-2')
    rig.store.reserve_call(other_job['id'], 'text', MODEL, 1)
    assert rig.post().status_code == 200  # Another user's spend does not block this job.
    another_same_owner, _ = rig.store.job('same-owner', 'generate', {}, owner_id='user-1')
    app = create_relay_app(rig.providers, another_same_owner['id'], TOKEN)
    rig.app = app
    assert rig.post().status_code == 502
    assert len(rig.wire.calls) == 1  # Same account's earlier job consumes its daily quota.


def test_async_scope_guard_runs_before_each_network_call_and_denial_spends_nothing(relay_case):
    checks = []

    async def before_call():
        checks.append(True)
        await asyncio.sleep(0)
        if len(checks) == 2:
            raise ValueError('private scope detail ' + REAL_KEY)

    rig = relay_case(before_call=before_call)
    assert rig.post().status_code == 200
    response = rig.post()
    assert response.status_code == 403 and REAL_KEY not in response.text
    assert len(checks) == 2 and len(rig.wire.calls) == len(rig.ledger()) == 1
    assert rig.app.state.request_count == 1


def test_false_scope_guard_rejects_before_provider_call(relay_case):
    rig = relay_case(before_call=lambda: False)
    assert rig.post().status_code == 403
    assert not rig.wire.calls and not rig.ledger()


@pytest.mark.parametrize('upstream', [
    {'choices': []},
    chat_response(choices=[{'index': 0, 'message': {'role': 'assistant', 'content': 'truncated'}, 'finish_reason': 'length'}]),
    chat_response(choices=[{'index': 0, 'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'stop'}]),
    chat_response(choices=[{'index': 0, 'message': {'role': 'assistant', 'content': None}, 'finish_reason': 'tool_calls'}]),
])
def test_malformed_or_incomplete_completion_records_failed_ledger_and_safe_error(relay_case, upstream):
    rig = relay_case(response=upstream)
    response = rig.post()
    assert response.status_code == 502
    assert len(rig.wire.calls) == 1 and rig.ledger()[0]['status'] == 'failed'
    assert rig.app.state.audit[0]['status'] == 'failed'
    assert 'response' not in rig.app.state.audit[0]


def test_upstream_error_never_echoes_raw_error_credentials_or_auto_retries(relay_case):
    rig = relay_case(upstream_status=401, response={'error': {'message': 'private ' + REAL_KEY + ' ' + TOKEN}})
    response = rig.post()
    assert response.status_code == 502
    assert len(rig.wire.calls) == 1 and rig.ledger()[0]['status'] == 'failed'
    for serialized in (response.text, json.dumps(rig.app.state.audit), json.dumps(rig.ledger())):
        assert REAL_KEY not in serialized and TOKEN not in serialized
        assert 'private ' not in serialized


def test_success_bodies_and_audit_redact_credentials_even_if_accidentally_echoed(relay_case):
    upstream = chat_response(choices=[{'index': 0, 'message': {'role': 'assistant',
                             'content': f'{REAL_KEY} {TOKEN}'}, 'finish_reason': 'stop'}],
                             headers={'Authorization': 'Bearer ' + REAL_KEY},
                             metadata={TOKEN: REAL_KEY})
    rig = relay_case(response=upstream)
    response = rig.post(REQUEST | {'messages': [{'role': 'user', 'content': f'Do not retain {TOKEN} or {REAL_KEY}.'}]})
    assert response.status_code == 200
    assert 'headers' not in response.json()
    for serialized in (response.text, json.dumps(rig.app.state.audit), json.dumps(rig.wire.calls[0]['body'])):
        assert REAL_KEY not in serialized and TOKEN not in serialized
    assert '[REDACTED]' in response.text


def test_serve_relay_binds_only_ephemeral_loopback_and_closes_its_server(relay_case, monkeypatch):
    # Exercise lifetime management without opening a real socket in the test suite.
    import app.harness_relay as relay
    import uvicorn

    rig = relay_case()
    sockets, servers = [], []

    class FakeSocket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)
        def bind(self, address):
            self.address = address
        def listen(self, backlog):
            assert backlog > 0
        def setblocking(self, blocking):
            assert blocking is False
        def getsockname(self):
            return ('127.0.0.1', 32123)
        def close(self):
            self.closed = True

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            self.force_exit = False
            self.finished = False
            servers.append(self)
        async def serve(self, *, sockets):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(.001)
            self.finished = True

    async def exercise():
        # Patch after asyncio has constructed its own event-loop wakeup sockets.
        with monkeypatch.context() as patch:
            patch.setattr(relay.socket, 'socket', FakeSocket)
            patch.setattr(uvicorn, 'Server', FakeServer)
            async with serve_relay(rig.providers, rig.job_id, TOKEN) as (base_url, app):
                assert base_url == 'http://127.0.0.1:32123/v1'
                assert app.state.audit == []
                assert sockets[0].address == ('127.0.0.1', 0)
                assert not sockets[0].closed
            assert sockets[0].closed and servers[0].finished

    asyncio.run(exercise())


def test_official_runtime_thinking_disabled_is_forwarded_and_audited(relay_case):
    rig = relay_case()
    response = rig.post(REQUEST | {'thinking': {'type': 'disabled'}})
    assert response.status_code == 200
    assert rig.wire.calls[0]['body']['thinking'] == {'type': 'disabled'}
    assert rig.app.state.audit[0]['request']['thinking'] == {'type': 'disabled'}


@pytest.mark.parametrize('thinking', [None, 'disabled', {'type': 'enabled'}, {},
                                     {'type': 'disabled', 'budget_tokens': 1024}])
def test_other_thinking_parameters_fail_before_provider(relay_case, thinking):
    rig = relay_case()
    assert rig.post(REQUEST | {'thinking': thinking}).status_code == 400
    assert not rig.wire.calls and not rig.ledger()


def test_configured_extra_cannot_reenable_thinking(relay_case):
    rig = relay_case(extra={'thinking': {'type': 'enabled'}})
    assert rig.post(REQUEST | {'thinking': {'type': 'disabled'}}).status_code == 400
    assert not rig.wire.calls and not rig.ledger()


def test_tool_allowlist_restricts_advertised_history_and_upstream_calls(relay_case):
    name = 'mcp__teaching__get_reference_chunks'
    tool = {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
    rig = relay_case(allowed_tool_names={name})
    assert rig.post(REQUEST | {'tools': [tool]}).status_code == 200
    foreign_tool = {'type': 'function', 'function': {'name': 'shell', 'parameters': {'type': 'object'}}}
    assert rig.post(REQUEST | {'tools': [foreign_tool]}).status_code == 400
    foreign_call = {'id': 'call-foreign', 'type': 'function', 'function': {'name': 'shell', 'arguments': '{}'}}
    assert rig.post(REQUEST | {'messages': [{'role': 'assistant', 'content': None,
                                           'tool_calls': [foreign_call]}]}).status_code == 400
    assert len(rig.wire.calls) == len(rig.ledger()) == 1
    rig.wire.response = chat_response(choices=[{'index': 0, 'message': {'role': 'assistant',
                        'content': None, 'tool_calls': [foreign_call]}, 'finish_reason': 'tool_calls'}])
    assert rig.post(REQUEST | {'tools': [tool]}).status_code == 502
    assert rig.ledger()[-1]['status'] == 'failed'


def test_empty_tool_allowlist_denies_all_tools(relay_case):
    rig = relay_case(allowed_tool_names=set())
    assert rig.post().status_code == 200
    assert rig.post(REQUEST | {'tools': [{'type': 'function', 'function': {'name': 'shell'}}]}).status_code == 400
    assert len(rig.wire.calls) == 1


def test_effective_provider_options_are_preserved_in_audit(relay_case):
    rig = relay_case(extra={'temperature': .2, 'max_tokens': 2048, 'thinking': {'type': 'disabled'}})
    assert rig.post().status_code == 200
    assert rig.app.state.audit[0]['request'] == rig.wire.calls[0]['body']


def test_extra_cannot_replace_reviewed_tool_allowlist(relay_case):
    rig = relay_case(allowed_tool_names={'safe'}, extra={'tools': [
        {'type': 'function', 'function': {'name': 'foreign'}}]})
    assert rig.post().status_code == 400
    assert not rig.wire.calls and not rig.ledger()


def observed_deepseek_tool_response():
    """Minimal structural fixture from P1_20260919/diagnostic.json; no course text.

    IDs/content/arguments are replaced with fixtures. The observed nonstream
    extension is index=0/1 on the tool calls, with finish_reason=tool_calls.
    """
    return chat_response(choices=[{'index': 0, 'logprobs': None, 'message': {
        'role': 'assistant', 'content': 'Compute with the deterministic tool.',
        'tool_calls': [{'index': index, 'id': 'fixture-call-' + str(index), 'type': 'function',
                        'function': {'name': 'mcp__teaching__cpu_schedule_v1',
                                     'arguments': json.dumps({'input': {'policy': policy}})}}
                       for index, policy in enumerate(('rr', 'stcf'))]}, 'finish_reason': 'tool_calls'}])


@pytest.mark.parametrize('stream', [False, True])
def test_observed_deepseek_nonstream_indices_normalize_for_harness(relay_case, stream):
    raw = observed_deepseek_tool_response()
    before = copy.deepcopy(raw)
    rig = relay_case(response=raw, allowed_tool_names={'mcp__teaching__cpu_schedule_v1'})
    response = rig.post(REQUEST | {'stream': stream})
    assert response.status_code == 200
    assert rig.ledger()[0]['status'] == 'succeeded'
    normalized = rig.app.state.audit[0]['response']['choices'][0]['message']['tool_calls']
    assert all('index' not in call for call in normalized)
    assert raw == before  # Normalization must not destroy the source evidence.
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        calls = [call for event in events for choice in event['choices']
                 for call in choice.get('delta', {}).get('tool_calls', [])]
        assert [call['index'] for call in calls] == [0, 1]
    else:
        assert response.json()['choices'][0]['message']['tool_calls'] == normalized
        followup = REQUEST | {'messages': [REQUEST['messages'][0],
                                          response.json()['choices'][0]['message']]}
        assert rig.post(followup).status_code == 200


@pytest.mark.parametrize('index', [True, False, -1, 1, 2, '0', 0.0, None])
def test_tampered_nonstream_tool_index_fails_with_sanitized_diagnostics(relay_case, index):
    raw = observed_deepseek_tool_response()
    raw['choices'][0]['message']['tool_calls'][0]['index'] = index
    raw['choices'][0]['message']['content'] = REAL_KEY + ' ' + TOKEN
    rig = relay_case(response=raw)
    assert rig.post().status_code == 502
    audit = rig.app.state.audit[0]
    assert audit['status'] == rig.ledger()[0]['status'] == 'failed'
    assert audit['validation_error']['code'] == 'invalid_tool_call_index'
    assert audit['raw_response']['choices'][0]['message']['tool_calls'][0]['index'] == index
    assert REAL_KEY not in json.dumps(audit) and TOKEN not in json.dumps(audit)
    assert 'response' not in audit


def test_nonstream_index_normalization_does_not_allow_other_extensions(relay_case):
    raw = observed_deepseek_tool_response()
    raw['choices'][0]['message']['tool_calls'][0]['arbitrary_extension'] = 'untrusted'
    rig = relay_case(response=raw)
    assert rig.post().status_code == 502
    assert rig.app.state.audit[0]['validation_error']['code'] == 'invalid_tool_call_fields'
    assert rig.app.state.audit[0]['raw_response'] == raw
    assert rig.ledger()[0]['status'] == 'failed'


def test_incoming_history_still_rejects_provider_only_tool_indices(relay_case):
    rig = relay_case()
    raw = observed_deepseek_tool_response()
    assert rig.post(REQUEST | {'messages': [raw['choices'][0]['message']]}).status_code == 400
    assert not rig.wire.calls and not rig.ledger()
