"""Bounded deterministic answer tools; no code evaluation, I/O or model calls.

Results validate the supplied structured payload only. A separate reviewer must
check that the payload matches the question and that the answer uses the result.
"""
from collections import deque
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, TypeAdapter, ValidationError, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class _Process(_StrictModel):
    id: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    arrival: Annotated[StrictInt, Field(ge=0, le=100)]
    burst: Annotated[StrictInt, Field(ge=1, le=100)]


class _ScheduleInput(_StrictModel):
    processes: Annotated[list[_Process], Field(min_length=1, max_length=8,
        description='Distinct nonempty process ids; simultaneous arrivals use id lexicographic order.')]

    @model_validator(mode='after')
    def unique_ids(self):
        if len({process.id for process in self.processes}) != len(self.processes):
            raise ValueError('Process ids must be unique after trimming surrounding whitespace.')
        return self


class _RoundRobinInput(_ScheduleInput):
    policy: Literal['rr']
    quantum: Annotated[StrictInt, Field(ge=1, le=100)]
    switch_cost: Annotated[StrictInt, Field(ge=0, le=20)]
    boundary: Literal['before', 'after']
    during_switch: Literal['tail', 'before_preempted']
    early_finish_free: StrictBool


class _STCFInput(_ScheduleInput):
    policy: Literal['stcf']
    switch_cost: Annotated[StrictInt, Field(ge=0, le=0)] = 0


_INPUT = TypeAdapter(Annotated[_RoundRobinInput | _STCFInput, Field(discriminator='policy')])

TOOL_CONTRACTS = {
    'cpu_schedule_v1': {
        'name': 'cpu_schedule_v1',
        'description': (
            'Deterministic single-processor RR or STCF scheduling with independent, single-burst processes and no I/O. '
            'Times are integer abstract time units. Initial dispatch and dispatch after an idle interval are free. '
            'RR boundary=before/after orders arrivals exactly at a slice end before/after the unfinished process is requeued; '
            'earlier waiting processes keep their position. during_switch=tail appends switch-time arrivals to the queue; '
            'before_preempted inserts them immediately before the just-preempted process if it is still queued, otherwise at the tail. '
            'The next process is selected before switching, and arrivals through the switch end are enqueued before it starts. '
            'early_finish_free waives the next different-process switch cost only if the previous process completed in less than a full quantum. '
            'Same-process continuation is free. STCF supports zero switch cost only and chooses (remaining time, id), '
            'including id-based preemption on equal remaining times. Id comparison is Unicode lexicographic order. '
            'switch_count counts adjacent different-process transitions, including free early-finish transitions, but excludes initial/idle dispatch. '
            'makespan is final completion measured from time zero, including initial and intermediate idle intervals. '
            'Consecutive run intervals for the same process without an intervening switch/idle interval are merged.'
        ),
        'input_schema': _INPUT.json_schema(),
        'limitations': [
            'The result proves only arithmetic for this payload; it does not establish that the payload matches the natural-language question.',
            'A separate review must check the question-to-input binding, source support, and the answer against these results.',
            'No support for I/O, priorities, multiple processors, arbitrary tie rules, noninteger times, or STCF switch overhead.',
            'At most eight processes and 800 units of CPU service; no arbitrary code, file access, network access or model invocation.',
        ],
    },
}


def _run_interval(timeline, process_id, start, end):
    if timeline and timeline[-1]['kind'] == 'run' and timeline[-1]['process_id'] == process_id and timeline[-1]['end'] == start:
        timeline[-1]['end'] = end
    else:
        timeline.append({'kind': 'run', 'process_id': process_id, 'start': start, 'end': end})


