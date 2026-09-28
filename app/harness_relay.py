"""Ephemeral loopback chat relay for a credential-isolated harness subprocess.

Every accepted model call goes through the original ApiProviders instance and
job ledger. This is a bounded transport adapter, not another provider or retry
layer. Model output and harness messages remain untrusted data.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import inspect
import json
import math
import secrets
import socket
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .providers import ProviderOutputError, safe_usage
from .task_control import JobCancelled


MAX_BODY_BYTES = 300_000
MAX_BODY_CHARS = 100_000
MAX_MESSAGES = 200
MAX_RESPONSE_CHARS = 300_000
ALLOWED_FIELDS = {
    'model', 'messages', 'tools', 'tool_choice', 'parallel_tool_calls', 'n',
    'max_tokens', 'max_completion_tokens', 'stream', 'stream_options',
    'temperature', 'top_p', 'stop', 'seed', 'frequency_penalty', 'presence_penalty',
    'response_format', 'thinking', 'reasoning_effort',
}
SECRET_FIELDS = {'authorization', 'proxy-authorization', 'api_key', 'api-key',
                 'x-api-key', 'access_token', 'refresh_token', 'token', 'cookie',
                 'set-cookie', 'headers'}


class RelayInputError(ValueError):
    pass


def _error(status, code):
    # Deliberately never include upstream error text, validation input, or headers.
    messages = {
        'unauthorized': 'A valid relay Bearer token is required.',
        'cookie_not_allowed': 'Cookies and alternative API credentials are not accepted.',
        'invalid_request': 'The relay request does not match the permitted chat contract.',
        'request_too_large': 'The relay request exceeds the local size limit.',
        'request_limit': 'The relay request limit has been reached. No automatic retry is performed.',
        'scope_denied': 'This job is no longer authorized to call the model.',
        'task_cancelled': 'This job was cancelled. No further model requests will be sent.',
        'provider_failed': 'The model request failed or returned an invalid response. No automatic retry is performed.',
    }
    return JSONResponse(status_code=status, content={'error': {'type': 'harness_relay_error',
                        'code': code, 'message': messages[code]}})


def _clean(value, secret_values):
    """Filter credentials even if an upstream success body accidentally echoes them."""
    if isinstance(value, dict):
        return {_clean(key, secret_values): _clean(item, secret_values) for key, item in value.items()
                if str(key).casefold() not in SECRET_FIELDS}
    if isinstance(value, list):
        return [_clean(item, secret_values) for item in value]
    if isinstance(value, str):
        for secret in secret_values:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value
    return value


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value <= 2**63 - 1


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _function_call(value):
    if not isinstance(value, dict) or set(value) - {'name', 'arguments'}:
        raise RelayInputError()
    if not isinstance(value.get('name'), str) or not 1 <= len(value['name']) <= 128:
        raise RelayInputError()
    if not isinstance(value.get('arguments'), str):
        raise RelayInputError()


def _tool_calls(value, *, allow_index=False):
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise RelayInputError()
    ids = []
    allowed = {'id', 'type', 'function'} | ({'index'} if allow_index else set())
    for position, call in enumerate(value):
        if not isinstance(call, dict) or set(call) - allowed or call.get('type') != 'function':
            raise RelayInputError('invalid_tool_call_fields')
        if 'index' in call and (type(call['index']) is not int or call['index'] != position):
            raise RelayInputError('invalid_tool_call_index')
        if not isinstance(call.get('id'), str) or not 1 <= len(call['id']) <= 256:
            raise RelayInputError()
        ids.append(call['id'])
        _function_call(call.get('function'))
    if len(ids) != len(set(ids)):
        raise RelayInputError()


def _messages(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MESSAGES:
        raise RelayInputError()
    for message in value:
        if not isinstance(message, dict) or set(message) - {
            'role', 'content', 'name', 'tool_calls', 'tool_call_id', 'reasoning_content', 'refusal',
        }:
            raise RelayInputError()
        if message.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool'):
            raise RelayInputError()
        content = message.get('content')
        if isinstance(content, list):
            if not content or any(not isinstance(part, dict) or set(part) != {'type', 'text'}
                                  or part['type'] != 'text' or not isinstance(part['text'], str)
                                  for part in content):
                raise RelayInputError()
        elif content is not None and not isinstance(content, str):
            raise RelayInputError()
        for key in ('name', 'tool_call_id', 'reasoning_content', 'refusal'):
            if key in message and message[key] is not None and not isinstance(message[key], str):
                raise RelayInputError()
        if message['role'] == 'tool' and not message.get('tool_call_id'):
            raise RelayInputError()
        if message.get('tool_calls') is not None:
            if message['role'] != 'assistant':
                raise RelayInputError()
            _tool_calls(message['tool_calls'])
        if content is None and not message.get('tool_calls'):
            raise RelayInputError()


def _payload(body, settings, max_output_tokens, allowed_tool_names=None):
    if not isinstance(body, dict) or set(body) - ALLOWED_FIELDS:
        raise RelayInputError()
    if 'model' in body and body['model'] != settings.text.model:
        raise RelayInputError()
    # Provider extras are merged last. They must not replace the reviewed tool
    # surface after request validation or make its recorded provenance untrue.
    if {'tools', 'tool_choice', 'parallel_tool_calls', 'response_format'} & settings.text.extra.keys():
        raise RelayInputError()
    _messages(body.get('messages'))
    if allowed_tool_names is not None:
        for message in body['messages']:
            if any(call['function']['name'] not in allowed_tool_names for call in message.get('tool_calls') or []):
                raise RelayInputError()
    # The host chooses the experimental condition. Neither the runtime nor
    # provider extras may silently change it, including when a runtime omits
    # these wire fields. The output cap includes reasoning and final output.
    reasoning_effort = getattr(settings, 'harness_reasoning_effort', 'off')
    response_mode = getattr(settings, 'harness_response_format', 'text')
    if reasoning_effort not in ('off', 'high') or response_mode not in ('text', 'json_object'):
        raise RelayInputError()
    thinking = {'type': 'enabled' if reasoning_effort == 'high' else 'disabled'}
    for options in (body, settings.text.extra):
        if 'thinking' in options and options['thinking'] != thinking:
            raise RelayInputError()
        if 'reasoning_effort' in options and not (
                reasoning_effort == 'high' and options['reasoning_effort'] == 'high'):
            raise RelayInputError()
    if 'n' in body and (type(body['n']) is not int or body['n'] != 1):
        raise RelayInputError()
    for key in ('stream', 'parallel_tool_calls'):
        if key in body and type(body[key]) is not bool:
            raise RelayInputError()
    if 'stream_options' in body:
        options = body['stream_options']
        if not isinstance(options, dict) or set(options) - {'include_usage'}:
            raise RelayInputError()
        if 'include_usage' in options and type(options['include_usage']) is not bool:
            raise RelayInputError()
    if 'max_tokens' in body and 'max_completion_tokens' in body:
        raise RelayInputError()
    token_field = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
    requested = body.get(token_field, max_output_tokens)
    if not _integer(requested):
        raise RelayInputError()
    cap = min(requested, max_output_tokens)
    for key, bounds in {'temperature': (0, 2), 'top_p': (0, 1),
                        'frequency_penalty': (-2, 2), 'presence_penalty': (-2, 2)}.items():
        if key in body and not _number(body[key], *bounds):
            raise RelayInputError()
    if 'seed' in body and (type(body['seed']) is not int or not -(2**63) <= body['seed'] < 2**63):
        raise RelayInputError()
    if 'stop' in body:
        stops = [body['stop']] if isinstance(body['stop'], str) else body['stop']
        if not isinstance(stops, list) or not 1 <= len(stops) <= 4 or any(not isinstance(s, str) or not s for s in stops):
            raise RelayInputError()
    if 'response_format' in body:
        if body['response_format'] != {'type': response_mode}:
            raise RelayInputError()
    names = set()
    if 'tools' in body:
        if not isinstance(body['tools'], list) or len(body['tools']) > 64:
            raise RelayInputError()
        for tool in body['tools']:
            if not isinstance(tool, dict) or set(tool) != {'type', 'function'} or tool['type'] != 'function':
                raise RelayInputError()
            function = tool['function']
            if not isinstance(function, dict) or set(function) - {'name', 'description', 'parameters', 'strict'}:
                raise RelayInputError()
            name = function.get('name')
            if not isinstance(name, str) or not 1 <= len(name) <= 128 or name in names:
                raise RelayInputError()
            if allowed_tool_names is not None and name not in allowed_tool_names:
                raise RelayInputError()
            names.add(name)
            if 'description' in function and not isinstance(function['description'], str):
                raise RelayInputError()
            if 'parameters' in function and not isinstance(function['parameters'], dict):
                raise RelayInputError()
            if 'strict' in function and type(function['strict']) is not bool:
                raise RelayInputError()
    if reasoning_effort == 'high' and names:
        # DeepSeek requires thinking history on every assistant message when
        # continuing with tools. Fail locally if the runtime loses that data;
        # inventing reasoning or paying for a known-invalid continuation is
        # not a repair. An explicitly empty string remains a valid passback.
        if any(message['role'] == 'assistant' and not isinstance(message.get('reasoning_content'), str)
               for message in body['messages']):
            raise RelayInputError()
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if reasoning_effort == 'high' and choice not in ('auto', 'none'):
            raise RelayInputError()
        if isinstance(choice, str):
            if choice not in ('auto', 'none', 'required') or choice == 'required' and not names:
                raise RelayInputError()
        elif not (isinstance(choice, dict) and set(choice) == {'type', 'function'}
                  and choice['type'] == 'function' and isinstance(choice['function'], dict)
                  and set(choice['function']) == {'name'} and choice['function']['name'] in names):
            raise RelayInputError()
    # ApiProviders merges configured extras after payload. Do not let an extra
    # replace this request's token cap or inject a second token-limit parameter.
    for field in ('max_tokens', 'max_completion_tokens'):
        if field in settings.text.extra:
            value = settings.text.extra[field]
            if field != token_field or not _integer(value) or value > cap:
                raise RelayInputError()
    payload = {key: value for key, value in body.items()
               if key not in ('model', 'stream_options', 'stream', 'max_tokens', 'max_completion_tokens')}
    payload.update(stream=False, n=1, thinking=thinking, **{token_field: cap})
    if reasoning_effort == 'high':
        payload['reasoning_effort'] = 'high'
    if response_mode == 'json_object':
        payload['response_format'] = {'type': 'json_object'}
    return payload


def _completion(data, allowed_tool_names=None):
    """Validate inside ApiProviders so malformed successes remain failed calls."""
    try:
        if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False, allow_nan=False)) > MAX_RESPONSE_CHARS:
            raise ValueError()
        choices = data['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError()
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get('index', 0) != 0 or type(choice.get('index', 0)) is not int:
            raise ValueError()
        # A truncated/filter-stopped tool call must not become a successful phase.
        if choice.get('finish_reason') not in ('stop', 'tool_calls'):
            raise ValueError()
        message = choice['message']
        if not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant':
            raise ValueError()
        for key in ('content', 'reasoning_content', 'refusal'):
            if message.get(key) is not None and not isinstance(message[key], str):
                raise ValueError()
        tool_calls = message.get('tool_calls')
        if tool_calls:
            # DeepSeek's observed nonstream response includes the streaming
            # index extension. Accept only its exact zero-based list position.
            _tool_calls(tool_calls, allow_index=True)
            if allowed_tool_names is not None and any(
                    call['function']['name'] not in allowed_tool_names for call in tool_calls):
                raise ValueError()
        if choice['finish_reason'] == 'tool_calls' and not tool_calls:
            raise ValueError()
        if not tool_calls and not (isinstance(message.get('content'), str) and message['content'].strip()):
            raise ValueError()
        if 'usage' in data and not isinstance(data['usage'], dict):
            raise ValueError()
        # Only scalar well-formed metadata is used when constructing SSE chunks.
        if 'id' in data and (not isinstance(data['id'], str) or len(data['id']) > 256):
            raise ValueError()
        if 'model' in data and (not isinstance(data['model'], str) or len(data['model']) > 256):
            raise ValueError()
        if 'created' in data and not _integer(data['created'], minimum=0):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        error = ProviderOutputError('Invalid harness chat completion.')
        error.validation_code = str(exc) if isinstance(exc, RelayInputError) and str(exc) in {
            'invalid_tool_call_fields', 'invalid_tool_call_index'} else 'invalid_chat_completion'
        raise error from None
    if tool_calls:
        # Keep the extension out of nonstream results and subsequent message
        # history. SSE builds its own validated indices from this canonical list.
        message = message | {'tool_calls': [{key: value for key, value in call.items() if key != 'index'}
                                           for call in tool_calls]}
        data = data | {'choices': [choice | {'message': message}]}
    return data


def _sse(data, model):
    base = {'id': data.get('id', 'chatcmpl-relay-' + uuid.uuid4().hex),
            'object': 'chat.completion.chunk', 'created': data.get('created', int(time.time())),
            'model': data.get('model', model)}

    def event(delta=None, finish_reason=None, usage=None):
        body = base | {'choices': [] if usage is not None else [
            {'index': 0, 'delta': delta or {}, 'finish_reason': finish_reason}]}
        if usage is not None:
            body['usage'] = usage
        return 'data: ' + json.dumps(body, ensure_ascii=False, allow_nan=False) + '\n\n'

    yield event({'role': 'assistant'})
    message = data['choices'][0]['message']
    for key in ('reasoning_content', 'content', 'refusal'):
        if message.get(key):
            yield event({key: message[key]})
    for index, call in enumerate(message.get('tool_calls') or []):
        yield event({'tool_calls': [dict(call, index=index)]})
    yield event(finish_reason=data['choices'][0]['finish_reason'])
    if 'usage' in data:
        yield event(usage=safe_usage(data['usage']))
    yield 'data: [DONE]\n\n'


def create_relay_app(providers, job_id, token, *, max_requests=6, max_output_tokens=4096,
                     before_call=None, allowed_tool_names=None, search_callback=None):
    """Return a relay app; before_call is a zero-argument sync/async scope guard.

    app.state.audit is available only to the parent process, never via a route.
    Invalid inputs/scope failures do not call providers. Accepted request attempts
    consume the local cap even when the provider or its durable budget rejects.
    """
    if not isinstance(token, str) or len(token) < 16 or not token.isascii():
        raise ValueError('Relay token must be an ASCII secret of at least 16 characters.')
    if not _integer(max_requests) or not _integer(max_output_tokens):
        raise ValueError('Relay limits must be positive integers.')
    if allowed_tool_names is not None:
        if not isinstance(allowed_tool_names, (set, frozenset, list, tuple)) or any(
                not isinstance(name, str) or not 1 <= len(name) <= 128 for name in allowed_tool_names):
            raise ValueError('Allowed tool names must be a collection of function names.')
        allowed_tool_names = frozenset(allowed_tool_names)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.audit = []
    app.state.request_count = 0
    lock = asyncio.Lock()

    def secret_values():
        keys = [getattr(providers.settings, capability).api_key for capability in
                ('text', 'embedding', 'rerank', 'speech', 'image', 'vision')]
        return [token, *keys]

    if search_callback is not None:
        @app.post('/v1/tools/search')
        async def search_tool(request: Request):
            authorization = request.headers.getlist('authorization')
            credential = authorization[0] if len(authorization) == 1 else ''
            scheme, separator, supplied = credential.partition(' ')
            if not (separator and scheme.casefold() == 'bearer' and
                    secrets.compare_digest(supplied.encode(), token.encode())):
                return _error(401, 'unauthorized')
            try:
                raw = await request.body()
                if len(raw) > 1024:
                    raise ValueError()
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {'query', 'language'}:
                    raise ValueError()
                if not isinstance(value['query'], str) or not isinstance(value['language'], str):
                    raise ValueError()
                result = search_callback(value['query'], value['language'])
                if inspect.isawaitable(result):
                    result = await result
                return JSONResponse(_clean({'candidates': result}, secret_values()),
                                    headers={'Cache-Control': 'no-store'})
            except (ValueError, TypeError, UnicodeError):
                return _error(400, 'invalid_request')
            except JobCancelled:
                return _error(409, 'task_cancelled')
            except Exception:
                return _error(502, 'provider_failed')

    @app.post('/v1/chat/completions')
    async def completion(request: Request):
        authorization = request.headers.getlist('authorization')
        credential = authorization[0] if len(authorization) == 1 else ''
        scheme, separator, supplied = credential.partition(' ')
        valid = bool(separator and scheme.casefold() == 'bearer')
        valid = secrets.compare_digest(supplied.encode(), token.encode()) and valid
        if not valid:
            return _error(401, 'unauthorized')
        if any(name in request.headers for name in ('cookie', 'x-api-key', 'api-key')):
            return _error(400, 'cookie_not_allowed')
        if request.query_params:
            return _error(400, 'invalid_request')
        try:
            raw, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return _error(413, 'request_too_large')
                raw.append(chunk)
            encoded = b''.join(raw).decode('utf-8')
            if len(encoded) > MAX_BODY_CHARS:
                return _error(413, 'request_too_large')
            body = json.loads(encoded, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            payload = _clean(_payload(body, providers.settings, max_output_tokens, allowed_tool_names), secret_values())
        except (ValueError, TypeError, KeyError, RecursionError):
            return _error(400, 'invalid_request')
        async with lock:
            if app.state.request_count >= max_requests:
                return _error(429, 'request_limit')
            if before_call is not None:
                try:
                    result = before_call()
                    if inspect.isawaitable(result):
                        result = await result
                    if result is False:
                        return _error(403, 'scope_denied')
                except JobCancelled:
                    return _error(409, 'task_cancelled')
                except Exception:
                    return _error(403, 'scope_denied')
            app.state.request_count += 1
            audit = {'number': app.state.request_count, 'status': 'in_flight',
                     'request': _clean(payload | {'model': providers.settings.text.model}
                                       | providers.settings.text.extra, secret_values()),
                     'requested_stream': body.get('stream', False)}
            app.state.audit.append(audit)
        try:
            def validate_response(value):
                try:
                    return _completion(value, allowed_tool_names)
                except ProviderOutputError as exc:
                    # Retain failed successful-HTTP responses for offline
                    # diagnostics; the runtime receives only the fixed error.
                    audit['raw_response'] = _clean(value, secret_values())
                    audit['validation_error'] = {'code': exc.validation_code,
                                                 'message': 'Invalid harness chat completion.'}
                    raise
            data = await providers.call('text', payload, job_id,
                                        validator=validate_response)
            data = _clean(data, secret_values())
            audit.update(status='succeeded', response=data)
        except JobCancelled:
            audit.update(status='cancelled', error_code='task_cancelled')
            return _error(409, 'task_cancelled')
        except asyncio.CancelledError:
            audit.update(status='cancelled', error_code='request_interrupted')
            raise
        except Exception:
            audit.update(status='failed', error_code='provider_failed')
            return _error(502, 'provider_failed')
        if body.get('stream', False):
            return StreamingResponse(_sse(data, providers.settings.text.model), media_type='text/event-stream',
                                     headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        return JSONResponse(data, headers={'Cache-Control': 'no-store'})

    return app


@asynccontextmanager
async def serve_relay(providers, job_id, token, *, max_requests=6, max_output_tokens=4096,
                      before_call=None, allowed_tool_names=None, search_callback=None):
    """Yield (OpenAI base_url including /v1, app); close server/socket on exit."""
    import uvicorn

    class RelayServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # This server lives inside the parent's event loop, not its process.
            yield

    app = create_relay_app(providers, job_id, token, max_requests=max_requests,
                           max_output_tokens=max_output_tokens, before_call=before_call,
                           allowed_tool_names=allowed_tool_names,
                           search_callback=search_callback)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    task = None
    server = None
    try:
        sock.bind(('127.0.0.1', 0))
        sock.listen(16)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = RelayServer(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error',
                                             access_log=False, lifespan='off', timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[sock]))

        async def started():
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError('Relay server did not start.')
                await asyncio.sleep(.01)

        await asyncio.wait_for(started(), timeout=5)
        yield f'http://127.0.0.1:{port}/v1', app
    finally:
        try:
            if server is not None:
                server.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=3)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    if server is not None:
                        server.force_exit = True
                    task.cancel()
                    with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                        await asyncio.wait_for(task, timeout=1)
        finally:
            sock.close()
