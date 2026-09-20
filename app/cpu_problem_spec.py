"""Compile bounded scheduling specifications into questions and exact answers.

The author supplies data and supported choices, never numerical prose. This
compiler establishes consistency with ``cpu_schedule_v1`` for those inputs; it
does not establish source support, pedagogical quality, or learner difficulty.
"""
from fractions import Fraction
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

from .answer_tools import TOOL_CONTRACTS, run_answer_tool
from .models import Question, Section


CPU_SPEC_REVISION = 'cpu-problem-spec-v2-20260919'
MAX_TIMELINE_INTERVALS = 240
_ID = Annotated[str, Field(strict=True, pattern=r'^[A-Za-z][A-Za-z0-9_]{0,15}$')]
_Metric = Literal['mean_response', 'mean_turnaround', 'makespan', 'overhead', 'switch_count']


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class _Process(_StrictModel):
    id: _ID
    arrival: Annotated[StrictInt, Field(ge=0, le=100)]
    burst: Annotated[StrictInt, Field(ge=1, le=100)]


class _RR(_StrictModel):
    id: _ID
    policy: Literal['rr']
    quantum: Annotated[StrictInt, Field(ge=1, le=100)]
    switch_cost: Annotated[StrictInt, Field(ge=0, le=20)]
    boundary: Literal['before', 'after']
    during_switch: Literal['tail', 'before_preempted']
    early_finish_free: StrictBool


class _STCF(_StrictModel):
    id: _ID
    policy: Literal['stcf']
    switch_cost: Annotated[StrictInt, Field(ge=0, le=0)] = 0


class _Remaining(_StrictModel):
    scenario_id: _ID
    process_id: _ID
    time: Annotated[StrictInt, Field(ge=0, le=17000)]


class _Rational(_StrictModel):
    numerator: Annotated[StrictInt, Field(ge=0, le=100000)]
    denominator: Annotated[StrictInt, Field(ge=1, le=10000)]


class _Constraint(_StrictModel):
    metric: _Metric
    operator: Literal['le', 'lt', 'ge', 'gt', 'eq']
    threshold: _Rational


class _Selection(_StrictModel):
    metric: _Metric
    direction: Literal['min', 'max']
    constraints: Annotated[list[_Constraint], Field(max_length=4)] = Field(default_factory=list)


class _Spec(_StrictModel):
    processes: Annotated[list[_Process], Field(min_length=1, max_length=8)]
    scenarios: Annotated[list[Annotated[_RR | _STCF, Field(discriminator='policy')]],
                         Field(min_length=1, max_length=4)]
    remaining_queries: Annotated[list[_Remaining], Field(max_length=8)] = Field(default_factory=list)
    selection: _Selection | None = None

    @model_validator(mode='after')
    def valid_references(self):
        processes = {item.id for item in self.processes}
        scenarios = {item.id for item in self.scenarios}
        if len(processes) != len(self.processes) or len(scenarios) != len(self.scenarios):
            raise ValueError('Duplicate identifiers.')
        queries = set()
        for item in self.remaining_queries:
            key = (item.scenario_id, item.process_id, item.time)
            if item.scenario_id not in scenarios or item.process_id not in processes or key in queries:
                raise ValueError('Invalid remaining-service query.')
            queries.add(key)
        return self


def cpu_spec_contract() -> dict:
    """Return a fresh JSON schema; language, target and citations are controller-owned."""
    schema = _Spec.model_json_schema()
    schema['description'] = (
        'A single short-answer CPU scheduling problem. All scenarios share the processes. '
        'The compiler asks and solves each full timeline, per-process first start, response, '
        'completion and turnaround, exact mean response/turnaround, switch count, overhead, '
        'and makespan. Optional remaining_queries ask unfinished service at a time; selection '
        'asks all feasible optima across all scenarios with exact rational thresholds. '
        'No prose or external language/difficulty/citation fields are permitted. '
        'All scenarios together must produce at most 240 timeline intervals. '
        'No I/O, priorities, multiple CPUs, noninteger service times or STCF switch cost.'
    )
    return schema


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _fraction(value):
    return Fraction(value['numerator'], value['denominator'])


