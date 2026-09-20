"""Small bounded ACP stdio client for the optional, pinned official runtime.

The controller supplies commands, workspace and MCP servers. The model cannot
request shell/filesystem operations from this client; all reverse RPCs fail shut.
"""
import asyncio
from contextlib import suppress
import json
import os
import signal

from .providers import ProviderError


class ACPClient:
    def __init__(self, command, *, cwd, env, max_bytes=8_000_000):
        self.command, self.cwd, self.env = command, cwd, env
        self.max_bytes = max_bytes
        self.events, self.pending = [], {}
        self.counter = self.bytes_read = 0
        self.proc = None
        self.session_id = None
        self.stderr_tail = b''
        self.reader = self.err_reader = None
        self.failure = None

    async def __aenter__(self):
        try:
            self.proc = await asyncio.create_subprocess_exec(*self.command, cwd=self.cwd,
                env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=2_000_000, start_new_session=True)
        except OSError:
            raise ProviderError('Harness 运行环境不可用，请先完成隔离安装。') from None
        self.reader = asyncio.create_task(self._read())
        self.err_reader = asyncio.create_task(self._read_stderr())
        return self

    async def _read_stderr(self):
        while chunk := await self.proc.stderr.read(8192):
            self.stderr_tail = (self.stderr_tail + chunk)[-65536:]

    async def _send(self, frame):
        if self.failure or self.proc.returncode is not None:
            raise ProviderError('Harness 连接中断；本次未自动重试。')
        try:
            self.proc.stdin.write((json.dumps(frame, ensure_ascii=False) + '\n').encode())
            await self.proc.stdin.drain()
        except (OSError, ConnectionError):
            raise ProviderError('Harness 连接中断；本次未自动重试。') from None

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                self.bytes_read += len(line)
                if self.bytes_read > self.max_bytes or len(self.events) >= 4096:
                    raise ValueError('ACP bound')
                frame = json.loads(line)
                if not isinstance(frame, dict) or frame.get('jsonrpc') != '2.0':
                    raise ValueError('ACP frame')
                if 'method' in frame:
                    self.events.append(frame)
                    if 'id' in frame:
                        # This profile needs no approvals. Never implement terminal,
                        # read/writeTextFile or arbitrary host callback operations.
                        if frame['method'] == 'session/request_permission':
                            reply = {'result': {'outcome': {'outcome': 'cancelled'}}}
                        else:
                            reply = {'error': {'code': -32601, 'message': 'Unsupported client operation'}}
                        await self._send({'jsonrpc': '2.0', 'id': frame['id'], **reply})
                    continue
                future = self.pending.get(frame.get('id'))
                if future is not None and not future.done():
                    if 'error' in frame:
                        future.set_exception(ProviderError('Harness 请求失败；本次未自动重试。'))
                    elif 'result' in frame:
                        future.set_result(frame['result'])
                    else:
                        raise ValueError('ACP response')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = True
        finally:
            self.failure = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProviderError('Harness 连接中断或返回内容超限；本次未自动重试。'))

    async def request(self, method, params, timeout=30):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method, params):
        await self._send({'jsonrpc': '2.0', 'method': method, 'params': params})

    async def initialize(self):
        result = await self.request('initialize', {'protocolVersion': 1,
            'clientInfo': {'name': 'fyp-teaching-controller', 'version': '1'}, 'clientCapabilities': {}})
        if result.get('protocolVersion') != 1:
            raise ProviderError('Harness ACP 协议版本不兼容。')
        return result

    async def new_session(self, cwd, mcp_servers):
        result = await self.request('session/new', {'cwd': str(cwd), 'mcpServers': mcp_servers})
        self.session_id = result['sessionId']
        return result

    async def prompt(self, text, timeout):
        try:
            return await self.request('session/prompt', {'sessionId': self.session_id,
                'prompt': [{'type': 'text', 'text': text}]}, timeout)
        except (TimeoutError, asyncio.CancelledError):
            with suppress(Exception):
                await asyncio.wait_for(self.notify('session/cancel', {'sessionId': self.session_id}), 2)
            raise

    async def __aexit__(self, *exc):
        if not self.proc:
            return
        if self.session_id and not self.failure and self.proc.returncode is None:
            with suppress(Exception):
                await self.request('session/close', {'sessionId': self.session_id}, timeout=3)
        if self.proc.stdin:
            self.proc.stdin.close()
        with suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 2)
        # Include the scoped MCP child, even if the controller already exited.
        with suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        with suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 2)
        # A parent may already have exited while a descendant ignores SIGTERM.
        # Check the process group, not just the parent's return code.
        for _ in range(10):
            try:
                os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            with suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        if self.proc.returncode is None:
            await self.proc.wait()
        for task in (self.reader, self.err_reader):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
