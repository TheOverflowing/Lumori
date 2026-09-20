"""V2 role capabilities through pinned Harness and a mock upstream only."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import harness_runner as runner
from app.config import Settings
from app.providers import ProviderError
from test_harness_proposal_mode import messages, proposal_agent
from test_harness_runner import CPU_INPUT, TOOLS, completion, integration


ROLE_CASES = [
    ('agent_plan', 'plan', True),
    ('agent_author_cpu_spec', 'author', True),
    ('agent_author', 'author', True),
    ('agent_compose_cpu_narrative', 'compose', True),
    ('agent_compose_cpu_narrative', 'compose_schema_repair', True),
    ('agent_solve', 'solve', False),
    ('agent_review', 'review', False),
    ('agent_review', 'review_evidence_repair', False),
]


@pytest.mark.parametrize('task,phase,expected', ROLE_CASES)
def test_v2_role_capabilities_do_not_change_during_schema_repair(task, phase, expected):
    request = messages(task)
    if phase.endswith('_schema_repair'):
        contract = json.loads(request[1]['content'])
        contract['schema_repair'] = {'mode': 'fresh_re_evaluation'}
        request[1]['content'] = json.dumps(contract)
    elif phase.endswith('_evidence_repair'):
        contract = json.loads(request[1]['content'])
        contract['evidence_repair'] = {'mode': 'fresh_candidate_audit'}
        contract['independent_solution'] = {'answerable': True, 'ambiguity_free': True, 'requires_calculation': True}
        request[1]['content'] = json.dumps(contract)
    assert runner.proposal_only(Settings(agent_orchestration='supervisor_v2'), request) is expected
    instructions = runner.phase_instructions(request, phase, proposal=expected)
    assert ('No tools are available' in instructions) is expected
    assert ('call cpu_schedule_v1' in instructions) is not expected
    assert 'JSON object alone' in instructions
    if phase == 'review_evidence_repair':
        assert 'This phase has NO tool_requests or tool_results field' in instructions


@pytest.mark.parametrize('task', ['agent_author', 'agent_compose_cpu_narrative'])
def test_new_tool_free_roles_are_explicitly_opted_in(task):
    for mode in ('sequential_v1', 'supervisor_v1'):
        assert runner.proposal_only(Settings(agent_orchestration=mode), messages(task)) is False


class FlexibleWire:
    def __init__(self, unexpected_tool=False):
        self.requests, self.unexpected_tool = [], unexpected_tool

    async def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert str(request.url) == 'https://fixture.invalid/v1/chat/completions'
        assert body['thinking'] == {'type': 'enabled'} and body['reasoning_effort'] == 'high'
        assert body['response_format'] == {'type': 'json_object'} and body['max_tokens'] == 32768
        contract = json.loads(next(message['content'] for message in body['messages'] if message['role'] == 'user'))
        proposal = contract['task'] not in ('agent_solve', 'agent_review')
        if proposal:
            assert not body.get('tools')
            assert len(body['messages']) == 2
            assert 'No tools are available' in body['messages'][0]['content']
            assert 'call cpu_schedule_v1' not in body['messages'][0]['content']
        else:
            assert {tool['function']['name'] for tool in body['tools']} == TOOLS
        tool_messages = [message for message in body['messages'] if message['role'] == 'tool']
        if self.unexpected_tool or not proposal and not tool_messages:
            result = completion(calls=[{'id': 'verify-cpu', 'type': 'function', 'function': {
                'name': 'mcp__teaching__cpu_schedule_v1', 'arguments': json.dumps({'input': CPU_INPUT})}}])
            result['choices'][0]['message']['reasoning_content'] = 'Fixture: calculation belongs only to this checker.'
        else:
            if tool_messages:
                assert len(tool_messages) == 1
                assert json.loads(tool_messages[0]['content'])['makespan'] == 8
                assistant = next(message for message in body['messages'] if message['role'] == 'assistant')
                assert assistant['reasoning_content'] == 'Fixture: calculation belongs only to this checker.'
            result = completion('{"ok":true}')
            result['choices'][0]['message']['reasoning_content'] = 'Fixture: return bounded JSON.'
        return httpx.Response(200, json=result)


@integration
def test_pinned_runtime_v2_proposals_and_composer_have_no_tools_but_checkers_keep_scoped_tools(tmp_path, monkeypatch):
    wire = FlexibleWire()
    agent = proposal_agent(tmp_path, wire)
    agent.settings.agent_orchestration = 'supervisor_v2'
    reports, mcp_sessions = [], []
    original = runner.ACPClient

    class ObservedClient(original):
        async def new_session(self, work_dir, mcp):
            mcp_sessions.append(mcp)
            return await super().new_session(work_dir, mcp)

    monkeypatch.setattr(runner, 'ACPClient', ObservedClient)

    async def exercise():
        try:
            for task, phase, _ in ROLE_CASES:
                report, request = {}, messages(task)
                contract = json.loads(request[1]['content'])
                contract['role_marker'] = phase + '_' + task
                if phase.endswith('_schema_repair'):
                    contract['schema_repair'] = {'mode': 'fresh_re_evaluation'}
                elif phase.endswith('_evidence_repair'):
                    contract['evidence_repair'] = {'mode': 'fresh_candidate_audit'}
                    contract['independent_solution'] = {'answerable': True, 'ambiguity_free': True,
                                                        'requires_calculation': True}
                request[1]['content'] = json.dumps(contract)
                assert await runner.run_harness_phase(agent, request, phase, report) == {'ok': True}
                reports.append(report)
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert len(reports) == len({report['session_id'] for report in reports}) == 8
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 11
    for (task, phase, proposal), report, mcp in zip(ROLE_CASES, reports, mcp_sessions):
        assert report['status'] == 'completed' and report['proposal_only'] is proposal
        assert report['phase'] == phase and report['max_output_tokens'] == 32768
        assert report['tools'] == ([] if proposal else ['get_reference_chunks', 'cpu_schedule_v1'])
        assert bool(mcp) is not proposal
        assert len(report['model_calls']) == (1 if proposal else 2)
        messages_for_role = report['model_calls'][0]['request']['messages']
        assert len(messages_for_role) == 2
        contract = json.loads(messages_for_role[1]['content'])
        assert contract['role_marker'] == phase + '_' + task
        audit_path = Path(report['tool_audit_path'])
        if proposal:
            assert not audit_path.exists()
        else:
            audit = [json.loads(line) for line in audit_path.read_text().splitlines()]
            assert len(audit) == 1 and audit[0]['result']['makespan'] == 8


@integration
def test_pinned_runtime_composition_schema_repair_cannot_call_a_tool(tmp_path):
    wire = FlexibleWire(unexpected_tool=True)
    agent = proposal_agent(tmp_path, wire)
    agent.settings.agent_orchestration = 'supervisor_v2'
    report = {}
    request = messages('agent_compose_cpu_narrative')
    contract = json.loads(request[1]['content'])
    contract['schema_repair'] = {'mode': 'fresh_re_evaluation'}
    request[1]['content'] = json.dumps(contract)

    async def exercise():
        try:
            with pytest.raises(ProviderError):
                await runner.run_harness_phase(agent, request, 'compose_schema_repair', report)
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 1
    assert report['proposal_only'] is True and report['tools'] == []
    assert report['status'] == 'failed' and report['model_calls'][0]['status'] == 'failed'
    assert not Path(report['tool_audit_path']).exists()