def _exact(value):
    value = Fraction(value)
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _number(value):
    value = Fraction(value)
    return str(value.numerator) if value.denominator == 1 else f'{value.numerator}/{value.denominator}'


def _metric(result, metric):
    if metric.startswith('mean_'):
        return _fraction(result['averages'][metric.removeprefix('mean_')])
    return Fraction(result[metric])


def _label(metric, zh):
    labels = {
        'mean_response': ('平均响应时间', 'mean response time'),
        'mean_turnaround': ('平均周转时间', 'mean turnaround time'),
        'makespan': ('最终完成时刻', 'makespan'),
        'overhead': ('切换总开销', 'total switching overhead'),
        'switch_count': ('进程切换次数', 'process switch count'),
    }
    return labels[metric][0 if zh else 1]


_SIGNS = {'le': '<=', 'lt': '<', 'ge': '>=', 'gt': '>', 'eq': '='}


def _satisfies(left, operator, right):
    return {'le': left <= right, 'lt': left < right, 'ge': left >= right,
            'gt': left > right, 'eq': left == right}[operator]


def _rules(scenario, zh):
    if scenario['policy'] == 'stcf':
        return ('STCF；切换开销为 0。每个整数时刻在已到达且未完成的进程中，依次按剩余服务时间、进程 ID 字典序选最小者；'
                '剩余服务时间相等时，ID 较小者也会抢占当前进程。' if zh else
                'STCF; switching cost is 0. At each integer time choose the arrived unfinished process '
                'with the smallest (remaining service, lexicographic process ID); a smaller ID can '
                'preempt the current process even when remaining service is equal.')
    quantum, cost = scenario['quantum'], scenario['switch_cost']
    before = scenario['boundary'] == 'before'
    tail = scenario['during_switch'] == 'tail'
    free = scenario['early_finish_free']
    if zh:
        boundary = '之前' if before else '之后'
        arrivals = ('追加到队尾' if tail else '插入刚被抢占且仍在队列中的进程之前；若该进程已不在队列，则追加到队尾')
        exception = ('仅当前一进程在刚结束的运行片段中完成，且该片段长度严格小于时间片时，免除随后切换到不同进程的开销。'
                     if free else '提前完成不会免除随后切换到不同进程的开销。')
        return (f'RR；时间片 q={quantum}，不同进程间切换开销={cost}。运行片段结束前到达的进程先入队；'
                f'恰在片段结束时刻到达的进程，排在刚重新入队的未完成进程{boundary}，原有等待者保持次序。'
                f'切换前已选定下一进程；切换期间（含结束时刻）到达的进程{arrivals}，随后才开始执行已选定进程。{exception}')
    boundary = 'before' if before else 'after'
    arrivals = ('appended to the queue tail' if tail else
                'inserted immediately before the just-preempted process if it is still queued, otherwise appended to the tail')
    exception = ('Waive the next different-process switching cost only when the previous process completed '
                 'in its just-ended run slice and that slice was strictly shorter than a full quantum.' if free else
                 'Finishing before a full quantum does not waive the next different-process switching cost.')
    return (f'RR; quantum q={quantum}, different-process switching cost={cost}. Arrivals before a slice end '
            f'are queued first; arrivals exactly at the slice end are queued {boundary} the unfinished '
            'process is requeued, while existing waiters keep their order. Select the next process before '
            f'switching; arrivals during switching, including its end, are {arrivals} before the selected '
            f'process starts. {exception}')


