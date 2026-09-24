"""Optional installed-runtime integration: real ACP/MCP, only mock model calls.

RUN_HARNESS_INTEGRATION=1 enables localhost subprocess/socket tests. They never
read environment credentials or make live provider calls; the upstream HTTP
client always uses MockTransport.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings, Endpoint
from app.harness_runner import run_harness_phase, child_environment
from app.providers import ApiProviders, ProviderError
from app.store import Store
from app.task_control import JobCancelled


ENABLED = os.getenv('RUN_HARNESS_INTEGRATION') == '1'
RUNTIME = Path(__file__).resolve().parents[1] / '.venv-harness/bin/dsh'
integration = pytest.mark.skipif(not ENABLED or not RUNTIME.is_file(),
                                reason='Opt-in installed Harness local integration')
TOOLS = {'mcp__teaching__get_reference_chunks', 'mcp__teaching__cpu_schedule_v1'}
CPU_INPUT = {'policy': 'rr', 'processes': [{'id': 'A', 'arrival': 0, 'burst': 4},
    {'id': 'B', 'arrival': 1, 'burst': 2}], 'quantum': 2, 'switch_cost': 1,
    'boundary': 'before', 'during_switch': 'tail', 'early_finish_free': False}


def completion(content=None, *, calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return {'id': 'mock-harness-completion', 'object': 'chat.completion', 'created': 1234,
            'model': 'fixture-flash', 'choices': [{'index': 0, 'message': message,
            'finish_reason': 'tool_calls' if calls else 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}


def make_agent(tmp_path, wire, *, timeout=30):
    settings = Settings(data_dir=tmp_path / 'data', agent_runtime='deepseek_harness',
                        harness_timeout=timeout, max_daily_calls=100)
    settings.text = Endpoint('https://fixture.invalid/v1', 'mock-private-model-key',
                             'fixture-flash', '/chat/completions')
    store = Store(settings.data_dir)
    for owner in ('alice', 'bob'):
        store.execute('INSERT INTO users VALUES(?,?,?,?,?)', (owner, owner+'@example.test', 'fixture', owner, 'now'))
        store.execute('INSERT INTO courses VALUES(?,?,?)', (owner+'-course', owner, 'now'))
        store.execute('INSERT INTO course_owners VALUES(?,?)', (owner+'-course', owner))
        store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                      (owner+'-doc', owner+'-course', owner+'.md', owner+'-sha', 'indexed', 1, 'now'))
    job, _ = store.job('runner-fixture', 'generate', {'course_id': 'alice-course'}, owner_id='alice')
    store.save_job_evidence(job['id'], {'schema_version': 'education-agent-v1', 'configuration': {'max_calls': 30}})
    providers = ApiProviders(settings, store, httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    pipeline = SimpleNamespace(enforce_account_ownership=True, providers=providers)
    agent = SimpleNamespace(settings=settings, store=store, pipeline=pipeline, job_id=job['id'],
        request=SimpleNamespace(course_id='alice-course'), check_scope=lambda: None,
        evidence={'sources': [{'id': 'chunk-1', 'document_id': 'alice-doc', 'page': 1,
                   'document_name': 'rr.md', 'text': 'Frozen RR evidence for this job.'}]})
    return agent


@pytest.fixture
def completed_phase_transport(monkeypatch):
    """Replace subprocess/socket boundaries, retaining the real runner logic."""
    from contextlib import asynccontextmanager
    import app.harness_runner as runner
    state = SimpleNamespace(agent=None, outcome='complete', prompts=0,
                            value={'ok': True, 'fixture_result': 'already-returned'})
    monkeypatch.setattr(runner, 'verify_runtime', lambda settings: None)
    @asynccontextmanager
    async def relay(*args, **kwargs):
        kwargs['before_call']()
        state.app = SimpleNamespace(state=SimpleNamespace(audit=[]))
        yield 'http://127.0.0.1:12345/v1', state.app
    monkeypatch.setattr(runner, 'serve_relay', relay)
    class Client:
        events = []
        stderr_tail = b''
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): return {'fixture': True}
        async def new_session(self, *args): return {'sessionId': 'fixture-session'}
        async def prompt(self, *args):
            state.prompts += 1
            state.agent.store.request_cancel(state.agent.job_id)
            if state.outcome == 'relay_cancelled':
                state.app.state.audit.append({'status': 'cancelled', 'error_code': 'task_cancelled'})
            else:
                state.app.state.audit.append({'status': 'succeeded', 'response': completion(json.dumps(state.value))})
            if state.outcome == 'scope_revoked':
                state.agent.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-doc', 0, None))
            return {'stopReason': 'end_turn' if state.outcome != 'incomplete' else 'cancelled'}
    monkeypatch.setattr(runner, 'ACPClient', Client)
    return state


@pytest.mark.parametrize('outcome', ['complete', 'relay_cancelled', 'incomplete', 'scope_revoked'])
def test_runner_preserves_only_complete_authorized_response_after_cancel(tmp_path, completed_phase_transport, outcome):
    state = completed_phase_transport
    def forbidden(request): pytest.fail('Fixture cannot make external provider calls')
    state.agent = make_agent(tmp_path, forbidden)
    state.outcome = outcome
    report = {}
    async def scenario():
        try:
            phase = run_harness_phase(state.agent, [
                {'role': 'system', 'content': 'Return JSON.'},
                {'role': 'user', 'content': '{"task":"agent_author"}'}], 'author', report)
            if outcome == 'complete':
                assert await phase == state.value
            else:
                with pytest.raises(JobCancelled): await phase
        finally: await state.agent.pipeline.providers.close()
    asyncio.run(scenario())
    assert report['status'] == ('completed' if outcome == 'complete' else 'cancelled')
    assert not state.agent.store.calls_for_job(state.agent.job_id)
    assert state.agent.store.job_control(state.agent.job_id)['cancel_requested'] is True


def test_agent_checkpoints_complete_harness_phase_before_stopping_and_reuses_on_resume(tmp_path, completed_phase_transport):
    from app.generation_agent import GenerationAgent
    from app.models import GenerateRequest
    from app.pipeline import Pipeline
    state = completed_phase_transport
    def forbidden(request): pytest.fail('Fixture cannot make external provider calls')
    base = make_agent(tmp_path, forbidden)
    pipeline = Pipeline(base.settings, base.store, base.pipeline.providers, enforce_account_ownership=True)
    request = GenerateRequest(course_id='alice-course', topic='A fixture learning objective', request_key='fixture-call')
    agent = GenerationAgent(pipeline, request, base.job_id)
    attempt = {'number': 1, 'status': 'pending'}
    agent.state = {'status': 'running', 'resume_count': 0, 'call_count': 0, 'current_slot': 'q1',
                   'slots': [{'slot_id': 'q1', 'status': 'pending', 'attempts': [attempt]}]}
    agent.evidence = {'agent': agent.state, 'sources': base.evidence['sources'], 'configuration': {}}
    state.agent = agent
    async def scenario():
        try:
            with pytest.raises(JobCancelled):
                await agent.call(attempt, 'author', {'task': 'agent_author'}, 'Return JSON.')
            saved = json.loads(agent.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (agent.job_id,))['evidence'])
            record = saved['agent']['slots'][0]['attempts'][0]['author']
            assert record['response'] == state.value
            assert record['history'][0]['status'] == record['history'][0]['harness']['status'] == 'completed'
            assert saved['agent']['status'] == 'cancelled'
            with agent.store.connect() as db:
                db.execute('DELETE FROM job_controls WHERE job_id=?', (agent.job_id,))
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (agent.job_id,))
            agent.state['status'] = 'running'
            assert await agent.call(attempt, 'author', {'task': 'agent_author'}, 'Return JSON.') == state.value
            assert state.prompts == 1
        finally: await base.pipeline.providers.close()
    asyncio.run(scenario())


class TeachingWire:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert str(request.url) == 'https://fixture.invalid/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer mock-private-model-key'
        assert body['thinking'] == {'type': 'disabled'}
        assert {tool['function']['name'] for tool in body['tools']} == TOOLS
        assert body['stream'] is False
        tool_messages = [message for message in body['messages'] if message['role'] == 'tool']
        if not tool_messages:
            calls = [{'id': 'refs-1', 'type': 'function', 'function': {
                        'name': 'mcp__teaching__get_reference_chunks', 'arguments': '{}'}},
                     {'id': 'cpu-1', 'type': 'function', 'function': {
                        'name': 'mcp__teaching__cpu_schedule_v1',
                        'arguments': json.dumps({'input': CPU_INPUT})}}]
            # Match the real nonstream DeepSeek P1 wire extension; the relay
            # must canonicalize it before the runtime builds message history.
            for index, call in enumerate(calls):
                call['index'] = index
            return httpx.Response(200, json=completion(calls=calls))
        outputs = {message['tool_call_id']: json.loads(message['content']) for message in tool_messages}
        assert outputs['refs-1']['ok'] is True
        assert outputs['refs-1']['sources'][0]['text'] == 'Frozen RR evidence for this job.'
        assert outputs['cpu-1']['ok'] is True and outputs['cpu-1']['makespan'] == 8
        return httpx.Response(200, json=completion(json.dumps({'ok': True, 'makespan': 8})))


@integration
def test_real_runtime_runner_with_both_teaching_tools_and_mock_provider(tmp_path):
    wire = TeachingWire()
    agent = make_agent(tmp_path, wire)
    report = {}

    async def exercise():
        try:
            result = await run_harness_phase(agent, [
                {'role': 'system', 'content': 'Return the requested JSON.'},
                {'role': 'user', 'content': '{"task":"agent_author"}'}], 'author', report)
            assert result == {'ok': True, 'makespan': 8}
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert report['status'] == 'completed', report
    assert len(wire.requests) == 2
    assert len(agent.store.calls_for_job(agent.job_id)) == 2
    audits = [json.loads(line) for line in Path(report['tool_audit_path']).read_text().splitlines()]
    assert {audit['tool'] for audit in audits} == {'get_reference_chunks', 'cpu_schedule_v1'}
    assert len(report['model_calls']) == 2
    assert 'mock-private-model-key' not in json.dumps(report)


def test_child_environment_contains_only_ephemeral_relay_credential(monkeypatch, tmp_path):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'must-not-inherit')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'must-not-inherit')
    monkeypatch.setenv('PYTHONPATH', 'must-not-inherit')
    environment = child_environment(tmp_path / 'home', tmp_path / 'cache', 'temporary-token')
    assert set(environment) == {'PATH', 'LANG', 'DSH_HOME', 'PKG_NATIVE_CACHE_PATH', 'FYP_HARNESS_PROXY_TOKEN'}
    assert 'must-not-inherit' not in json.dumps(environment)


@integration
def test_three_roles_have_fresh_runtime_sessions_and_no_previous_role_context(tmp_path):
    wire = TeachingWire()
    agent = make_agent(tmp_path, wire)
    reports = []

    async def exercise():
        try:
            for phase in ('author', 'solve', 'review'):
                report = {}
                await run_harness_phase(agent, [
                    {'role': 'system', 'content': 'Role ' + phase},
                    {'role': 'user', 'content': json.dumps({'task': 'agent_' + phase,
                                                          'role_marker': phase + '_unique_fixture'})}], phase, report)
                reports.append(report)
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert len({report['session_id'] for report in reports}) == 3
    assert len({report['directory'] for report in reports}) == 3
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 6
    for index, role in enumerate(('author', 'solve', 'review')):
        messages = wire.requests[index * 2]['messages']
        assert len(messages) == 2
        assert role + '_unique_fixture' in json.dumps(messages)
        assert all(other + '_unique_fixture' not in json.dumps(messages)
                   for other in ('author', 'solve', 'review') if other != role)


@pytest.mark.parametrize('change', ['foreign_document', 'disabled_document'])
def test_scope_rejection_never_starts_model_or_subprocess(tmp_path, monkeypatch, change):
    import app.harness_runner as runner
    wire = TeachingWire()
    agent = make_agent(tmp_path, wire)
    monkeypatch.setattr(runner, 'verify_runtime', lambda settings: None)
    def forbidden(*args, **kwargs):
        pytest.fail('A denied scope must not start its runtime or relay.')
    monkeypatch.setattr(runner, 'serve_relay', forbidden)
    if change == 'foreign_document':
        agent.evidence['sources'][0]['document_id'] = 'bob-doc'
    else:
        agent.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-doc', 0, None))
    report = {}

    async def exercise():
        try:
            with pytest.raises(ValueError, match='范围已改变'):
                await run_harness_phase(agent, [{'role': 'system', 'content': 's'},
                    {'role': 'user', 'content': '{}'}], 'author', report)
        finally:
            await agent.pipeline.providers.close()
    asyncio.run(exercise())
    assert not wire.requests and not agent.store.calls_for_job(agent.job_id)
    assert report['status'] == 'failed'
    assert Path(report['directory'], 'report.json').is_file()


@integration
def test_real_runtime_timeout_sends_cancel_and_closes_process(tmp_path, monkeypatch):
    import app.harness_runner as runner
    from app.harness_acp import ACPClient
    seen, clients = [], []

    class ObservedClient(ACPClient):
        async def __aenter__(self):
            clients.append(self)
            return await super().__aenter__()
        async def notify(self, method, params):
            seen.append(method)
            return await super().notify(method, params)

    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json=completion('{"ok":true}'))

    monkeypatch.setattr(runner, 'ACPClient', ObservedClient)
    agent = make_agent(tmp_path, slow, timeout=.3)
    report = {}

    async def exercise():
        try:
            with pytest.raises(ProviderError, match='超时'):
                await asyncio.wait_for(run_harness_phase(agent, [
                    {'role': 'system', 'content': 'Return JSON.'},
                    {'role': 'user', 'content': '{}'}], 'author', report), 20)
        finally:
            await agent.pipeline.providers.close()
    asyncio.run(exercise())
    assert report['status'] == 'timeout'
    assert 'session/cancel' in seen
    assert len(clients) == 1 and clients[0].proc.returncode is not None
    ledger = agent.store.calls_for_job(agent.job_id)
    assert len(ledger) == 1 and ledger[0]['status'] == 'failed'
    assert report['model_calls'][0]['status'] == 'cancelled'


@integration
@pytest.mark.parametrize('gate', ['daily_cap', 'job_cap', 'disabled_during_loop'])
def test_real_runtime_loop_rechecks_scope_and_durable_budgets(tmp_path, gate):
    teaching = TeachingWire()
    agent = None

    async def wire(request):
        response = await teaching(request)
        if gate == 'disabled_during_loop':
            agent.store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-doc', 0, None))
        return response

    agent = make_agent(tmp_path, wire)
    if gate == 'daily_cap':
        agent.settings.max_daily_calls = 1
    elif gate == 'job_cap':
        agent.store.save_job_evidence(agent.job_id, {'schema_version': 'education-agent-v1',
                                                   'configuration': {'max_calls': 1}})
    report = {}

    async def exercise():
        try:
            with pytest.raises(ProviderError):
                await run_harness_phase(agent, [
                    {'role': 'system', 'content': 'Return JSON.'},
                    {'role': 'user', 'content': '{}'}], 'author', report)
        finally:
            await agent.pipeline.providers.close()
    asyncio.run(exercise())
    assert report['status'] == 'failed'
    assert len(teaching.requests) == len(agent.store.calls_for_job(agent.job_id)) == 1
    assert report['model_calls'][0]['status'] == 'succeeded'
    if gate == 'disabled_during_loop':
        audits = [json.loads(line) for line in Path(report['tool_audit_path']).read_text().splitlines()]
        assert all(audit['result']['error']['code'] == 'scope_denied' for audit in audits)
    else:
        # The runtime may not retry automatically after the single budget error.
        assert len(report['model_calls']) == 2
        assert report['model_calls'][1]['status'] == 'failed'
