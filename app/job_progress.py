"""Durable display milestones and safe event summaries, never model reasoning.

Timing measures active execution only. A process-local monotonic clock prevents
wall-clock changes and downtime from being counted as work. After an unclean
restart only checkpointed durations are known; the missing interval is explicit.
"""
import json
import logging
import re
import time
import uuid
from functools import wraps

from .store import dumps, now

VERSION = 'job-progress-v1'
MAX_EVENTS = 120
RUN_ID = uuid.uuid4().hex
STATUSES = {'pending', 'active', 'completed', 'failed', 'blocked'}
ACTIVITIES = {'retrieving', 'embedding', 'rewriting', 'reranking', 'planning',
    'writing', 'solving', 'reviewing', 'repairing', 'validating', 'saving',
    'generating_audio', 'generating_image', 'checking_evidence', 'searching_sources',
    'reading_sources', 'screening_sources', 'indexing_sources'}
FUSION_REASONS = {'clarification_unresolved', 'input_limit', 'interrupted_rewrite',
    'rewrite_timeout', 'rewrite_failed', 'invalid_output', 'ambiguous_input',
    'language_changed', 'literal_changed', 'number_added', 'unchanged_query',
    'rewrite_retrieval_failed', 'unavailable'}
# Keys and values are host-owned. No free-text data, model output or exception.
EVENT_FIELDS = {
    'stage_started': {}, 'stage_completed': {},
    'activity_changed': {'activity': ACTIVITIES},
    'retrieval_selected': {'selected': 'integer'},
    'exploration_started': {},
    'exploration_search': {'round': 'integer', 'queries': 'integer'},
    'exploration_source_accepted': {'accepted': 'integer'},
    'exploration_complete': {'accepted': 'integer'},
    'exploration_stopped': {'accepted': 'integer'},
    'fusion_applied': {'routes': {2}},
    'fusion_fallback': {'reason': FUSION_REASONS},
    'plan_ready': {'total': 'integer'},
    'question_started': {'number': 'integer', 'total': 'integer'},
    'question_passed': {'number': 'integer', 'completed': 'integer', 'total': 'integer'},
    'question_repair': {'number': 'integer', 'attempt': 'integer'},
    'question_rejected': {'number': 'integer', 'attempt': 'integer'},
    'question_check_failed': {'number': 'integer', 'attempt': 'integer',
        'reason': {'format', 'difficulty', 'quality', 'evidence', 'duplicate'}},
    'question_format_adapting': {'number': 'integer'},
    'question_format_adapted': {'number': 'integer', 'level': {'compatible'}},
    'task_succeeded': {}, 'task_failed': {}, 'task_blocked': {},
    'task_interrupted': {'cause': {'service_stop', 'restart'}},
    'task_resumed': {},
}


def _nonfatal(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            # Display instrumentation cannot change a business result. Keep
            # diagnostics fixed, without logging model text or user input.
            logging.getLogger(__name__).warning('Task progress could not be persisted.')
    return wrapped


def _timestamp(value):
    return isinstance(value, str) and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.+Z-]{8,40}', value))


def _integer(value):
    return type(value) is int and 0 <= value <= 10**18


def _event_data(code, data):
    fields = EVENT_FIELDS.get(code)
    if fields is None or not isinstance(data, dict):
        return None
    result = {}
    for key, allowed in fields.items():
        value = data.get(key)
        if allowed == 'integer':
            if type(value) is not int or not 0 <= value <= 10**7:
                return None
        elif type(value) not in (str, int) or value not in allowed:
            return None
        result[key] = value
    return result


def _write(db, job_id, value):
    value['updated_at'] = now()
    db.execute('''INSERT INTO job_timelines VALUES(?,?,?) ON CONFLICT(job_id)
        DO UPDATE SET timeline=excluded.timeline,updated_at=excluded.updated_at''',
        (job_id, dumps(value), value['updated_at']))