def _base_rules(zh):
    if zh:
        return ('各进程彼此独立，只有一次 CPU 服务且无 I/O；使用单处理器和整数抽象时间单位。'
                '表中服务量是本练习预先给定的已知输入，不是调度器对真实工作负载的预测。'
                '进程完成后立即结束运行，不消耗当前时间片尚未使用的部分；是否免除随后切换费用另按场景规则判断。'
                '同时到达按进程 ID 字典序入队。首次调度、空闲后的调度和同一进程连续执行均无切换开销。'
                '响应时间=首次开始时刻−到达时刻；周转时间=完成时刻−到达时刻。'
                '最终完成时刻从 t=0 计，包含所有空闲和切换时间。进程切换次数计相邻不同进程的转换，'
                '包含免费转换，不含首次或空闲后的调度。时间线使用左闭右开区间；没有切换或空闲隔开的同进程运行区间合并显示。')
    return ('Processes are independent, have a single CPU burst and no I/O, on one processor with integer '
            'abstract time units. The listed burst lengths are known inputs supplied for this exercise, '
            'not predictions of a real workload. A completed process stops immediately without consuming '
            'the unused part of its quantum; any waiver of the following switching cost is a separate '
            'scenario rule. Simultaneous arrivals use lexicographic process ID order. Initial dispatch, '
            'dispatch after idle, and same-process continuation are free. Response time = first start − arrival; '
            'turnaround time = completion − arrival. Makespan is final completion measured from t=0, including '
            'all idle and switching time. Switch count includes adjacent different-process transitions, even '
            'free transitions, and excludes initial/after-idle dispatch. Timeline intervals are half-open '
            '[start, end); consecutive same-process runs are merged unless separated by switching or idle.')


def _timeline(result, zh):
    parts = []
    for row in result['timeline']:
        if row['kind'] == 'run':
            name = row['process_id']
        elif row['kind'] == 'idle':
            name = '空闲' if zh else 'idle'
        else:
            name = ('切换 ' if zh else 'switch ') + row['from_process_id'] + '→' + row['to_process_id']
        parts.append(f"[{row['start']}, {row['end']}): {name}")
    return '; '.join(parts)


