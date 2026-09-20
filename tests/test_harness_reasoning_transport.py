"""Host-owned thinking/JSON contracts using HTTP fixtures and the real ledger."""
import json

import pytest

from app.harness_profile import profile_patch
from test_harness_relay import REQUEST, chat_response, relay_case  # noqa: F401


TOOL_NAME = 'mcp__teaching__cpu_schedule_v1'
TOOL = {'type': 'function', 'function': {'name': TOOL_NAME, 'parameters': {'type': 'object'}}}
TOOL_CALL = {'id': 'fixture-cpu-1', 'type': 'function',
             'function': {'name': TOOL_NAME, 'arguments': '{"input":{}}'}}


def configure(rig, *, reasoning='high', response_format='json_object'):
    rig.settings.harness_reasoning_effort = reasoning
    rig.settings.harness_response_format = response_format
    return rig


@pytest.mark.parametrize('effort,thinking', [('off', 'disabled'), ('high', 'enabled')])
def test_profile_matches_host_reasoning_policy(tmp_path, effort, thinking):
    patch = profile_patch('fixture-flash', 'http://127.0.0.1:1234/v1', 8192, tmp_path,
                          reasoning_effort=effort)
    llm = next(item['config'] for item in patch if item['id'] == 'llm-deepseek')
    assert llm['thinking'] == thinking
    assert llm['reasoningEffort'] == effort
    assert llm['maxTokens'] == 8192
    assert llm['models'] == [{'id': 'fixture-flash', 'contextWindow': 131072}]


@pytest.mark.parametrize('effort', ['low', 'max', None, True])
def test_profile_rejects_unsupported_reasoning_conditions(tmp_path, effort):
    with pytest.raises(ValueError, match='reasoning_effort'):
        profile_patch('fixture-flash', 'http://127.0.0.1:1234/v1', 4096, tmp_path,
                      reasoning_effort=effort)


@pytest.mark.parametrize('field,requested,expected', [
    ('max_tokens', 99999, 8192), ('max_tokens', 1024, 1024),
    ('max_completion_tokens', 99999, 8192),
])
def test_host_injects_high_and_json_even_when_runtime_omits_them(relay_case, field, requested, expected):
    rig = configure(relay_case(max_output_tokens=8192))
    assert rig.post(REQUEST | {field: requested}).status_code == 200
    sent = rig.wire.calls[0]['body']
    assert sent['thinking'] == {'type': 'enabled'}
    assert sent['reasoning_effort'] == 'high'
    assert sent['response_format'] == {'type': 'json_object'}
    assert sent[field] == expected
    assert ('max_tokens' in sent) != ('max_completion_tokens' in sent)
    assert sent['stream'] is False
    assert rig.app.state.audit[0]['request'] == sent
    assert len(rig.ledger()) == 1


