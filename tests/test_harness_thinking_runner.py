"""Strict final output and installed-runtime thinking regression coverage.

Runtime tests use the pinned local Harness with a mock upstream transport. They
exercise ACP, the HTTP relay, SSE, and MCP without any external model request.
"""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app import harness_runner as runner
from app.providers import ProviderError, ProviderOutputError
from test_harness_runner import CPU_INPUT, TOOLS, completion, integration, make_agent


@pytest.mark.parametrize('content', [
    'Here is the result:\n{"ok":true}',
    '```json\n{"ok":true}\n```',
    '{"ok":true}\nExplanation follows.',
    '{"ok":true,"ok":false}',
    '{"answer":{"value":8,"value":9}}',
    '{"answer":NaN}',
    '{"answer":Infinity}',
    '{"answer":-Infinity}',
    '[]',
    'null',
    '"answer"',
    '{"ok":',
    '{"ok":true} {"ok":false}',
])
def test_strict_phase_output_rejects_ambiguous_or_non_json_objects(content):
    with pytest.raises(ProviderOutputError):
        runner.strict_text_object(completion(content))


@pytest.mark.parametrize('mutation', [
    'truncated', 'refusal', 'multiple_choices', 'tool_call',
])
def test_parseable_json_is_not_enough_without_a_completed_final_response(mutation):
    result = completion('{"ok":true}')
    if mutation == 'truncated':
        result['choices'][0]['finish_reason'] = 'length'
    elif mutation == 'refusal':
        result['choices'][0]['message']['refusal'] = 'Cannot provide the result.'
    elif mutation == 'multiple_choices':
        result['choices'].append(copy.deepcopy(result['choices'][0]))
    else:
        result['choices'][0]['message']['tool_calls'] = [{
            'id': 'unfinished', 'type': 'function', 'function': {
                'name': 'mcp__teaching__get_reference_chunks', 'arguments': '{}'}}]
    with pytest.raises(ProviderOutputError):
        runner.strict_text_object(result)


def test_strict_phase_output_accepts_only_final_content_not_reasoning_text():
    expected = {'answer': {'makespan': 8, 'label': '总时间'}, 'ok': True}
    result = completion(' \n' + json.dumps(expected, ensure_ascii=False) + '\n ')
    result['choices'][0]['message']['reasoning_content'] = (
        'Non-JSON private reasoning fixture: {"answer":"not the final answer"}')
    assert runner.strict_text_object(result) == expected


def phase_messages(phase):
    return [
        {'role': 'system', 'content': 'Return only the requested JSON object.'},
        {'role': 'user', 'content': json.dumps({
            'task': 'agent_' + phase,
            'marker': phase + '_isolated_fixture',
            'schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                       'required': ['ok'], 'additionalProperties': False},
        })},
    ]


class ThinkingToolWire:
    """Require reasoning to survive the real runtime's tool-call round trip."""

    def __init__(self, bad_final=None):
        self.requests = []
        self.bad_final = bad_final
        self.reasoning = {}

    async def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert str(request.url) == 'https://fixture.invalid/v1/chat/completions'
        assert body['thinking'] == {'type': 'enabled'}
        assert body['reasoning_effort'] == 'high'
        assert body['response_format'] == {'type': 'json_object'}
        assert body['max_tokens'] == 2048
        assert body['stream'] is False
        assert {tool['function']['name'] for tool in body['tools']} == TOOLS
        user_message = next(message['content'] for message in body['messages']
                            if message['role'] == 'user')
        contract = json.loads(user_message)
        phase = contract['task'].removeprefix('agent_')
        tool_messages = [message for message in body['messages'] if message['role'] == 'tool']
        if not tool_messages:
            assert len(body['messages']) == 2
            assert not any(message['role'] == 'assistant' for message in body['messages'])
            assert all(other + '_isolated_fixture' not in json.dumps(body['messages'])
                       for other in ('author', 'solve') if other != phase)
            reasoning = '  ' + phase + '_private_reasoning\n中文 fixture <>& "quoted"  '
            self.reasoning[phase] = reasoning
            value = completion(calls=[{
                'id': phase + '-cpu', 'type': 'function', 'function': {
                    'name': 'mcp__teaching__cpu_schedule_v1',
                    'arguments': json.dumps({'input': CPU_INPUT}),
                },
            }])
            value['choices'][0]['message']['reasoning_content'] = reasoning
            return httpx.Response(200, json=value)

        assistant_messages = [message for message in body['messages'] if message['role'] == 'assistant']
        assert len(assistant_messages) == 1
        assert assistant_messages[0]['reasoning_content'] == self.reasoning[phase]
        assert assistant_messages[0]['tool_calls'][0]['id'] == phase + '-cpu'
        assert len(tool_messages) == 1
        assert tool_messages[0]['tool_call_id'] == phase + '-cpu'
        assert json.loads(tool_messages[0]['content'])['makespan'] == 8
        for other, reasoning in self.reasoning.items():
            if other != phase:
                assert reasoning not in [message.get('reasoning_content') for message in body['messages']]
        final = completion('{"ok":true}')
        final['choices'][0]['message']['reasoning_content'] = phase + ' final reasoning fixture'
        if self.bad_final == 'prose':
            final['choices'][0]['message']['content'] = 'Verified result:\n{"ok":true}'
        elif self.bad_final == 'truncated':
            # Even syntactically complete JSON is untrusted when the provider
            # reports length exhaustion; a missing last field may be invisible.
            final['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200, json=final)


def thinking_agent(tmp_path, wire):
    agent = make_agent(tmp_path, wire)
    agent.settings.harness_reasoning_effort = 'high'
    agent.settings.harness_response_format = 'json_object'
    agent.settings.harness_max_output_tokens = 2048
    return agent


@integration
def test_high_thinking_and_json_mode_survive_tools_without_cross_role_context(tmp_path):
    wire = ThinkingToolWire()
    agent = thinking_agent(tmp_path, wire)
    reports = []

    async def exercise():
        try:
            for phase in ('author', 'solve'):
                report = {}
                reports.append(report)
                result = await runner.run_harness_phase(agent, phase_messages(phase), phase, report)
                assert result == {'ok': True}
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 4
    assert len({report['session_id'] for report in reports}) == 2
    for report in reports:
        assert report['status'] == 'completed'
        assert report['reasoning'] == 'high'
        assert report['response_format'] == 'json_object'
        assert report['max_output_tokens'] == 2048
        assert len(report['model_calls']) == 2
        assert all(call['status'] == 'succeeded' for call in report['model_calls'])
        audits = [json.loads(line) for line in Path(report['tool_audit_path']).read_text().splitlines()]
        assert len(audits) == 1 and audits[0]['result']['makespan'] == 8
        assert 'mock-private-model-key' not in json.dumps(report)


@integration
@pytest.mark.parametrize('bad_final', ['prose', 'truncated'])
def test_high_thinking_bad_final_is_rejected_without_automatic_retry(tmp_path, bad_final):
    wire = ThinkingToolWire(bad_final)
    agent = thinking_agent(tmp_path, wire)
    report = {}

    async def exercise():
        try:
            with pytest.raises(ProviderError):
                await runner.run_harness_phase(agent, phase_messages('author'), 'author', report)
        finally:
            await agent.pipeline.providers.close()

    asyncio.run(exercise())
    assert report['status'] == 'failed'
    assert len(wire.requests) == len(agent.store.calls_for_job(agent.job_id)) == 2
    assert len(report['model_calls']) == 2
    assert report['model_calls'][0]['status'] == 'succeeded'
    assert report['model_calls'][1]['status'] == ('failed' if bad_final == 'truncated' else 'succeeded')