def _selection_rationale(spec, results, claims, zh):
    """Explain the frozen calculations without introducing model-authored facts.

    Feasibility and optima reuse existing checked claims. Metric comparisons
    describe the finite candidates only, independently of quantum ordering.
    """
    selection = next(claim for claim in claims if claim['type'] == 'optimal_selection')
    metrics = ('mean_response', 'mean_turnaround', 'overhead', 'switch_count')
    scenario_ids = [scenario['id'] for scenario in spec['scenarios']]
    lines = []
    for sid in scenario_ids:
        values = '; '.join(f'{_label(name, zh)}={_number(_metric(results[sid], name))}' for name in metrics)
        failures = [claim for claim in claims if claim['type'] == 'threshold_comparison'
                    and claim['scenario_id'] == sid and not claim['satisfied']]
        if failures:
            failed = '; '.join(f"{_label(claim['metric'], zh)} {_number(_fraction(claim['value']))} "
                               f"{_SIGNS[claim['operator']]} {_number(_fraction(claim['threshold']))}"
                               for claim in failures)
            status = (f'不可行；以下要求不满足：{failed}' if zh else
                      f'infeasible; these required comparisons fail: {failed}')
        else:
            status = '可行，满足全部给定约束' if zh else 'feasible under all stated constraints'
        lines.append(f'{sid}: {values} → {status}.')

    best = selection['optimal_scenario_ids']
    direction = ('最小化' if selection['direction'] == 'min' else '最大化') if zh else (
        'minimize' if selection['direction'] == 'min' else 'maximize')
    goal = direction + (' ' if not zh else '') + _label(selection['metric'], zh)
    if best:
        feasible = ', '.join(selection['feasible_scenario_ids'])
        optimum = _number(_fraction(selection['optimal_value']))
        lines.append((f'先以约束筛出可行方案 {feasible}，再按“{goal}”比较；全部最优方案为 '
                      f"{', '.join(best)}，目标值为 {optimum}。" if zh else
                      f'First filter to the feasible scenarios {feasible}, then {goal}; all optimal '
                      f"scenarios are {', '.join(best)}, with objective value {optimum}."))
    else:
        lines.append((f'所有候选均违反至少一项约束，不能在这些约束下给出实现“{goal}”的可行推荐。'
                      '需修改约束或候选并重新计算，不能直接把一个不可行方案称为最优。' if zh else
                      f'Every candidate violates at least one constraint, so there is no feasible '
                      f'recommendation to {goal} under these constraints. Revise the constraints or '
                      'candidates and recompute instead of calling an infeasible candidate optimal.'))

    # These four response/latency/overhead/count metrics are compared in the
    # lower-is-better direction solely to describe actual observed conflicts;
    # the requested selection objective and direction above remain unchanged.
    comparison_metrics = ('mean_response', 'overhead', 'mean_turnaround', 'switch_count')
    conflicts, dominances = [], []
    for index, left in enumerate(scenario_ids):
        for right in scenario_ids[index + 1:]:
            lower = [m for m in comparison_metrics if _metric(results[left], m) < _metric(results[right], m)]
            higher = [m for m in comparison_metrics if _metric(results[left], m) > _metric(results[right], m)]
            if lower and higher:
                conflicts.append((left, right, lower[0], higher[0]))
            elif lower:
                dominances.append((left, right, lower[0]))
            elif higher:
                dominances.append((right, left, higher[0]))
    if conflicts:
        left, right, lower, higher = conflicts[0]
        low_values = f'{_number(_metric(results[left], lower))} < {_number(_metric(results[right], lower))}'
        high_values = f'{_number(_metric(results[left], higher))} > {_number(_metric(results[right], higher))}'
        lines.append((f'把上述四项指标分别按越小越好比较，当前候选确有取舍。例如 {left} 相比 {right}，'
                      f'{_label(lower, zh)}更小（{low_values}），但{_label(higher, zh)}更大（{high_values}）。'
                      '因此需要结合约束与指定目标判断，不能仅凭某一项指标选择。' if zh else
                      f'Comparing the four listed metrics in the lower-is-better direction reveals a real '
                      f'tradeoff among these candidates: {left} has lower {_label(lower, zh)} than {right} '
                      f'({low_values}), but higher {_label(higher, zh)} ({high_values}). Evaluate the '
                      'stated constraints and objective together rather than selecting on one metric alone.'))
    elif dominances:
        left, right, lower = dominances[0]
        values = f'{_number(_metric(results[left], lower))} < {_number(_metric(results[right], lower))}'
        lines.append((f'把上述四项指标分别按越小越好比较，这些候选没有呈现指标之间的冲突。'
                      f'例如 {left} 的四项指标均不大于 {right}，其中{_label(lower, zh)}更小（{values}）；'
                      '这里是支配关系，不应人为声称存在取舍。是否可选仍取决于全部约束与指定目标。' if zh else
                      f'Comparing the four listed metrics in the lower-is-better direction shows no '
                      f'conflict between these metrics among the candidates. For example, {left} is '
                      f'no worse than {right} on all four, with lower {_label(lower, zh)} ({values}); '
                      'this is dominance, not a tradeoff. Feasibility and the stated objective still determine selection.'))
    elif len(scenario_ids) > 1:
        lines.append('各候选的上述四项指标完全相同，没有观测到这些指标之间的取舍；最终仍按全部约束与指定目标判断。' if zh else
                     'All candidates have identical values for the four listed metrics, so no tradeoff '
                     'between these metrics is observed; all constraints and the stated objective still apply.')
    else:
        lines.append('这里只提供一个候选，无法据此观察方案之间的指标取舍。' if zh else
                     'Only one candidate is supplied, so this comparison cannot establish a tradeoff between alternatives.')
    lines.append('本结论仅适用于当前给定的工作负载、有限候选、目标和约束；平均响应更低或切换开销更低，单独都不足以证明某策略或时间片普遍最优。' if zh else
                 'This conclusion applies only to the supplied workload, finite candidates, objective and '
                 'constraints. Lower mean response or lower switching overhead alone does not establish '
                 'a universally optimal policy or quantum.')
    return lines


