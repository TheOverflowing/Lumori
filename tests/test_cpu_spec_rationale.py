"""Bilingual deterministic explanations for independently hand-checkable inputs."""
from copy import deepcopy

import pytest

from app.cpu_problem_spec import CPU_SPEC_REVISION, compile_cpu_spec


PROCESSES = [{'id': 'A', 'arrival': 0, 'burst': 3}, {'id': 'B', 'arrival': 0, 'burst': 1}]


def rr(name, quantum):
    return {'id': name, 'policy': 'rr', 'quantum': quantum, 'switch_cost': 1,
            'boundary': 'before', 'during_switch': 'tail', 'early_finish_free': True}


def threshold(metric='overhead', operator='le', value=2):
    return {'metric': metric, 'operator': operator,
            'threshold': {'numerator': value, 'denominator': 1}}


def compile_case(language, *, scenarios=None, constraints=None, metric='mean_response', direction='min'):
    return compile_cpu_spec({
        'processes': deepcopy(PROCESSES),
        'scenarios': scenarios if scenarios is not None else [rr('small', 1), rr('large', 3)],
        'selection': {'metric': metric, 'direction': direction,
                      'constraints': constraints if constraints is not None else [threshold()]},
    }, language=language, difficulty='hard', source_ids=['fixture'])


