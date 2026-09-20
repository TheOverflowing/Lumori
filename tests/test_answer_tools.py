"""Hand-derived answer-tool fixtures and strict input boundaries; no providers."""
from copy import deepcopy

import pytest

from app.answer_tools import TOOL_CONTRACTS, run_answer_tool


def rr(processes, **overrides):
    return {'policy': 'rr', 'processes': processes, 'quantum': 2, 'switch_cost': 0,
            'boundary': 'before', 'during_switch': 'tail', 'early_finish_free': False, **overrides}


def process(name, arrival, burst):
    return {'id': name, 'arrival': arrival, 'burst': burst}


def run(payload):
    return run_answer_tool('cpu_schedule_v1', payload)


def assert_conservation(payload, result):
    timeline = result['timeline']
    assert timeline[0]['start'] == 0
    assert all(row['end'] > row['start'] for row in timeline)
    assert all(left['end'] == right['start'] for left, right in zip(timeline, timeline[1:]))
    for item in payload['processes']:
        intervals = [row for row in timeline if row['kind'] == 'run' and row['process_id'] == item['id'].strip()]
        assert sum(row['end'] - row['start'] for row in intervals) == item['burst']
        assert all(row['start'] >= item['arrival'] for row in intervals)
    idle = sum(row['end'] - row['start'] for row in timeline if row['kind'] == 'idle')
    assert sum(item['burst'] for item in payload['processes']) + result['overhead'] + idle == result['makespan']


def test_q016_rr_corrected_hand_timeline_and_exact_averages():
    payload = rr([process('A', 0, 6), process('B', 1, 3), process('C', 2, 1)])
    before = deepcopy(payload)
    result = run(payload)
    assert [(row['process_id'], row['start'], row['end']) for row in result['timeline']] == [
        ('A', 0, 2), ('B', 2, 4), ('C', 4, 5), ('A', 5, 7), ('B', 7, 8), ('A', 8, 10)]
    assert result['per_process'] == {
        'A': {'first_start': 0, 'response': 0, 'completion': 10, 'turnaround': 10},
        'B': {'first_start': 2, 'response': 1, 'completion': 8, 'turnaround': 7},
        'C': {'first_start': 4, 'response': 2, 'completion': 5, 'turnaround': 3}}
    assert result['averages'] == {'response': {'numerator': 1, 'denominator': 1},
                                   'turnaround': {'numerator': 20, 'denominator': 3}}
    assert result['switch_count'] == 5 and result['overhead'] == 0
    assert payload == before
    assert_conservation(payload, result)


def test_q016_stcf_hand_timeline():
    payload = {'policy': 'stcf', 'processes': [process('A', 0, 6), process('B', 1, 3), process('C', 2, 1)]}
    result = run(payload)
    assert [(row['process_id'], row['start'], row['end']) for row in result['timeline']] == [
        ('A', 0, 1), ('B', 1, 2), ('C', 2, 3), ('B', 3, 5), ('A', 5, 10)]
    assert result['averages'] == {'response': {'numerator': 0, 'denominator': 1},
                                   'turnaround': {'numerator': 5, 'denominator': 1}}
    assert result['switch_count'] == 4
    assert_conservation(payload, result)


@pytest.mark.parametrize('q,first,completion,switches', [
    (1, [0, 3, 7], [22, 16, 20], 10),
    (3, [0, 4, 12], [11, 7, 15], 3),
    (6, [0, 7, 11], [6, 10, 14], 2),
])
def test_q032_existing_waiter_stays_before_switch_time_arrival(q, first, completion, switches):
    payload = rr([process('A', 0, 6), process('B', 2, 3), process('C', 4, 3)], quantum=q, switch_cost=1)
    result = run(payload)
    assert [row['first_start'] for row in result['per_process'].values()] == first
    assert [row['completion'] for row in result['per_process'].values()] == completion
    assert result['switch_count'] == result['overhead'] == switches
    assert_conservation(payload, result)


@pytest.mark.parametrize('policy', ['rr', 'stcf'])
def test_initial_and_intermediate_idle_dispatch_are_free(policy):
    jobs = [process('A', 5, 2), process('B', 10, 1)]
    payload = rr(jobs, switch_cost=20) if policy == 'rr' else {'policy': policy, 'processes': jobs}
    result = run(payload)
    assert result['timeline'] == [
        {'kind': 'idle', 'start': 0, 'end': 5},
        {'kind': 'run', 'process_id': 'A', 'start': 5, 'end': 7},
        {'kind': 'idle', 'start': 7, 'end': 10},
        {'kind': 'run', 'process_id': 'B', 'start': 10, 'end': 11}]
    assert result['switch_count'] == result['overhead'] == 0
    assert result['makespan'] == 11
    assert result['averages']['turnaround'] == {'numerator': 3, 'denominator': 2}
    assert_conservation(payload, result)