def compile_cpu_spec(raw: dict, *, language: str, difficulty: str, source_ids: list[str]) -> dict:
    """Validate, freeze, execute all cases, and deterministically render one question.

    Only sanitized errors leave this boundary. Inputs and citations are copied;
    modifying an input requires recompilation and produces fresh evidence.
    """
    if (language not in ('zh', 'en') or difficulty not in ('easy', 'medium', 'hard')
            or not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 10
            or any(not isinstance(item, str) or not item.strip() or len(item) > 200
                   or any(ord(char) < 32 for char in item) for item in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise ValueError('Invalid CPU problem controller context.')
    if not isinstance(raw, dict):
        raise ValueError('Invalid CPU problem specification; use the supported schema and bounded values.')
    try:
        model = _Spec.model_validate(raw)
    except ValidationError:
        raise ValueError('Invalid CPU problem specification; use the supported schema and bounded values.') from None
    spec = model.model_dump()
    spec['processes'].sort(key=lambda item: item['id'])
    if spec['selection']:
        for constraint in spec['selection']['constraints']:
            constraint['threshold'] = _exact(_fraction(constraint['threshold']))
    canonical = {'revision': CPU_SPEC_REVISION, 'language': language, 'difficulty': difficulty,
                 'citation_ids': list(source_ids), 'spec': spec,
                 'tool_contract_sha256': _digest(TOOL_CONTRACTS['cpu_schedule_v1'])}
    evidence = {'revision': CPU_SPEC_REVISION, 'spec_sha256': _digest(canonical),
                'canonical_spec': canonical, 'tool_results': [], 'claims': [],
                'scope': 'Deterministic calculations for the frozen inputs; not source or difficulty validation.'}
    results = {}
    interval_count = 0
    for scenario in spec['scenarios']:
        payload = {key: value for key, value in scenario.items() if key != 'id'}
        payload['processes'] = [dict(item) for item in spec['processes']]
        result = run_answer_tool('cpu_schedule_v1', payload)
        interval_count += len(result['timeline'])
        if interval_count > MAX_TIMELINE_INTERVALS:
            raise ValueError('CPU problem exceeds the total 240 timeline interval rendering limit.')
        results[scenario['id']] = result
        evidence['tool_results'].append({'scenario_id': scenario['id'],
                                        'request': {'name': 'cpu_schedule_v1', 'input': payload},
                                        'result': result})
        service = sum(item['burst'] for item in spec['processes'])
        idle = sum(row['end'] - row['start'] for row in result['timeline'] if row['kind'] == 'idle')
        verified = service + result['overhead'] + idle == result['makespan']
        for process in spec['processes']:
            served = sum(row['end'] - row['start'] for row in result['timeline']
                         if row['kind'] == 'run' and row['process_id'] == process['id'])
            verified = verified and served == process['burst']
        if not verified:
            raise ValueError('CPU problem calculation failed its conservation check.')
        evidence['claims'].append({'type': 'conservation', 'scenario_id': scenario['id'],
                                   'service': service, 'idle': idle, 'overhead': result['overhead'],
                                   'makespan': result['makespan'], 'verified': True})

    zh = language == 'zh'
    stem = [_base_rules(zh), '进程（到达时刻，服务时间）：' if zh else 'Processes (arrival, burst):']
    stem.extend(f"{item['id']}: ({item['arrival']}, {item['burst']})" for item in spec['processes'])
    for scenario in spec['scenarios']:
        stem.append(f"{scenario['id']}: {_rules(scenario, zh)}")
    stem.append('对每个方案，给出完整时间线、各进程首次开始时刻/响应时间/完成时刻/周转时间、平均响应时间、平均周转时间、切换次数、切换总开销和最终完成时刻。平均值保留精确分数。'
                if zh else 'For every scenario, give the complete timeline; each process’s first start, response, completion and turnaround; mean response, mean turnaround, switch count, total switching overhead and makespan. Use exact fractions for averages.')
    answer = []
    explanation = [
        ('表中服务量是本练习预先给定的已知输入。进程完成后立即结束运行，不消耗时间片剩余部分；'
         '随后切换是否免费，必须另按场景规定判断。' if zh else
         'The listed burst lengths are known inputs supplied for this exercise. A completed process '
         'stops immediately without consuming the unused part of its quantum; whether the following '
         'switch is free must be determined separately from the scenario rules.')]
    for scenario in spec['scenarios']:
        sid, result = scenario['id'], results[scenario['id']]
        answer.append(f"{sid}: {_timeline(result, zh)}")
        answer.append('进程 | 首次开始 | 响应 | 完成 | 周转' if zh else 'Process | First start | Response | Completion | Turnaround')
        for pid, values in result['per_process'].items():
            answer.append(f"{pid} | {values['first_start']} | {values['response']} | {values['completion']} | {values['turnaround']}")
        answer.append('; '.join(f'{_label(metric, zh)}={_number(_metric(result, metric))}'
                                for metric in ('mean_response', 'mean_turnaround', 'switch_count', 'overhead', 'makespan')))
        conservation = next(item for item in evidence['claims'] if item['scenario_id'] == sid)
        explanation.append((f"{sid}：总服务 {conservation['service']} + 空闲 {conservation['idle']} + 切换开销 {result['overhead']} = 最终完成时刻 {result['makespan']}。" if zh else
                            f"{sid}: service {conservation['service']} + idle {conservation['idle']} + switching overhead {result['overhead']} = makespan {result['makespan']}."))

    jobs = {item['id']: item for item in spec['processes']}
    for query in spec['remaining_queries']:
        sid, pid, time = query['scenario_id'], query['process_id'], query['time']
        result = results[sid]
        served = sum(max(0, min(time, row['end']) - row['start']) for row in result['timeline']
                     if row['kind'] == 'run' and row['process_id'] == pid and row['start'] < time)
        remaining = jobs[pid]['burst'] - served
        stem.append(f"在 {sid} 中，t={time} 时 {pid} 还剩多少 CPU 服务量？只计 [0, {time}) 内已执行的服务；未到达的进程也保留其全部服务量。" if zh else
                    f"In {sid}, how much CPU service remains for {pid} at t={time}? Count only execution in [0, {time}); a process not yet arrived still has its entire burst remaining.")
        claim = {'type': 'remaining_service', **query, 'burst': jobs[pid]['burst'],
                 'executed_service': served, 'remaining_service': remaining, 'verified': True}
        evidence['claims'].append(claim)
        text = (f"{sid}, {pid}, t={time}：剩余服务={jobs[pid]['burst']}−{served}={remaining}。" if zh else
                f"{sid}, {pid}, t={time}: remaining service={jobs[pid]['burst']}−{served}={remaining}.")
        answer.append(text)
        explanation.append(text)

    selection = spec['selection']
    if selection:
        metric, direction = selection['metric'], selection['direction']
        constraints = selection['constraints']
        terms = [f"{_label(item['metric'], zh)} {_SIGNS[item['operator']]} {_number(_fraction(item['threshold']))}" for item in constraints]
        condition = (' 且 ' if zh else ' and ').join(terms) or ('无额外约束' if zh else 'no additional constraints')
        stem.append((f"在满足「{condition}」的方案中，选择{_label(metric, zh)}{'最小' if direction == 'min' else '最大'}的方案，逐项说明约束判断；并列最优时列出全部方案，无可行方案时明确说明。" if zh else
                     f"Among scenarios satisfying {condition}, {'minimize' if direction == 'min' else 'maximize'} {_label(metric, zh)}. Show every constraint check, report all tied optima, or state that no scenario is feasible.") )
        feasible = []
        for scenario in spec['scenarios']:
            sid, result = scenario['id'], results[scenario['id']]
            checks = []
            for constraint in constraints:
                left = _metric(result, constraint['metric'])
                right = _fraction(constraint['threshold'])
                satisfied = _satisfies(left, constraint['operator'], right)
                checks.append(satisfied)
                claim = {'type': 'threshold_comparison', 'scenario_id': sid, 'metric': constraint['metric'],
                         'operator': constraint['operator'], 'value': _exact(left), 'threshold': _exact(right),
                         'satisfied': satisfied, 'verified': True}
                evidence['claims'].append(claim)
                status = ('满足' if satisfied else '不满足') if zh else ('satisfied' if satisfied else 'not satisfied')
                answer.append(f"{sid}: {_label(constraint['metric'], zh)} {_number(left)} {_SIGNS[constraint['operator']]} {_number(right)} → {status}")
            if all(checks):
                feasible.append((sid, _metric(result, metric)))
        optimum = (min if direction == 'min' else max)(value for _, value in feasible) if feasible else None
        best = [sid for sid, value in feasible if value == optimum]
        evidence['claims'].append({'type': 'optimal_selection', 'metric': metric, 'direction': direction,
                                   'feasible_scenario_ids': [sid for sid, _ in feasible], 'optimal_scenario_ids': best,
                                   'optimal_value': _exact(optimum) if optimum is not None else None,
                                   'verified': True})
        if best:
            answer.append((f"最优方案：{', '.join(best)}；{_label(metric, zh)}={_number(optimum)}。" if zh else
                           f"Optimal scenarios: {', '.join(best)}; {_label(metric, zh)}={_number(optimum)}."))
        else:
            answer.append('无可行方案。' if zh else 'No scenario is feasible.')
        stem.append('最后结合已算出的指标论证选择：说明候选间实际存在的指标取舍；若各指标同向改善或相同，应如实说明没有观测到相应冲突。说明为何本题结论不能推广为对所有工作负载和目标都最优的策略。'
                    if zh else 'Finally justify the selection using the computed metrics: explain the '
                    'tradeoffs actually present between candidates, or state when metrics improve together '
                    'or remain equal without an observed conflict. Explain why this result does not '
                    'establish a policy that is optimal for every workload and objective.')
        explanation.extend(_selection_rationale(spec, results, evidence['claims'], zh))

    steps = (['根据指定规则排列运行与切换区间。', '由首次开始及完成时刻计算进程指标与精确平均数。',
              '核对服务量守恒及题目要求的状态或约束判断。'] if zh else
             ['Order execution and switching intervals using the stated rules.',
              'Compute process metrics and exact averages from first starts and completions.',
              'Check service conservation and the requested state or constraint judgments.'])
    if difficulty == 'easy':
        steps = steps[:2]
    question = Question(slot_id='q1', difficulty=difficulty, kind='short_answer', stem='\n\n'.join(stem),
                        answer='\n'.join(answer), explanation='\n'.join(explanation),
                        difficulty_design={'cognitive_process': {'easy': 'understand', 'medium': 'apply', 'hard': 'analyze'}[difficulty],
                                           'concepts': ['CPU 调度', '响应时间与周转时间'] if zh else ['CPU scheduling', 'Response and turnaround time'],
                                           'expected_steps': steps},
                        difficulty_reason=('目标难度由请求指定；执行轨迹和指标已计算，实际难度仍需独立评估。' if zh else
                                           'The request sets the target difficulty. Traces and metrics are computed; actual difficulty still requires independent assessment.'),
                        citation_ids=list(source_ids)).model_dump()
    section = Section(heading='调度指标与约定' if zh else 'Scheduling metrics and conventions',
                      text=_base_rules(zh), citation_ids=list(source_ids)).model_dump()
    return {'question': question, 'section': section,
            'learning_objectives': ['根据明确的调度规则计算时间线与进程指标。'] if zh else
                                   ['Compute execution timelines and process metrics under explicit scheduling rules.'],
            'evidence': evidence}
