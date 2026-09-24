"""Scoped bridge core tests; no MCP installation, credentials, or API calls."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import stat
import sys
import types

import pytest

from app.harness_tools import MANIFEST_SCHEMA, ScopedTeachingTools, create_server
from app.store import Store


def cpu_input(**updates):
    return {'policy': 'rr', 'processes': [{'id': 'A', 'arrival': 0, 'burst': 4},
            {'id': 'B', 'arrival': 1, 'burst': 2}], 'quantum': 2,
            'switch_cost': 1, 'boundary': 'before', 'during_switch': 'tail',
            'early_finish_free': False, **updates}


@pytest.fixture
def scoped(tmp_path):
    store = Store(tmp_path / 'data')
    for user in ('alice', 'bob'):
        store.execute('INSERT INTO users VALUES(?,?,?,?,?)', (user, user+'@example.test', 'fixture', user, 'now'))
        store.execute('INSERT INTO courses VALUES(?,?,?)', (user+'-course', user, 'now'))
        store.execute('INSERT INTO course_owners VALUES(?,?)', (user+'-course', user))
        store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                      (user+'-document', user+'-course', user+'.md', user+'-sha', 'indexed', 1, 'now'))
    job, _ = store.job('own-job', 'generate', {'course_id': 'alice-course'}, owner_id='alice')
    other, _ = store.job('foreign-job', 'generate', {'course_id': 'bob-course'}, owner_id='bob')
    manifest = {'schema': MANIFEST_SCHEMA, 'job_id': job['id'], 'data_dir': str(tmp_path / 'data'),
                'course_id': 'alice-course', 'owner_id': 'alice', 'enforce_account_ownership': True,
                'sources': [{'id': 'chunk-1', 'document_id': 'alice-document', 'page': 1,
                             'document_name': 'notes.md', 'text': 'Frozen RR reference.',
                             'metadata': {'extraction_warnings': ['fixture warning']}}],
                'audit_path': str(tmp_path / 'audit' / 'tools.jsonl')}
    return store, manifest, other


def read_audit(manifest):
    return [json.loads(line) for line in Path(manifest['audit_path']).read_text().splitlines()]


def test_positive_snapshot_cpu_and_private_full_audit(scoped, capsys):
    _, manifest, _ = scoped
    core = ScopedTeachingTools(manifest)
    manifest['sources'][0]['text'] = 'changed after construction'
    refs = core.get_reference_chunks()
    assert refs['ok'] and refs['retrieval_mode'] == 'frozen_job_snapshot'
    assert refs['sources'][0]['text'] == 'Frozen RR reference.'
    assert refs['sources'][0]['metadata']['extraction_warnings'] == ['fixture warning']
    refs['sources'][0]['text'] = 'modified returned object'
    assert core.get_reference_chunks()['sources'][0]['text'] == 'Frozen RR reference.'
    result = core.cpu_schedule_v1(cpu_input())
    assert result['ok'] and result['makespan'] == 8
    assert result['per_process']['A']['completion'] == 8
    assert result['per_process']['B']['completion'] == 5
    assert result['averages']['turnaround'] == {'numerator': 6, 'denominator': 1}
    assert result['timeline_omitted'] is False
    assert any('payload' in value for value in result['limitations'])
    audit = read_audit(manifest)
    assert len(audit) == 3
    assert audit[-1]['input'] == cpu_input()
    assert audit[-1]['result']['timeline'] == result['timeline']
    assert audit[0]['result']['sources'][0]['text'] == 'Frozen RR reference.'
    assert stat.S_IMODE(Path(manifest['audit_path']).stat().st_mode) == 0o600
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('mutation', ['job', 'course', 'owner', 'document', 'job_payload', 'job_owner', 'course_owner'])
@pytest.mark.parametrize('tool', ['get_reference_chunks', 'cpu_schedule_v1'])
def test_foreign_scope_is_rejected_without_reference_leak(scoped, mutation, tool):
    store, manifest, other = scoped
    if mutation == 'job':
        manifest['job_id'] = other['id']
    elif mutation == 'course':
        manifest['course_id'] = 'bob-course'
    elif mutation == 'owner':
        manifest['owner_id'] = 'bob'
    elif mutation == 'document':
        manifest['sources'][0]['document_id'] = 'bob-document'
    elif mutation == 'job_payload':
        store.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps({'course_id': 'bob-course'}), manifest['job_id']))
    elif mutation == 'job_owner':
        store.execute('UPDATE job_owners SET user_id=? WHERE job_id=?', ('bob', manifest['job_id']))
    else:
        store.execute('UPDATE course_owners SET user_id=? WHERE course_id=?', ('bob', 'alice-course'))
    core = ScopedTeachingTools(manifest)
    result = getattr(core, tool)(*([cpu_input()] if tool == 'cpu_schedule_v1' else []))
    assert result['error']['code'] == 'scope_denied'
    assert 'Frozen RR' not in json.dumps(result)
    assert read_audit(manifest)[0]['result'] == result


@pytest.mark.parametrize('state', ['disabled', 'deleted', 'missing', 'moved'])
@pytest.mark.parametrize('tool', ['get_reference_chunks', 'cpu_schedule_v1'])
def test_each_call_rechecks_reference_lifecycle(scoped, state, tool):
    store, manifest, _ = scoped
    core = ScopedTeachingTools(manifest)
    assert core.get_reference_chunks()['ok']
    if state == 'disabled':
        store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-document', 0, None))
    elif state == 'deleted':
        store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-document', 1, 'now'))
    elif state == 'missing':
        store.execute('DELETE FROM documents WHERE id=?', ('alice-document',))
    else:
        store.execute('UPDATE documents SET course_id=? WHERE id=?', ('bob-course', 'alice-document'))
    result = getattr(core, tool)(*([cpu_input()] if tool == 'cpu_schedule_v1' else []))
    assert result['error']['code'] == 'scope_denied'


def test_owner_is_rechecked_after_server_creation(scoped):
    store, manifest, _ = scoped
    core = ScopedTeachingTools(manifest)
    assert core.get_reference_chunks()['ok']
    store.execute('DELETE FROM job_owners WHERE job_id=?', (manifest['job_id'],))
    assert core.cpu_schedule_v1(cpu_input())['error']['code'] == 'scope_denied'


@pytest.mark.parametrize('tool', ['get_reference_chunks', 'cpu_schedule_v1'])
def test_cancelled_task_blocks_new_tools_and_preserves_denial_audit(scoped, monkeypatch, tool):
    import app.harness_tools as tools
    store, manifest, _ = scoped
    core = ScopedTeachingTools(manifest)
    store.request_cancel(manifest['job_id'])
    def forbidden(*args): pytest.fail('Cancelled work must not execute a calculation')
    monkeypatch.setattr(tools, 'run_answer_tool', forbidden)
    result = getattr(core, tool)(*([cpu_input()] if tool == 'cpu_schedule_v1' else []))
    assert result['ok'] is False and result['error']['code'] == 'task_cancelled'
    assert 'Frozen RR' not in json.dumps(result)
    assert read_audit(manifest)[-1]['result'] == result


def test_revision_tools_keep_same_course_and_document_boundary(scoped):
    store, manifest, _ = scoped
    store.execute("UPDATE jobs SET kind='revise_question' WHERE id=?", (manifest['job_id'],))
    core = ScopedTeachingTools(manifest)
    assert core.get_reference_chunks()['ok'] is True
    store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-document', 0, None))
    assert core.get_reference_chunks()['error']['code'] == 'scope_denied'


def test_research_ownerless_requires_matching_ownerless_course(scoped):
    store, manifest, _ = scoped
    manifest.update(enforce_account_ownership=False, owner_id=None)
    assert ScopedTeachingTools(manifest).get_reference_chunks()['error']['code'] == 'scope_denied'
    store.execute('DELETE FROM job_owners WHERE job_id=?', (manifest['job_id'],))
    assert ScopedTeachingTools(manifest).get_reference_chunks()['error']['code'] == 'scope_denied'
    store.execute('DELETE FROM course_owners WHERE course_id=?', ('alice-course',))
    assert ScopedTeachingTools(manifest).get_reference_chunks()['ok']
    manifest['course_id'] = 'bob-course'
    assert ScopedTeachingTools(manifest).get_reference_chunks()['error']['code'] == 'scope_denied'


def test_research_still_checks_document_activity(scoped):
    store, manifest, _ = scoped
    manifest['enforce_account_ownership'] = False
    store.execute('INSERT INTO document_lifecycle VALUES(?,?,?)', ('alice-document', 0, None))
    assert ScopedTeachingTools(manifest).cpu_schedule_v1(cpu_input())['error']['code'] == 'scope_denied'


@pytest.mark.parametrize('payload', [
    {}, [], {'sql': 'SELECT * FROM users'}, cpu_input(quantum=0),
    cpu_input(quantum=True), cpu_input(quantum=1000000), cpu_input(extra='not allowed'),
    cpu_input(processes=[{'id': 'A', 'arrival': -1, 'burst': 1}]),
    cpu_input(processes=[{'id': 'A', 'arrival': 0, 'burst': 0}]),
    cpu_input(course_id='bob-course'), cpu_input(file='/private/secret-file'),
])
def test_invalid_model_parameters_are_sanitized_and_audited(scoped, payload):
    _, manifest, _ = scoped
    result = ScopedTeachingTools(manifest).cpu_schedule_v1(payload)
    assert result['error']['code'] == 'invalid_input'
    assert result == {'ok': False, 'error': {'code': 'invalid_input',
        'message': 'Invalid cpu_schedule_v1 input; follow the published tool schema.'}}
    assert read_audit(manifest)[0]['input'] == payload


def test_large_timeline_keeps_all_metrics_and_full_audit(scoped):
    _, manifest, _ = scoped
    payload = cpu_input(quantum=1, processes=[{'id': 'A', 'arrival': 0, 'burst': 30},
                                            {'id': 'B', 'arrival': 0, 'burst': 30}])
    result = ScopedTeachingTools(manifest).cpu_schedule_v1(payload)
    audit_result = read_audit(manifest)[0]['result']
    assert result['ok'] and result['timeline_omitted'] is True
    assert result['timeline'] == [] and result['timeline_count'] == 119
    assert len(audit_result['timeline']) == 119
    for field in ('per_process', 'averages', 'switch_count', 'overhead', 'makespan'):
        assert result[field] == audit_result[field]
    assert result['makespan'] == 119 and result['overhead'] == 59


@pytest.mark.parametrize('mutation', ['missing_owner', 'bool_string', 'schema', 'relative_path', 'api_key', 'missing_document', 'duplicate_source'])
def test_invalid_manifests_are_rejected(scoped, mutation):
    _, manifest, _ = scoped
    if mutation == 'missing_owner':
        manifest.pop('owner_id')
    elif mutation == 'bool_string':
        manifest['enforce_account_ownership'] = 'false'
    elif mutation == 'schema':
        manifest['schema'] = 'unknown'
    elif mutation == 'relative_path':
        manifest['audit_path'] = 'relative.jsonl'
    elif mutation == 'api_key':
        manifest['api_key'] = 'must-not-be-accepted'
    elif mutation == 'missing_document':
        manifest['sources'][0].pop('document_id')
    else:
        manifest['sources'].append(deepcopy(manifest['sources'][0]))
    with pytest.raises(ValueError, match='^Invalid teaching tools manifest.$'):
        ScopedTeachingTools(manifest)


def test_manifest_file_loader_and_fixed_read_error(scoped, tmp_path):
    _, manifest, _ = scoped
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    assert ScopedTeachingTools.from_manifest(path).get_reference_chunks()['ok']
    with pytest.raises(ValueError, match='^Invalid teaching tools manifest.$'):
        ScopedTeachingTools.from_manifest(tmp_path / 'sensitive-missing-path')


def test_audit_error_fails_closed_and_never_exposes_path(scoped, tmp_path):
    _, manifest, _ = scoped
    manifest['audit_path'] = str(tmp_path)  # Existing directory cannot be the audit file.
    result = ScopedTeachingTools(manifest).get_reference_chunks()
    assert result['error']['code'] == 'audit_unavailable'
    assert 'sources' not in result and str(tmp_path) not in json.dumps(result)


def test_audit_symlink_is_not_followed(scoped, tmp_path):
    _, manifest, _ = scoped
    target = tmp_path / 'unchanged.txt'
    target.write_text('keep')
    audit = Path(manifest['audit_path'])
    audit.parent.mkdir()
    audit.symlink_to(target)
    assert ScopedTeachingTools(manifest).get_reference_chunks()['error']['code'] == 'audit_unavailable'
    assert target.read_text() == 'keep'


def test_existing_audit_permissions_are_tightened(scoped):
    _, manifest, _ = scoped
    audit = Path(manifest['audit_path'])
    audit.parent.mkdir()
    audit.write_text('')
    audit.chmod(0o644)
    assert ScopedTeachingTools(manifest).get_reference_chunks()['ok']
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600


def test_core_import_does_not_import_mcp_or_providers(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not (name == 'mcp' or name.startswith('mcp.') or name.endswith('providers') or name.endswith('generation_agent'))
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    import app.harness_tools
    importlib.reload(app.harness_tools)


def test_mcp_publishes_only_two_scoped_tools(scoped, monkeypatch):
    class FakeMCP:
        def __init__(self, *args, **kwargs):
            self.tools = {}
        def tool(self, *, name, **kwargs):
            def decorate(fn):
                self.tools[name] = fn
                return fn
            return decorate
    module = types.ModuleType('mcp.server.fastmcp')
    module.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, 'mcp.server.fastmcp', module)
    _, manifest, _ = scoped
    server = create_server(ScopedTeachingTools(manifest))
    assert set(server.tools) == {'get_reference_chunks', 'cpu_schedule_v1'}
    assert server.tools['get_reference_chunks']()['ok']
    assert server.tools['cpu_schedule_v1'](cpu_input())['ok']
    with pytest.raises(TypeError):
        server.tools['get_reference_chunks'](course_id='bob-course')
