import pytest

from app.content_consistency_audit import messages, validate


CASE = {
    'learner_profile': '已学习 TCP 的本科生',
    'student_visible_text': '有效累积确认号停在缺失区间的起点前。首个缺失字节为 1100。',
    'reference_answer': 'C',
    'reference_explanation': '累积确认号停在 1100，正确。',
}


def finding():
    return {
        'category': 'contradiction', 'certainty': 'definite',
        'student_quote': '有效累积确认号停在缺失区间的起点前',
        'answer_quote': '累积确认号停在 1100',
        'explanation': '学生段落把确认号放在缺口起点之前，答案却放在起点。',
        'suggested_correction': '把学生段落改为确认号指向首个缺失字节。',
    }


def test_exact_quotes_and_derived_status():
    prompt = messages(CASE)
    assert '1100' in prompt[1]['content']
    assert 'difficulty' not in prompt[1]['content']
    assert validate({'findings': [finding()]}, CASE)['status'] == 'correction_required'
    assert validate({'findings': []}, CASE)['status'] == 'no_issue_found'
    uncertain = finding() | {'certainty': 'uncertain'}
    assert validate({'findings': [uncertain]}, CASE)['status'] == 'review_required'


@pytest.mark.parametrize('key,bad', [('student_quote', '不存在的题面'),
                                      ('answer_quote', '不存在的答案')])
def test_findings_must_quote_actual_fields(key, bad):
    entry = finding() | {key: bad}
    with pytest.raises(ValueError, match='exact'):
        validate({'findings': [entry]}, CASE)


def test_invalid_or_unresolved_findings_fail_closed():
    with pytest.raises(ValueError):
        validate({'findings': [finding() | {'certainty': 'certain'}]}, CASE)
    with pytest.raises(ValueError):
        validate({'findings': [finding() | {'suggested_correction': '无需修改'}]}, CASE)
