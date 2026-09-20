"""Regression checks for information lost before source selection/coverage."""
from app.auto_exploration import MAX_BODY_CHARS, _body_excerpt, _excerpts


def test_long_source_keeps_intro_and_distinct_topic_sections():
    definition = 'The introductory definition provides the scope of the subject.'
    sparse = 'Sparse matrices store nonzero entries with their coordinates.'
    collision = 'A hash collision occurs when distinct keys map to one bucket.'
    repetitive = ('learning model workflow key concepts basic introduction ' * 50)[:1900]
    text = '# Introduction\n' + ('General background. ' * 160) + definition + '\n'
    text += (repetitive + '\n\n') * 5
    text += '\n## Sparse matrices\n' + sparse + '\n'
    text += (repetitive + '\n\n') * 10
    text += '\n## Hash collisions\n' + collision + '\n'
    text += (repetitive + '\n\n') * 15
    gaps = [{'need': concept, 'queries': [{'query': concept}]} for concept in
            ['introductory definition', 'sparse matrices coordinates', 'hash collisions keys']]
    result = _body_excerpt(text, gaps)
    assert len(result) <= MAX_BODY_CHARS
    assert definition in result and sparse in result and collision in result


def test_short_source_is_preserved_without_rewriting():
    text = '# 机器学习\n原始表述、代码标识和数学符号 x_i 保持原样。'
    assert _body_excerpt(text, []) == text


def test_coverage_keeps_late_anchor_in_a_merged_source_within_total_budget():
    quote = 'A distinct late section is still valid evidence for another requirement.'
    text = 'Early background. ' * 400 + quote
    assert text.index(quote) > 6000
    rows = _excerpts([{'id':'source-a','text':text,'metadata':{}},
                      {'id':'source-b','text':'Additional evidence. ' * 2000,'metadata':{}}])
    assert quote in rows[0]['text']
    assert sum(len(row['text']) for row in rows) == MAX_BODY_CHARS