def optimum(result):
    return next(claim for claim in result['evidence']['claims'] if claim['type'] == 'optimal_selection')


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_real_tradeoff_uses_exact_computed_values_with_all_candidates_feasible(language):
    result = compile_case(language)
    text = result['question']['explanation']
    # Hand trace: q1 A[0,1), switch[1,2), B[2,3), switch[3,4), A[4,6).
    # q3 A[0,3), switch[3,4), B[4,5).
    assert optimum(result)['feasible_scenario_ids'] == ['small', 'large']
    assert optimum(result)['optimal_scenario_ids'] == ['small']
    assert optimum(result)['optimal_value'] == {'numerator': 1, 'denominator': 1}
    if language == 'en':
        assert 'small: mean response time=1; mean turnaround time=9/2; total switching overhead=2; process switch count=2 → feasible' in text
        assert 'large: mean response time=2; mean turnaround time=4; total switching overhead=1; process switch count=1 → feasible' in text
        assert 'real tradeoff' in text and 'lower mean response time than large (1 < 2)' in text
        assert 'higher total switching overhead (2 > 1)' in text
        assert 'Finally justify the selection' in result['question']['stem']
    else:
        assert 'small: 平均响应时间=1; 平均周转时间=9/2; 切换总开销=2; 进程切换次数=2 → 可行' in text
        assert 'large: 平均响应时间=2; 平均周转时间=4; 切换总开销=1; 进程切换次数=1 → 可行' in text
        assert '当前候选确有取舍' in text and '平均响应时间更小（1 < 2）' in text
        assert '切换总开销更大（2 > 1）' in text
        assert '最后结合已算出的指标论证选择' in result['question']['stem']


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_excludes_actual_failed_constraints_and_recommends_feasible_optimum(language):
    result = compile_case(language, constraints=[threshold(value=1), threshold('switch_count', value=1)])
    text = result['question']['explanation']
    assert optimum(result)['feasible_scenario_ids'] == ['large']
    assert optimum(result)['optimal_scenario_ids'] == ['large']
    failures = [claim for claim in result['evidence']['claims']
                if claim['type'] == 'threshold_comparison' and not claim['satisfied']]
    assert [(c['scenario_id'], c['metric']) for c in failures] == [('small', 'overhead'), ('small', 'switch_count')]
    if language == 'en':
        assert 'infeasible; these required comparisons fail: total switching overhead 2 <= 1; process switch count 2 <= 1' in text
        assert 'all optimal scenarios are large, with objective value 2.' in text
    else:
        assert '不可行；以下要求不满足：切换总开销 2 <= 1; 进程切换次数 2 <= 1' in text
        assert '全部最优方案为 large，目标值为 2。' in text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_no_feasible_candidate_does_not_invent_recommendation(language):
    result = compile_case(language, constraints=[threshold(operator='lt', value=0)])
    text = result['question']['explanation']
    assert optimum(result)['feasible_scenario_ids'] == []
    assert optimum(result)['optimal_scenario_ids'] == []
    assert optimum(result)['optimal_value'] is None
    if language == 'en':
        assert 'there is no feasible recommendation' in text
        assert 'Revise the constraints or candidates and recompute' in text
        assert 'all optimal scenarios are' not in text
    else:
        assert '不能在这些约束下给出' in text
        assert '需修改约束或候选并重新计算' in text
        assert '全部最优方案为' not in text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_identical_candidates_report_all_tied_optima_without_fabricated_tradeoff(language):
    result = compile_case(language, scenarios=[rr('left', 3), rr('right', 3)], constraints=[])
    text = result['question']['explanation']
    assert optimum(result)['optimal_scenario_ids'] == ['left', 'right']
    assert optimum(result)['optimal_value'] == {'numerator': 2, 'denominator': 1}
    assert ('All candidates have identical values' if language == 'en' else '各候选的上述四项指标完全相同') in text
    assert ('all optimal scenarios are left, right' if language == 'en' else '全部最优方案为 left, right') in text
    assert ('real tradeoff' if language == 'en' else '当前候选确有取舍') not in text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_dominating_candidate_is_not_misrepresented_as_metric_conflict(language):
    # Independent STCF trace: B[0,1), A[1,4); response mean 1/2, turnaround mean 5/2.
    result = compile_case(language, scenarios=[{'id': 'stcf', 'policy': 'stcf'}, rr('large', 3)])
    text = result['question']['explanation']
    assert optimum(result)['optimal_scenario_ids'] == ['stcf']
    assert optimum(result)['optimal_value'] == {'numerator': 1, 'denominator': 2}
    if language == 'en':
        assert 'stcf: mean response time=1/2; mean turnaround time=5/2; total switching overhead=0; process switch count=1' in text
        assert 'no conflict between these metrics' in text
        assert 'this is dominance, not a tradeoff' in text
        assert 'lower mean response time (1/2 < 2)' in text
        assert 'real tradeoff' not in text
    else:
        assert 'stcf: 平均响应时间=1/2; 平均周转时间=5/2; 切换总开销=0; 进程切换次数=1' in text
        assert '没有呈现指标之间的冲突' in text
        assert '这里是支配关系' in text
        assert '平均响应时间更小（1/2 < 2）' in text
        assert '当前候选确有取舍' not in text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_single_candidate_makes_no_comparative_claim(language):
    result = compile_case(language, scenarios=[rr('only', 3)])
    text = result['question']['explanation']
    assert ('Only one candidate is supplied' if language == 'en' else '这里只提供一个候选') in text
    assert ('real tradeoff' if language == 'en' else '当前候选确有取舍') not in text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_requested_maximization_and_makespan_selection_stay_unchanged(language):
    result = compile_case(language, metric='overhead', direction='max')
    assert optimum(result)['optimal_scenario_ids'] == ['small']
    assert optimum(result)['optimal_value'] == {'numerator': 2, 'denominator': 1}
    assert ('maximize total switching overhead' if language == 'en' else '最大化切换总开销') in result['question']['explanation']
    result = compile_case(language, metric='makespan')
    assert optimum(result)['optimal_scenario_ids'] == ['large']
    assert optimum(result)['optimal_value'] == {'numerator': 5, 'denominator': 1}
    assert ('with objective value 5' if language == 'en' else '目标值为 5') in result['question']['explanation']


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_known_bursts_completion_rule_and_limits_are_explicit(language):
    result = compile_case(language)
    for text in (result['question']['stem'], result['question']['explanation'], result['section']['text']):
        assert ('known inputs supplied for this exercise' if language == 'en' else '本练习预先给定的已知输入') in text
        assert ('unused part of its quantum' if language == 'en' else '时间片剩余部分') in text or (
            language == 'zh' and '时间片尚未使用的部分' in text)
    text = result['question']['explanation']
    assert ('finite candidates, objective and constraints' if language == 'en' else '有限候选、目标和约束') in text
    assert ('does not establish a universally optimal policy or quantum' if language == 'en' else '不足以证明某策略或时间片普遍最优') in text
    assert CPU_SPEC_REVISION == 'cpu-problem-spec-v2-20260919'
    assert result['evidence']['canonical_spec']['revision'] == CPU_SPEC_REVISION


def test_explanation_additions_do_not_modify_schedule_math_or_add_freeform_schema_fields():
    result = compile_case('en')
    by_id = {item['scenario_id']: item['result'] for item in result['evidence']['tool_results']}
    assert by_id['small']['per_process'] == {
        'A': {'first_start': 0, 'response': 0, 'completion': 6, 'turnaround': 6},
        'B': {'first_start': 2, 'response': 2, 'completion': 3, 'turnaround': 3},
    }
    assert by_id['large']['per_process'] == {
        'A': {'first_start': 0, 'response': 0, 'completion': 3, 'turnaround': 3},
        'B': {'first_start': 4, 'response': 4, 'completion': 5, 'turnaround': 5},
    }
    assert [c['type'] for c in result['evidence']['claims']] == [
        'conservation', 'conservation', 'threshold_comparison', 'threshold_comparison', 'optimal_selection']
    assert set(result['evidence']['canonical_spec']['spec']) == {
        'processes', 'scenarios', 'remaining_queries', 'selection'}
