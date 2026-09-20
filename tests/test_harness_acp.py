"""Exercise controller failure/cancellation boundaries with a subprocess peer.

This is protocol-fixture evidence only; real-runtime checks are separate.
"""
import asyncio
import json
import sys

import pytest

from app.harness_acp import ACPClient
from app.providers import ProviderError


PEER = '''
import json, sys, os, subprocess, time
def send(value):
    print(json.dumps({'jsonrpc':'2.0', **value}), flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    method=frame.get('method')
    if method=='initialize':
        send({'id':frame['id'], 'result':{'protocolVersion':1}})
    elif method=='session/new':
        send({'id':frame['id'], 'result':{'sessionId':'fixture-session'}})
    elif method=='session/prompt':
        send({'id':'permission-1','method':'session/request_permission','params':{}})
        send({'id':'filesystem-1','method':'fs/read_text_file','params':{'path':'secret'}})
        send({'method':'session/update','params':{'sessionId':'fixture-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'waiting'}}}})
    elif method=='session/cancel':
        open(sys.argv[1], 'w').write('cancelled')
    elif method=='session/close':
        send({'id':frame['id'], 'result':{}})
    elif method=='fixture/large':
        send({'id':frame['id'], 'result':{'text':'x'*2000}})
    elif method=='fixture/exit':
        sys.exit(0)
    elif method=='fixture/stubborn-child':
        marker=sys.argv[1]+'.child-ready'
        code="import signal,time,pathlib,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
        child=subprocess.Popen([sys.executable,'-c',code,marker], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not os.path.exists(marker): time.sleep(0.01)
        send({'id':frame['id'],'result':{'child':child.pid}})
    elif method=='fixture/error':
        send({'id':frame['id'], 'error':{'code':-1,'message':'private upstream text'}})
    elif 'result' in frame or 'error' in frame:
        with open(sys.argv[1]+'.replies','a') as out:
            out.write(json.dumps(frame)+'\\n')
'''


def client(tmp_path, max_bytes=8000000):
    peer = tmp_path / 'peer.py'
    peer.write_text(PEER)
    return ACPClient([sys.executable, str(peer), str(tmp_path / 'cancel')],
                     cwd=str(tmp_path), env={'PATH': '/usr/bin:/bin'}, max_bytes=max_bytes)


def test_timeout_cancels_and_reverse_host_operations_are_denied(tmp_path):
    async def run():
        async with client(tmp_path) as acp:
            await acp.initialize()
            await acp.new_session(tmp_path, [])
            with pytest.raises(TimeoutError):
                await acp.prompt('question', 0.15)
        assert acp.proc.returncode is not None
    asyncio.run(run())
    assert (tmp_path / 'cancel').read_text() == 'cancelled'
    replies = [json.loads(line) for line in (tmp_path / 'cancel.replies').read_text().splitlines()]
    assert replies[0]['result']['outcome']['outcome'] == 'cancelled'
    assert replies[1]['error']['code'] == -32601


@pytest.mark.parametrize('method,max_bytes', [('fixture/exit',8000000), ('fixture/large',1024),
                                            ('fixture/error',8000000)])
def test_broken_oversized_or_error_reply_fails_without_private_error_text(tmp_path, method, max_bytes):
    async def run():
        async with client(tmp_path, max_bytes) as acp:
            await acp.initialize()
            with pytest.raises(ProviderError) as error:
                await acp.request(method, {}, timeout=2)
            assert 'private upstream text' not in str(error.value)
    asyncio.run(run())


def test_caller_cancel_closes_subprocess_and_sends_protocol_cancel(tmp_path):
    async def run():
        async with client(tmp_path) as acp:
            await acp.initialize()
            await acp.new_session(tmp_path, [])
            task = asyncio.create_task(acp.prompt('question', 30))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert acp.proc.returncode is not None
    asyncio.run(run())
    assert (tmp_path / 'cancel').read_text() == 'cancelled'


def test_cleanup_kills_stubborn_descendant_after_parent_exit(tmp_path):
    import os
    async def run():
        async with client(tmp_path) as acp:
            await acp.initialize()
            child = (await acp.request('fixture/stubborn-child', {}))['child']
        for _ in range(40):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)
        pytest.fail('A descendant survived process-group cleanup')
    asyncio.run(run())