def test_off_default_is_explicit_without_adding_reasoning_or_json(relay_case):
    rig = relay_case()
    assert rig.post().status_code == 200
    sent = rig.wire.calls[0]['body']
    assert sent['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in sent
    assert 'response_format' not in sent


@pytest.mark.parametrize('changes', [
    {'thinking': {'type': 'disabled'}}, {'thinking': {'type': 'enabled', 'budget_tokens': 32}},
    {'reasoning_effort': 'off'}, {'reasoning_effort': 'max'}, {'reasoning_effort': True},
    {'response_format': {'type': 'text'}},
    {'response_format': {'type': 'json_schema', 'json_schema': {'type': 'object'}}},
    {'response_format': {'type': 'json_object', 'extra': True}},
])
def test_runtime_cannot_change_host_high_json_condition(relay_case, changes):
    rig = configure(relay_case())
    assert rig.post(REQUEST | changes).status_code == 400
    assert not rig.wire.calls and not rig.ledger()
    assert rig.app.state.request_count == 0 and not rig.app.state.audit


@pytest.mark.parametrize('extra', [
    {'thinking': {'type': 'disabled'}}, {'reasoning_effort': 'off'},
    {'reasoning_effort': 'max'}, {'response_format': {'type': 'text'}},
    # ApiProviders reserves response_format even when it matches the host.
    {'response_format': {'type': 'json_object'}},
    {'max_tokens': 8193}, {'max_completion_tokens': 8192},
])
def test_provider_extras_cannot_override_condition_or_token_cap(relay_case, extra):
    rig = configure(relay_case(max_output_tokens=8192, extra=extra))
    assert rig.post().status_code == 400
    assert not rig.wire.calls and not rig.ledger() and rig.app.state.request_count == 0


def test_matching_high_provider_extras_do_not_change_wire_provenance(relay_case):
    rig = configure(relay_case(extra={'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}))
    assert rig.post(REQUEST | {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high',
                              'response_format': {'type': 'json_object'}}).status_code == 200
    assert rig.app.state.audit[0]['request'] == rig.wire.calls[0]['body']


@pytest.mark.parametrize('changes', [
    {'thinking': {'type': 'enabled'}}, {'reasoning_effort': 'high'},
    {'reasoning_effort': 'off'}, {'response_format': {'type': 'json_object'}},
])
def test_explicit_off_text_host_cannot_be_upgraded_by_runtime(relay_case, changes):
    rig = configure(relay_case(), reasoning='off', response_format='text')
    assert rig.post(REQUEST | changes).status_code == 400
    assert not rig.wire.calls and not rig.ledger()


def test_reasoning_survives_sse_and_tool_continuation_with_usage(relay_case):
    rationale = 'Fixture reasoning retained through the tool boundary.'
    upstream = chat_response(choices=[{'index': 0, 'message': {
        'role': 'assistant', 'content': None, 'reasoning_content': rationale,
        'tool_calls': [TOOL_CALL]}, 'finish_reason': 'tool_calls'}],
        usage={'prompt_tokens': 8, 'completion_tokens': 54, 'total_tokens': 62,
               'completion_tokens_details': {'reasoning_tokens': 50}})
    rig = configure(relay_case(response=upstream, allowed_tool_names={TOOL_NAME}))
    response = rig.post(REQUEST | {'stream': True, 'tools': [TOOL]})
    assert response.status_code == 200
    records = [json.loads(line[6:]) for line in response.text.splitlines()
               if line.startswith('data: ') and line != 'data: [DONE]']
    deltas = [choice['delta'] for record in records for choice in record['choices']]
    assert {'reasoning_content': rationale} in deltas
    assert records[-1]['usage']['completion_tokens_details']['reasoning_tokens'] == 50
    assert rig.ledger()[0]['usage']['completion_tokens_details']['reasoning_tokens'] == 50
    history = [*REQUEST['messages'], upstream['choices'][0]['message'],
               {'role': 'tool', 'tool_call_id': TOOL_CALL['id'], 'content': '{"ok":true}'}]
    rig.wire.response = chat_response(choices=[{'index': 0, 'message': {
        'role': 'assistant', 'content': '{"ok":true}', 'reasoning_content': 'Checked the result.'},
        'finish_reason': 'stop'}])
    assert rig.post(REQUEST | {'messages': history, 'tools': [TOOL]}).status_code == 200
    assert rig.wire.calls[1]['body']['messages'][1]['reasoning_content'] == rationale
    assert rig.wire.calls[1]['body']['messages'] == history
    assert len(rig.wire.calls) == len(rig.ledger()) == 2


@pytest.mark.parametrize('assistant', [
    {'role': 'assistant', 'content': None, 'tool_calls': [TOOL_CALL]},
    {'role': 'assistant', 'content': 'Prior answer.'},
    {'role': 'assistant', 'content': 'Prior answer.', 'reasoning_content': None},
])
def test_high_tool_history_missing_reasoning_fails_without_call(relay_case, assistant):
    rig = configure(relay_case())
    assert rig.post(REQUEST | {'messages': [*REQUEST['messages'], assistant],
                              'tools': [TOOL]}).status_code == 400
    assert not rig.wire.calls and not rig.ledger() and rig.app.state.request_count == 0


def test_explicit_empty_reasoning_history_is_not_invented_or_dropped(relay_case):
    rig = configure(relay_case())
    assistant = {'role': 'assistant', 'content': 'Prior answer.', 'reasoning_content': ''}
    assert rig.post(REQUEST | {'messages': [*REQUEST['messages'], assistant],
                              'tools': [TOOL]}).status_code == 200
    assert rig.wire.calls[0]['body']['messages'][-1] == assistant


@pytest.mark.parametrize('tools', [None, []])
def test_no_tool_reasoning_request_does_not_require_history_reasoning(relay_case, tools):
    rig = configure(relay_case())
    body = REQUEST | {'messages': [*REQUEST['messages'], {'role': 'assistant', 'content': 'Prior answer.'}]}
    if tools is not None:
        body['tools'] = tools
    assert rig.post(body).status_code == 200


@pytest.mark.parametrize('choice', ['required', {'type': 'function', 'function': {'name': TOOL_NAME}}])
def test_thinking_does_not_send_unsupported_forced_tool_choice(relay_case, choice):
    rig = configure(relay_case())
    assert rig.post(REQUEST | {'tools': [TOOL], 'tool_choice': choice}).status_code == 400
    assert not rig.wire.calls and not rig.ledger()


@pytest.mark.parametrize('choice', ['auto', 'none'])
def test_thinking_allows_supported_tool_choice(relay_case, choice):
    rig = configure(relay_case())
    assert rig.post(REQUEST | {'tools': [TOOL], 'tool_choice': choice}).status_code == 200


@pytest.mark.parametrize('finish_reason,content', [('length', '{"partial":'), ('stop', None), ('stop', '')])
def test_reasoning_without_complete_final_output_remains_failed(relay_case, finish_reason, content):
    upstream = chat_response(choices=[{'index': 0, 'message': {
        'role': 'assistant', 'content': content, 'reasoning_content': 'Fixture thinking.'},
        'finish_reason': finish_reason}])
    rig = configure(relay_case(response=upstream))
    assert rig.post().status_code == 502
    assert len(rig.wire.calls) == 1 and rig.ledger()[0]['status'] == 'failed'
    assert rig.app.state.audit[0]['status'] == 'failed'
    assert rig.app.state.audit[0]['raw_response'] == upstream
