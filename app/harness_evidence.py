"""Candidate-bound evidence selection for the Harness review wire format.

The model still judges semantic coverage. Selecting a real span proves only
where its evidence came from; host validation retains source and quality gates.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

from .providers import ProviderOutputError


REVISION = 'candidate-evidence-selection-v1'
INSTRUCTION = (
    'Evidence encoding for this session: return stem_evidence_id and answer_evidence_id, '
    'not copied quotations or the old stem_evidence/answer_evidence fields. Select IDs from '
    'candidate_evidence_catalog: stem IDs for the task, answer or explanation IDs for the response. '
    'The host restores each exact original excerpt. These excerpts are anchors, not proof of compliance: '
    'judge the whole requirement against the full candidate, its reasoning and sources. Explain why '
    'the selected text supports your judgment. Use partial, missing or unsupported when appropriate; '
    'use an empty ID when no supporting excerpt exists. Never select text from the independent solution. '
    'This ID encoding replaces instructions elsewhere to copy literal evidence into response fields.'
)


@dataclass(frozen=True)
class EvidenceCatalog:
    candidate_sha256: str
    spans: dict

    def public(self):
        return {'revision': REVISION, 'candidate_sha256': self.candidate_sha256,
                'spans': self.spans}


def build_catalog(question):
    spans = {}
    for field, prefix in (('stem', 's'), ('answer', 'a'), ('explanation', 'e')):
        text = question.get(field)
        if not isinstance(text, str):
            raise ProviderOutputError('复核候选缺少完整的题干、答案或解析。')
        for line in re.finditer(r'[^\n]+', text):
            start, end = line.span()
            while start < end:
                stop = min(start + 400, end)
                if stop < end:
                    space = text.rfind(' ', start + 200, stop)
                    if space >= 0:
                        stop = space
                if stop - start >= 8:
                    key = prefix + str(len(spans) + 1)
                    spans[key] = {'field': field, 'start': start, 'end': stop,
                                  'text': text[start:stop]}
                start = stop
    digest = hashlib.sha256(json.dumps(
        {field: question[field] for field in ('stem', 'answer', 'explanation')},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return EvidenceCatalog(digest, spans)


def prepare_messages(messages):
    """Translate only the flexible review schema; leave all other roles intact."""
    contract = json.loads(messages[1]['content'])
    schema = contract.get('schema', {})
    definition = schema.get('$defs', {}).get('RequirementCheck', {})
    properties = definition.get('properties', {})
    if (contract.get('task') != 'agent_review' or
            not {'stem_evidence', 'answer_evidence'} <= properties.keys()):
        return messages, None
    result = deepcopy(messages)
    catalog = build_catalog(contract['question'])
    for old, new, allowed_fields in (
            ('stem_evidence', 'stem_evidence_id', ('stem',)),
            ('answer_evidence', 'answer_evidence_id', ('answer', 'explanation'))):
        properties.pop(old)
        properties[new] = {'type': 'string', 'enum': ['', *[
            key for key, span in catalog.spans.items() if span['field'] in allowed_fields]]}
        definition['required'] = [new if name == old else name for name in definition.get('required', [])]
    contract['candidate_evidence_catalog'] = catalog.public()
    for repair_key in ('evidence_repair', 'schema_repair'):
        if isinstance(contract.get(repair_key), dict):
            contract[repair_key]['evidence_encoding'] = INSTRUCTION
    result[0]['content'] += '\n' + INSTRUCTION
    result[1]['content'] = json.dumps(contract, ensure_ascii=False)
    return result, catalog


def resolve_selection(value, catalog):
    """Reject unknown/wrong-field IDs. Never correct or promote model verdicts."""
    if catalog is None:
        return value
    result = deepcopy(value)
    checks = result.get('requirement_checks')
    if not isinstance(checks, list):
        raise ProviderOutputError('复核未返回逐项证据选择。')
    for check in checks:
        if not isinstance(check, dict) or 'stem_evidence' in check or 'answer_evidence' in check:
            raise ProviderOutputError('复核证据编码无效。')
        for old, new, allowed in (
                ('stem_evidence_id', 'stem_evidence', ('stem',)),
                ('answer_evidence_id', 'answer_evidence', ('answer', 'explanation'))):
            identifier = check.pop(old, None)
            if not isinstance(identifier, str):
                raise ProviderOutputError('复核证据编号缺失或类型无效。')
            if identifier == '':
                check[new] = ''
                continue
            span = catalog.spans.get(identifier)
            if span is None or span['field'] not in allowed:
                raise ProviderOutputError('复核证据编号不属于当前候选或字段。')
            check[new] = span['text']
    return result