def test_single_process_continuation_never_charged():
    payload = rr([process('A', 0, 5)], quantum=2, switch_cost=20)
    result = run(payload)
    assert result['timeline'] == [{'kind': 'run', 'process_id': 'A', 'start': 0, 'end': 5}]
    assert result['switch_count'] == result['overhead'] == 0
    assert_conservation(payload, result)


def test_boundary_order_changes_schedule_without_overriding_existing_waiters():
    jobs = [process('A', 0, 3), process('B', 1, 1)]
    before, after = run(rr(jobs, quantum=1)), run(rr(jobs, quantum=1, boundary='after'))
    assert before['per_process']['B']['first_start'] == 1
    assert after['per_process']['B']['first_start'] == 2
    tied = run(rr([process('Z', 0, 1), process('A', 0, 1)], quantum=1))
    assert [row['process_id'] for row in tied['timeline']] == ['A', 'Z']


def test_switch_time_arrival_policy_and_early_finish_exception_are_explicit():
    jobs = [process('A', 0, 6), process('B', 1, 3), process('C', 2, 1)]
    tail = run(rr(jobs, quantum=1, switch_cost=1))
    ahead = run(rr(jobs, quantum=1, switch_cost=1, during_switch='before_preempted'))
    assert tail['per_process']['C']['first_start'] == 6
    assert ahead['per_process']['C']['first_start'] == 4
    payload = rr([process('A', 0, 6), process('B', 1, 3), process('C', 2, 2)],
                 quantum=4, switch_cost=1, early_finish_free=True)
    free = run(payload)
    assert free['switch_count'] == 3 and free['overhead'] == 1
    assert [row['completion'] for row in free['per_process'].values()] == [12, 8, 10]
    assert_conservation(payload, free)


def test_stcf_equal_remaining_uses_id_even_if_it_preempts_current_process():
    payload = {'policy': 'stcf', 'processes': [process('B', 0, 3), process('A', 1, 2)]}
    result = run(payload)
    assert [(row['process_id'], row['start'], row['end']) for row in result['timeline']] == [
        ('B', 0, 1), ('A', 1, 3), ('B', 3, 5)]
    assert result['switch_count'] == 2
    assert_conservation(payload, result)


@pytest.mark.parametrize('field,value', [
    ('arrival', -1), ('arrival', 101), ('arrival', True), ('arrival', '0'), ('arrival', 1.5),
    ('burst', -1), ('burst', 0), ('burst', 101), ('burst', True), ('burst', '1'),
    ('id', ''), ('id', '   '), ('id', 'x' * 33), ('id', 1),
])
def test_invalid_process_fields_are_rejected(field, value):
    jobs = [process('A', 0, 1) | {field: value}]
    with pytest.raises(ValueError, match='Invalid cpu_schedule_v1 payload'):
        run(rr(jobs))


@pytest.mark.parametrize('field,value', [
    ('quantum', 0), ('quantum', -1), ('quantum', 101), ('quantum', True), ('quantum', '2'),
    ('switch_cost', -1), ('switch_cost', 21), ('switch_cost', False), ('switch_cost', 0.5),
    ('boundary', 'random'), ('during_switch', 'jump_to_front'), ('early_finish_free', 1),
    ('early_finish_free', 'false'), ('extra', 'ignored'),
])
def test_invalid_rr_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        run(rr([process('A', 0, 1)], **{field: value}))


def test_count_duplicates_unknown_tool_and_stcf_restrictions():
    invalid_jobs = [[], [process(str(i), 0, 1) for i in range(9)],
                    [process('A', 0, 1), process(' A ', 2, 1)]]
    for jobs in invalid_jobs:
        with pytest.raises(ValueError):
            run(rr(jobs))
    for extra in ({'switch_cost': 1}, {'switch_cost': False}, {'quantum': 2}, {'boundary': 'before'}):
        with pytest.raises(ValueError):
            run({'policy': 'stcf', 'processes': [process('A', 0, 1)], **extra})
    with pytest.raises(ValueError, match='Unknown answer tool'):
        run_answer_tool('__import__', {})
    with pytest.raises(ValueError, match='must be an object'):
        run_answer_tool('cpu_schedule_v1', [])
    bad = rr([process('A', 0, 1)])
    del bad['quantum']
    with pytest.raises(ValueError):
        run(bad)


def test_maximum_bounded_workload_and_public_contract():
    payload = rr([process(str(index), 100, 100) for index in range(8)], quantum=1, switch_cost=20)
    result = run(payload)
    assert result['switch_count'] == 799
    assert result['overhead'] == 15980
    assert result['makespan'] == 16880
    assert_conservation(payload, result)
    schema = TOOL_CONTRACTS['cpu_schedule_v1']['input_schema']
    assert schema['discriminator']['propertyName'] == 'policy'
    assert all(definition.get('additionalProperties') is False for definition in schema['$defs'].values())
    assert 'payload matches' in TOOL_CONTRACTS['cpu_schedule_v1']['limitations'][0]
