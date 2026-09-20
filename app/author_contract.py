"""Share the author's output contract and bounded, non-verbatim repair hints.

The storage model keeps historical optional fields. These additions describe its
existing question validators to authors; they never relax content validation or
silently remove options, concepts, or exercise conditions.
"""
from copy import deepcopy
import re

from .models import LearningAsset
from .difficulty import AssetRuleError


_QUESTION_ERRORS = {
    'mcq_option_count': ('options', {'min_length': 4, 'max_length': 4, 'unique_items': True}),
    'mcq_option_unique': ('options', {'min_length': 4, 'max_length': 4, 'unique_items': True}),
    'mcq_option_blank': ('options', {'nonempty_items': True}),
    'mcq_answer_key': ('answer', {'allowed_values': ['A', 'B', 'C', 'D']}),
    'short_answer_options': ('options', {'max_length': 0}),
}
_SAFE_ERROR_CODES = frozenset({
    'missing', 'extra_forbidden', 'model_type', 'model_attributes_type',
    'dict_type', 'list_type', 'tuple_type', 'string_type', 'string_too_short',
    'string_too_long', 'string_pattern_mismatch', 'bool_type', 'bool_parsing',
    'int_type', 'int_parsing', 'int_from_float', 'float_type', 'float_parsing',
    'finite_number', 'greater_than', 'greater_than_equal', 'less_than',
    'less_than_equal', 'literal_error', 'enum', 'too_short', 'too_long',
    'value_error', 'assertion_error', 'json_invalid', 'json_type',
    *_QUESTION_ERRORS,
})


def student_text_layout_instruction():
    """Presentation guidance only; it never introduces a validation gate."""
    return (
        'Make student-facing text easy to scan. Keep the scenario/background in its own paragraph. '
        'Put each numbered condition, Claim, Statement or solution step on its own line, retaining '
        'its label; separate the final question from the supporting statements with a blank line. '
        'Use short paragraphs in explanations and reference answers, and separate listed points '
        'with line breaks. These are line breaks within the existing text fields, not new schema '
        'fields: encode them as \\n inside JSON strings so the decoded text contains real newlines, '
        'not literal backslash-n text. Do not emit HTML or <br> tags. Keep options in the options '
        'array and MCQ answer keys as single letters. Preserve all facts, conditions, formulas, '
        'code and citation IDs; layout must not add, remove or change the task or its answer.')


def author_asset_schema(relaxed=False):
    """Return a fresh asset schema including kind-dependent question rules."""
    schema = LearningAsset.model_json_schema()
    question = schema['$defs']['Question']
    minimum, maximum = (2, 26) if relaxed else (4, 4)
    letters = [chr(65 + index) for index in range(maximum)]
    question['properties']['options']['maxItems'] = maximum
    if not relaxed:
        schema['$defs']['DifficultyDesign']['properties']['concepts']['maxItems'] = 6
    question['allOf'] = [
        {'if': {'properties': {'kind': {'const': 'mcq'}}, 'required': ['kind']},
         'then': {'properties': {
             'options': {'type': 'array', 'minItems': minimum, 'maxItems': maximum, 'uniqueItems': True},
             'answer': {'type': 'string', 'enum': letters},
         }, 'required': ['options']}},
        {'if': {'properties': {'kind': {'const': 'short_answer'}}, 'required': ['kind']},
         'then': {'properties': {'options': {'type': 'array', 'maxItems': 0}}}},
    ]
    if relaxed:
        # JSON Schema must also reject a valid letter for an absent option.
        # Thus two options allow A/B, six allow A-F, and so on through Z.
        question['allOf'].extend(
            {'if': {'properties': {'kind': {'const': 'mcq'},
                                  'options': {'minItems': count, 'maxItems': count}},
                    'required': ['kind', 'options']},
             'then': {'properties': {'answer': {'enum': letters[:count]}}}}
            for count in range(2, 26))
    question['properties']['options']['description'] = (
        ('For mcq, 2 to 26 distinct options without letter prefixes. ' if relaxed else
         'For mcq, exactly four distinct options without letter prefixes. ') +
        'For short_answer, an empty array.')
    question['properties']['answer']['description'] = (
        ('For mcq, one bare uppercase letter A-Z mapping to an existing option; no parentheses, ' if relaxed else
         'For mcq, exactly one bare uppercase letter A, B, C, or D; no parentheses, ') +
        'prefix or explanation. For short_answer, the reference answer.')
    return schema


def author_repair_contract(relaxed=False):
    """Fixed author instructions; safe to include in contracts and feedback."""
    contract = {
        'mcq_options': 'Provide exactly four different options, without A/B/C/D prefixes. '
                       'If the option count is wrong, revise the question and verify the answer; '
                       'do not automatically truncate the options.',
        'mcq_answer': 'Use exactly one bare uppercase letter: A, B, C, or D. '
                      'For example, replace (B) with B only when B is the verified correct option. '
                      'Keep reasoning in explanation.',
        'short_answer_options': 'Use an empty options array for short_answer.',
        'difficulty_design.concepts': 'Provide 1 to 6 nonempty concept labels. Consolidate '
                                      'overlapping labels if necessary, preserving every fact '
                                      'and condition needed to solve the question.',
        'difficulty_design.expected_steps': 'Provide 1 to 8 nonempty scoring steps. Combine '
                                            'related steps without removing required reasoning.',
        'repair_scope': 'Correct every reported field and return the complete JSON object. '
                        'Keep source support, solvability, answer correctness, and the requested '
                        'question slot and difficulty. The schema does not permit weaker answers.',
    }
    if relaxed:
        contract.update(
            mcq_options='Keep 2 to 26 different nonempty options. Preserve every option; do not truncate.',
            mcq_answer='Use one bare uppercase A-Z letter mapping to an existing option. '
                       'Verify that this option is correct and that other options are not also correct.',
        )
        contract['difficulty_design.concepts'] = (
            'Keep all nonempty concept labels needed for the question; no six-label cap applies.')
    return contract