def _round_robin(data):
    jobs = {process.id: process for process in data.processes}
    remaining = {process.id: process.burst for process in data.processes}
    pending = deque(sorted(jobs, key=lambda key: (jobs[key].arrival, key)))
    ready = deque()
    timeline = []
    time = 0
    previous = None
    previous_early_finish = False
    switch_count = 0

    def arrive(until, inclusive=True, anchor=None):
        while pending and (jobs[pending[0]].arrival <= until if inclusive else jobs[pending[0]].arrival < until):
            process_id = pending.popleft()
            if anchor in ready:
                ready.insert(ready.index(anchor), process_id)
            else:
                ready.append(process_id)

    arrive(0)
    while ready or pending:
        if not ready:
            next_arrival = jobs[pending[0]].arrival
            if next_arrival > time:
                timeline.append({'kind': 'idle', 'start': time, 'end': next_arrival})
                time = next_arrival
                previous = None
                previous_early_finish = False
            arrive(time)
        process_id = ready.popleft()
        if previous is not None and previous != process_id:
            switch_count += 1
            cost = 0 if data.early_finish_free and previous_early_finish else data.switch_cost
            if cost:
                timeline.append({'kind': 'switch', 'from_process_id': previous,
                                 'to_process_id': process_id, 'start': time, 'end': time + cost})
                anchor = previous if data.during_switch == 'before_preempted' and remaining[previous] else None
                arrive(time + cost, anchor=anchor)
                time += cost
        duration = min(data.quantum, remaining[process_id])
        end = time + duration
        _run_interval(timeline, process_id, time, end)
        remaining[process_id] -= duration
        arrive(end, inclusive=False)
        if data.boundary == 'before':
            arrive(end)
        if remaining[process_id]:
            ready.append(process_id)
        if data.boundary == 'after':
            arrive(end)
        previous = process_id
        previous_early_finish = remaining[process_id] == 0 and duration < data.quantum
        time = end
    return timeline, switch_count


def _stcf(data):
    jobs = {process.id: process for process in data.processes}
    remaining = {process.id: process.burst for process in data.processes}
    timeline = []
    time = 0
    previous = None
    switch_count = 0
    # All inputs are bounded integers. At most 800 service iterations plus at
    # most eight idle jumps; each unit covers every possible arrival event.
    while any(remaining.values()):
        ready = [key for key in jobs if jobs[key].arrival <= time and remaining[key]]
        if not ready:
            next_arrival = min(jobs[key].arrival for key in jobs if remaining[key])
            timeline.append({'kind': 'idle', 'start': time, 'end': next_arrival})
            time = next_arrival
            previous = None
            continue
        process_id = min(ready, key=lambda key: (remaining[key], key))
        if previous is not None and previous != process_id:
            switch_count += 1
        _run_interval(timeline, process_id, time, time + 1)
        remaining[process_id] -= 1
        previous = process_id
        time += 1
    return timeline, switch_count


def _result(data, timeline, switch_count):
    runs = [item for item in timeline if item['kind'] == 'run']
    per_process = {}
    for process in sorted(data.processes, key=lambda item: item.id):
        own = [item for item in runs if item['process_id'] == process.id]
        first = own[0]['start']
        completion = own[-1]['end']
        per_process[process.id] = {'first_start': first, 'response': first - process.arrival,
                                   'completion': completion, 'turnaround': completion - process.arrival}
    averages = {}
    for metric in ('response', 'turnaround'):
        exact = Fraction(sum(item[metric] for item in per_process.values()), len(per_process))
        averages[metric] = {'numerator': exact.numerator, 'denominator': exact.denominator}
    overhead = sum(item['end'] - item['start'] for item in timeline if item['kind'] == 'switch')
    return {'tool': 'cpu_schedule_v1', 'policy': data.policy, 'unit': 'abstract_time_units',
            'timeline': timeline, 'per_process': per_process, 'averages': averages,
            'switch_count': switch_count, 'overhead': overhead, 'makespan': timeline[-1]['end']}


def run_answer_tool(name: str, payload: dict) -> dict:
    """Run an allowlisted tool, rejecting malformed input before computation.

    ValueError messages do not echo input or arbitrary validation exceptions.
    No settings, credentials, files, subprocesses or network APIs are accessed.
    """
    if name != 'cpu_schedule_v1':
        raise ValueError('Unknown answer tool.')
    if not isinstance(payload, dict):
        raise ValueError('Answer tool payload must be an object.')
    try:
        data = _INPUT.validate_python(payload)
    except ValidationError:
        raise ValueError('Invalid cpu_schedule_v1 payload; check the tool input schema and unique process ids.') from None
    timeline, switch_count = _round_robin(data) if data.policy == 'rr' else _stcf(data)
    return _result(data, timeline, switch_count)