def _stored(db, job_id):
    row = db.execute('SELECT timeline FROM job_timelines WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row['timeline'])
        if value.get('version') != VERSION or not isinstance(value.get('stages'), list):
            return None
        stages = []
        for stage in value['stages']:
            if not isinstance(stage, dict) or not all(isinstance(stage.get(k), str)
                    for k in ('id', 'kind', 'status')):
                return None
            if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', stage['id']) or
                    not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', stage['kind']) or stage['status'] not in STATUSES):
                return None
            clean = {k: stage[k] for k in ('id', 'kind', 'status')}
            timing_valid = (type(stage.get('timing_complete')) is bool
                and (stage.get('elapsed_ms') is None or _integer(stage['elapsed_ms']))
                and all(stage.get(k) is None or _timestamp(stage[k]) for k in ('started_at', 'finished_at')))
            clean.update(started_at=stage.get('started_at') if timing_valid else None,
                finished_at=stage.get('finished_at') if timing_valid else None,
                elapsed_ms=stage.get('elapsed_ms') if timing_valid else None,
                timing_complete=stage['timing_complete'] if timing_valid else False)
            elapsed_ns = stage.get('_elapsed_ns')
            if not _integer(elapsed_ns):
                elapsed_ns = (clean['elapsed_ms'] or 0) * 1_000_000
            clean['_elapsed_ns'] = elapsed_ns
            timer = stage.get('_timer')
            if (timing_valid and isinstance(timer, dict) and
                    isinstance(timer.get('run'), str) and re.fullmatch(r'[0-9a-f]{32}', timer['run']) and
                    _integer(timer.get('tick_ns'))):
                clean['_timer'] = {'run': timer['run'], 'tick_ns': timer['tick_ns']}
            stages.append(clean)
        ids = {stage['id'] for stage in stages}
        if len(ids) != len(stages) or not stages:
            return None
        completed, total, unit = value.get('completed'), value.get('total'), value.get('unit')
        if total is not None:
            if type(total) is not int or type(completed) is not int or not 0 <= completed <= total or unit != 'questions':
                return None
        elif completed is not None or unit is not None:
            return None
        active = value.get('active_stage')
        if active is not None and (not isinstance(active, str) or active not in ids):
            return None
        activity = value.get('activity')
        if activity is not None and (not isinstance(activity, str) or activity not in ACTIVITIES):
            activity = None
        updated = value.get('updated_at')
        if not _timestamp(updated):
            return None
        raw_events = value.get('events', [])
        events = []
        if not isinstance(raw_events, list):
            raw_events = []
        for event in raw_events[-MAX_EVENTS:]:
            if (not isinstance(event, dict) or not _integer(event.get('id')) or event['id'] < 1 or
                    not isinstance(event.get('stage'), str) or event['stage'] not in ids or
                    not isinstance(event.get('code'), str) or not _timestamp(event.get('at'))):
                continue
            data = _event_data(event['code'], event.get('data'))
            if data is None or (events and event['id'] <= events[-1]['id']):
                continue
            events.append({'id': event['id'], 'stage': event['stage'], 'code': event['code'],
                'at': event['at'], 'data': data})
        event_seq = value.get('_event_seq')
        minimum = events[-1]['id'] + 1 if events else 1
        if not _integer(event_seq) or event_seq < minimum:
            event_seq = minimum
        terminal = value.get('_terminal_status')
        terminal = terminal if terminal in ('succeeded', 'failed', 'insufficient_evidence') else None
        # Whitelist projection includes private timer fields only internally;
        # snapshot strips them before returning an HTTP result.
        return {'version': VERSION, 'recorded': True, 'stages': stages,
            'active_stage': active, 'activity': activity, 'completed': completed,
            'total': total, 'unit': unit, 'updated_at': updated,
            'events': events, 'events_truncated': value.get('events_truncated') is True or len(raw_events) > MAX_EVENTS,
            '_event_seq': event_seq, '_terminal_status': terminal}
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _append(value, stage, code, data, at):
    data = _event_data(code, data)
    if data is None or stage not in {s['id'] for s in value['stages']}:
        return
    value['events'].append({'id': value['_event_seq'], 'stage': stage, 'code': code, 'at': at, 'data': data})
    value['_event_seq'] += 1
    if len(value['events']) > MAX_EVENTS:
        value['events'] = value['events'][-MAX_EVENTS:]
        value['events_truncated'] = True


def _accrue(stage, tick, *, interrupted=False):
    timer = stage.get('_timer')
    if timer is None:
        if stage['status'] == 'active':
            stage['timing_complete'] = False
        return
    if interrupted or timer['run'] != RUN_ID or tick < timer['tick_ns']:
        stage['timing_complete'] = False
    else:
        stage['_elapsed_ns'] += tick - timer['tick_ns']
        stage['elapsed_ms'] = stage['_elapsed_ns'] // 1_000_000
    timer['tick_ns'] = tick


def _stop(stage, tick, at, *, interrupted=False):
    _accrue(stage, tick, interrupted=interrupted)
    had_timer = stage.pop('_timer', None)
    if interrupted:
        stage['timing_complete'] = False
        stage['finished_at'] = None  # The stop time was not observed.
    else:
        stage['finished_at'] = at
        if had_timer is None and stage['elapsed_ms'] is None:
            stage['timing_complete'] = False


def _checkpoint(value, tick):
    for stage in value['stages']:
        if stage['status'] == 'active':
            _accrue(stage, tick)


@_nonfatal
def begin(store, job_id, stages, *, total=None):
    """A repeated execution keeps completed milestones and accepted counts."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _stored(db, job_id) is not None:
            return
        value = {'version': VERSION, 'recorded': True,
            'stages': [{'id': stage, 'kind': stage, 'status': 'pending',
                'started_at': None, 'finished_at': None, 'elapsed_ms': None,
                'timing_complete': True, '_elapsed_ns': 0} for stage in stages],
            'active_stage': None, 'activity': None,
            'completed': 0 if total is not None else None, 'total': total,
            'unit': 'questions' if total is not None else None,
            'events': [], 'events_truncated': False, '_event_seq': 1, '_terminal_status': None}
        _write(db, job_id, value)


@_nonfatal
def update(store, job_id, stage=None, *, activity=None, complete=False, completed=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        value = _stored(db, job_id)
        if value is None:
            return  # Retrieval-only research scripts do not acquire a fake workflow.
        tick, at = time.monotonic_ns(), now()
        _checkpoint(value, tick)
        if stage is not None:
            target = next((s for s in value['stages'] if s['id'] == stage), None)
            if target is None:
                return
            if complete:
                if target['status'] != 'completed':
                    _stop(target, tick, at)
                    _append(value, stage, 'stage_completed', {}, at)
                target['status'] = 'completed'
                if value['active_stage'] == stage:
                    value.update(active_stage=None, activity=None)
            else:
                was_active = target['status'] == 'active' and value['active_stage'] == stage
                previous_activity = value['activity'] if was_active else None
                for other in value['stages']:
                    if other['status'] == 'active' and other is not target:
                        _stop(other, tick, at)
                        other['status'] = 'pending'
                if not was_active or '_timer' not in target or target['_timer']['run'] != RUN_ID:
                    if target['started_at'] is None:
                        target['started_at'] = at
                    target['finished_at'] = None
                    target['elapsed_ms'] = target['_elapsed_ns'] // 1_000_000
                    target['_timer'] = {'run': RUN_ID, 'tick_ns': tick}
                    _append(value, stage, 'stage_started', {}, at)
                target['status'] = 'active'
                value.update(active_stage=stage, activity=activity if activity in ACTIVITIES else None)
                if activity in ACTIVITIES and activity != previous_activity:
                    _append(value, stage, 'activity_changed', {'activity': activity}, at)
        if completed is not None and value['total'] is not None:
            value['completed'] = min(value['total'], max(0, completed))
        _write(db, job_id, value)


@_nonfatal
def event(store, job_id, stage, code, **data):
    """Record a host-observed result using a closed, non-prose vocabulary."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        value = _stored(db, job_id)
        if value is None or _event_data(code, data) is None:
            return
        _checkpoint(value, time.monotonic_ns())
        _append(value, stage, code, data, now())
        _write(db, job_id, value)


@_nonfatal
def finish(store, job_id, status, *, interruption=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        value = _stored(db, job_id)
        if value is None or value['_terminal_status'] == status:
            return
        tick, at = time.monotonic_ns(), now()
        target = next((s for s in value['stages'] if s['id'] == value['active_stage']), None)
        if target:
            _stop(target, tick, at, interrupted=interruption == 'restart')
            target['status'] = {'succeeded': 'completed', 'insufficient_evidence': 'blocked'}.get(status, 'failed')
            if status == 'succeeded':
                _append(value, target['id'], 'stage_completed', {}, at)
        stage = target['id'] if target else value['stages'][0]['id']
        if interruption in ('restart', 'service_stop'):
            _append(value, stage, 'task_interrupted', {'cause': interruption}, at)
        else:
            code = {'succeeded': 'task_succeeded', 'insufficient_evidence': 'task_blocked'}.get(status, 'task_failed')
            _append(value, stage, code, {}, at)
        value.update(activity=None, _terminal_status=status)
        _write(db, job_id, value)


@_nonfatal
def queued(db, job_id):
    """Called inside the same atomic transaction as explicit resume."""
    value = _stored(db, job_id)
    if value is None:
        return
    at, tick = now(), time.monotonic_ns()
    target = value['active_stage'] or value['stages'][0]['id']
    for stage in value['stages']:
        if stage['status'] == 'active':
            # An active timer in a failed job means its stopping instant was
            # not checkpointed. Never count the pause before this resume.
            _stop(stage, tick, at, interrupted=True)
        if stage['status'] in ('active', 'failed', 'blocked'):
            stage['status'] = 'pending'
    _append(value, target, 'task_resumed', {}, at)
    value.update(active_stage=None, activity=None, _terminal_status=None)
    _write(db, job_id, value)


def _unknown():
    return {'version': VERSION, 'recorded': False, 'stages': [],
        'active_stage': None, 'activity': None, 'completed': None,
        'total': None, 'unit': None, 'updated_at': None,
        'elapsed_ms': None, 'timing_complete': False, 'events': [], 'events_truncated': False}


def snapshot(store, job):
    try:
        return _snapshot(store, job)
    except Exception:
        logging.getLogger(__name__).warning('Task progress is temporarily unavailable.')
        return _unknown()


def _snapshot(store, job):
    with store.connect() as db:
        value = _stored(db, job['id'])
    if value is None:
        return _unknown()
    status, tick = job['status'], time.monotonic_ns()
    for stage in value['stages']:
        if stage['status'] == 'active':
            if status == 'running':
                _accrue(stage, tick)
            else:
                # A terminal database row without a terminal timer write has
                # an unknown stopping instant; preserve only checkpointed time.
                _stop(stage, tick, now(), interrupted=True)
                stage['status'] = {'succeeded': 'completed', 'failed': 'failed',
                    'insufficient_evidence': 'blocked'}.get(status, 'pending')
        stage.pop('_timer', None)
        stage.pop('_elapsed_ns', None)
    if status != 'running':
        value['activity'] = None
    if status == 'queued':
        value['active_stage'] = None
    known = [stage['elapsed_ms'] for stage in value['stages'] if stage['elapsed_ms'] is not None]
    value['elapsed_ms'] = sum(known) if known else None
    value['timing_complete'] = all(stage['timing_complete'] for stage in value['stages'])
    value.pop('_event_seq', None)
    value.pop('_terminal_status', None)
    return value