def parse_author_asset(raw, relaxed=False):
    """Parse a strict author response, or losslessly accommodate format variance.

    Compatibility changes representation only. A caller must still run its
    normal citation, count, solver, quality and difficulty checks before saving.
    Original provider output remains untouched and every adjustment is audited.
    """
    value = deepcopy(raw)
    audit = {'mode': 'compatible' if relaxed else 'strict', 'transformations': [],
             'relaxed_constraints': []}
    if relaxed and isinstance(value, dict) and isinstance(value.get('questions'), list):
        for index, question in enumerate(value['questions']):
            if not isinstance(question, dict) or question.get('kind') != 'mcq':
                continue
            answer = question.get('answer')
            if not isinstance(answer, str):
                continue
            wrapped = re.fullmatch(r'(?:\(([A-Za-z])\)|\[([A-Za-z])\]|([A-Za-z])\.|([a-z]))', answer.strip())
            if wrapped:
                question['answer'] = next(group for group in wrapped.groups() if group is not None).upper()
                audit['transformations'].append({'path': ['questions', index, 'answer'],
                                                  'rule': 'bare_option_letter'})
    candidate = LearningAsset.model_validate(value)
    fields = []
    for index, question in enumerate(candidate.questions):
        if question.kind == 'mcq' and len(question.options) != 4:
            fields.append({'loc': ['questions', index, 'options'], 'type': 'mcq_option_count',
                           'expected': {'min_length': 4, 'max_length': 4, 'unique_items': True},
                           'actual_length': len(question.options)})
        if question.difficulty_design and len(question.difficulty_design.concepts) > 6:
            fields.append({'loc': ['questions', index, 'difficulty_design', 'concepts'], 'type': 'too_long',
                           'expected': {'min_length': 1, 'max_length': 6},
                           'actual_length': len(question.difficulty_design.concepts)})
    if fields and not relaxed:
        raise AssetRuleError('作者输出需要修正选项或概念列表格式。', 'schema_validation', fields=fields[:20])
    audit['relaxed_constraints'] = fields
    return candidate, audit


def _branches(node, schema):
    """Follow only local schema references and union branches."""
    if not isinstance(node, dict):
        return []
    ref = node.get('$ref')
    if isinstance(ref, str) and ref.startswith('#/$defs/'):
        target = schema.get('$defs', {}).get(ref.removeprefix('#/$defs/'))
        if isinstance(target, dict):
            node = target
    result = [node]
    for key in ('anyOf', 'oneOf'):
        for branch in node.get(key, []):
            result.extend(_branches(branch, schema))
    return result


def _safe_path_and_nodes(path, schema):
    # A globally known field name is not enough: it must be valid at this
    # position. Unknown provider-controlled keys never enter repair messages.
    nodes = _branches(schema, schema)
    safe = []
    for part in path:
        children = []
        if type(part) is int and part >= 0:
            children = [node['items'] for node in nodes if isinstance(node.get('items'), dict)]
        elif isinstance(part, str):
            children = [node['properties'][part] for node in nodes
                        if part in node.get('properties', {})]
        safe.append(part if children else 'unknown_field')
        nodes = [branch for child in children for branch in _branches(child, schema)]
    return safe, nodes


def _length(value):
    return value if type(value) is int and value >= 0 else None


def schema_validation_diagnostic(exc, schema):
    """Expose stable field names, codes, and numeric bounds, never error text.

    Pydantic's msg, input, URL, arbitrary ctx values and custom error codes can
    contain provider-controlled text. Only the explicit whitelist is returned.
    """
    fields = []
    for error in exc.errors(include_input=False, include_context=True, include_url=False)[:20]:
        code = error.get('type')
        code = code if code in _SAFE_ERROR_CODES else 'validation_error'
        path, nodes = _safe_path_and_nodes(error.get('loc', ()), schema)
        item = {'loc': path, 'type': code}
        context = error.get('ctx') or {}
        expected = {}
        if code in _QUESTION_ERRORS:
            field, expected = _QUESTION_ERRORS[code]
            # Model validators report the whole question. Point the author to
            # its actual field without echoing any generated value.
            if any(field in node.get('properties', {}) for node in nodes):
                item['loc'] = [*path, field]
            expected = deepcopy(expected)
            if schema.get('$defs', {}).get('Question', {}).get('properties', {}).get('options', {}).get('maxItems') == 26:
                if code in {'mcq_option_count', 'mcq_option_unique'}:
                    expected.update(min_length=2, max_length=26)
                elif code == 'mcq_answer_key':
                    count = _length(context.get('option_count'))
                    expected = {'allowed_values': [chr(65 + index) for index in range(min(count or 26, 26))]}
        elif code in {'too_short', 'too_long', 'string_too_short', 'string_too_long'}:
            for name, schema_names in (
                    ('min_length', ('minItems', 'minLength')),
                    ('max_length', ('maxItems', 'maxLength'))):
                bounds = [_length(node[key]) for node in nodes for key in schema_names if key in node]
                bounds = [bound for bound in bounds if bound is not None]
                if bounds:
                    # Union schemas may contain multiple possible lengths;
                    # only report bounds that all relevant branches agree on.
                    if len(set(bounds)) == 1:
                        expected[name] = bounds[0]
        if expected:
            item['expected'] = expected
        if code in {'too_short', 'too_long', *_QUESTION_ERRORS}:
            actual = _length(context.get('actual_length'))
            if actual is not None:
                item['actual_length'] = actual
        fields.append(item)
    return {'rule': 'schema_validation', 'fields': fields}
