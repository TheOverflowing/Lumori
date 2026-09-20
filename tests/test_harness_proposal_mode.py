"""Supervisor proposals have no tools; larger budgets retain existing defaults.

Installed-runtime cases use only MockTransport, never external model calls.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import harness_runner as runner
from app.config import Settings
from app.providers import ProviderError
from test_harness_runner import CPU_INPUT, TOOLS, completion, integration, make_agent


def messages(task):
    return [
        {'role': 'system', 'content': 'Return the requested JSON object.'},
        {'role': 'user', 'content': json.dumps({
            'task': task,
            'schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                       'required': ['ok'], 'additionalProperties': False},
        })},
    ]


@pytest.mark.parametrize(('mode', 'task', 'expected'), [
    ('supervisor_v1', 'agent_plan', True),
    ('supervisor_v1', 'agent_author_cpu_spec', True),
    ('supervisor_v1', 'agent_author', False),
    ('supervisor_v1', 'agent_solve', False),
    ('supervisor_v1', 'agent_review', False),
    ('sequential_v1', 'agent_plan', False),
    ('sequential_v1', 'agent_author_cpu_spec', False),
])
def test_proposal_roles_require_explicit_supervisor_configuration(mode, task, expected):
    assert runner.proposal_only(Settings(agent_orchestration=mode), messages(task)) is expected


@pytest.mark.parametrize(('task', 'phase', 'required'), [
    ('agent_plan', 'plan', 'do not write questions, numerical answers'),
    ('agent_author_cpu_spec', 'author', 'exactly one candidate CPU problem specification'),
    ('agent_author_cpu_spec', 'author_schema_repair', 'exactly one candidate CPU problem specification'),
])
def test_proposal_prompt_never_instructs_model_to_call_tools(task, phase, required):
    instruction = runner.phase_instructions(messages(task), phase, proposal=True)
    assert 'No tools are available' in instruction
    assert required in instruction
    assert 'call cpu_schedule_v1' not in instruction
    assert 'MCP function-call channel' not in instruction
    assert 'ONLY allowed top-level keys in your final JSON are: ok.' in instruction
    assert 'JSON object alone' in instruction


def test_existing_tool_phase_prompt_retains_calculation_requirement():
    assert 'call cpu_schedule_v1' in runner.phase_instructions(messages('agent_solve'), 'solve')


@pytest.mark.parametrize(('tokens', 'batch', 'expected_tokens', 'expected_batch'), [
    (None, None, 4096, 8),
    ('32768', '10', 32768, 10),
    ('99999', '999', 32768, 10),
    ('100', '0', 256, 1),
])
def test_environment_budgets_and_default_orchestration(monkeypatch, tokens, batch, expected_tokens, expected_batch):
    # Do not load real provider credentials or production settings during tests.
    import dotenv
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *args, **kwargs: None)
    for key in ('HARNESS_MAX_OUTPUT_TOKENS', 'AGENT_PLAN_BATCH_SIZE', 'AGENT_ORCHESTRATION'):
        monkeypatch.delenv(key, raising=False)
    if tokens is not None:
        monkeypatch.setenv('HARNESS_MAX_OUTPUT_TOKENS', tokens)
    if batch is not None:
        monkeypatch.setenv('AGENT_PLAN_BATCH_SIZE', batch)
    settings = Settings.from_env()
    assert settings.harness_max_output_tokens == expected_tokens
    assert settings.agent_plan_batch_size == expected_batch
    assert settings.agent_orchestration == 'sequential_v1'
    monkeypatch.setenv('AGENT_ORCHESTRATION', 'supervisor_v1')
    assert Settings.from_env().agent_orchestration == 'supervisor_v1'


class ProposalWire:
    def __init__(self, unexpected_tool=False):
        self.requests = []
        self.unexpected_tool = unexpected_tool

    async def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert str(request.url) == 'https://fixture.invalid/v1/chat/completions'
        assert body['thinking'] == {'type': 'enabled'}
        assert body['reasoning_effort'] == 'high'
        assert body['response_format'] == {'type': 'json_object'}
        assert body['max_tokens'] == 32768
        contract = json.loads(next(message['content'] for message in body['messages']
                                   if message['role'] == 'user'))
        proposal = contract['task'] in ('agent_plan', 'agent_author_cpu_spec')
        if proposal:
            assert not body.get('tools')
            assert len(body['messages']) == 2
            assert 'No tools are available' in body['messages'][0]['content']
            assert 'call cpu_schedule_v1' not in body['messages'][0]['content']
        else:
            assert {tool['function']['name'] for tool in body['tools']} == TOOLS
        tool_messages = [message for message in body['messages'] if message['role'] == 'tool']
        if self.unexpected_tool or (not proposal and not tool_messages):
            value = completion(calls=[{
                'id': 'cpu-check', 'type': 'function', 'function': {
                    'name': 'mcp__teaching__cpu_schedule_v1',
                    'arguments': json.dumps({'input': CPU_INPUT}),
                },
            }])
            value['choices'][0]['message']['reasoning_content'] = 'Only this role needs a calculation.'
        else:
            if tool_messages:
                assert len(tool_messages) == 1
                assert json.loads(tool_messages[0]['content'])['makespan'] == 8
                assert next(message for message in body['messages'] if message['role'] == 'assistant')[
                    'reasoning_content'] == 'Only this role needs a calculation.'
            value = completion('{"ok":true}')
            value['choices'][0]['message']['reasoning_content'] = 'Return the bounded result.'
        return httpx.Response(200, json=value)


def proposal_agent(tmp_path, wire):
    agent = make_agent(tmp_path, wire)
    agent.settings.agent_orchestration = 'supervisor_v1'
    agent.settings.harness_reasoning_effort = 'high'
    agent.settings.harness_response_format = 'json_object'
    agent.settings.harness_max_output_tokens = 32768
    return agent


@integration
def test_real_runtime_proposals_have_no_tools_and_checkers_keep_separate_tools(tmp_path, monkeypatch):
    wire = ProposalWire()
    agent = proposal_agent(tmp_path, wire)
    mcp_sessions, reports = [], []
    original = runner.ACPClient

    class ObservedClient(original):
        async def new_session(self, work_dir, mcp):
            mcp_sessions.append(mcp)
            return await super().new_session(work_dir, mcp)

    monkeypatch.setattr(runner, 'ACPClient', ObservedClient)

    async def exercise():
        try:
            for task, phase in (('agent_plan', 'plan'), ('agent_author_cpu_spec', 'author'),
                                ('agent_solve', 'solve'), ('agent_review', 'review')):
                report = {}
                reports.append(report)
                assert await runner.run_harness_phase(agent, messages(task), phase, report) == {'ok': True}
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 6
    assert len({report['session_id'] for report in reports}) == 4
    for index, report in enumerate(reports):
        proposal = index < 2
        assert report['status'] == 'completed'
        assert report['proposal_only'] is proposal
        assert report['tools'] == ([] if proposal else ['get_reference_chunks', 'cpu_schedule_v1'])
        assert bool(mcp_sessions[index]) is not proposal
        assert len(report['model_calls']) == (1 if proposal else 2)
        assert report['max_output_tokens'] == 32768
        audit = Path(report['tool_audit_path'])
        if proposal:
            assert not audit.exists()
        else:
            records = [json.loads(line) for line in audit.read_text().splitlines()]
            assert len(records) == 1 and records[0]['result']['makespan'] == 8


@integration
def test_real_runtime_unrequested_tool_call_cannot_escape_proposal_role(tmp_path):
    wire = ProposalWire(unexpected_tool=True)
    agent = proposal_agent(tmp_path, wire)
    report = {}

    async def exercise():
        try:
            with pytest.raises(ProviderError):
                await runner.run_harness_phase(agent, messages('agent_author_cpu_spec'), 'author', report)
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 1
    assert report['status'] == 'failed'
    assert report['model_calls'][0]['status'] == 'failed'
    assert not Path(report['tool_audit_path']).exists()
