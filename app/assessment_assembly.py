"""Lossless, question-local material bundles for supervisor_v2 assembly.

The host groups already-reviewed sections; it does not rewrite questions or
ask a model to reinterpret local labels such as Source A/Source B. Per-section
citation mappings remain in each frozen accepted asset; a displayed bundle's
citations cover its combined supporting material.
"""
from collections.abc import Mapping


ASSESSMENT_ASSEMBLY_REVISION = 'question-local-sections-v1-20260919'
SECTION_SEPARATOR = '\n\n---\n\n'


def _source_ids(value, *, allow_duplicates):
    if not isinstance(value, (list, tuple, set, frozenset)) or not 1 <= len(value) <= 10:
        raise ValueError('Each question context needs 1–10 scoped source IDs.')
    if any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in value):
        raise ValueError('Question context contains an invalid source ID.')
    if not allow_duplicates and len(set(value)) != len(value):
        raise ValueError('Question context source IDs must be unique.')
    return set(value)


def assemble_question_sections(pieces, *, language, allowed_sources_by_slot):
    """Return one material Section dict per ordered, accepted single-question asset.

    Accept at most fifty q1..qN assets, each with one through ten sections.
    Preserve every original heading/text in order, including equal sections
    reused by different questions. Never truncate or mutate input assets.
    Citation unions are ordered by first appearance and validated against the
    particular worker's source scope, not the complete course source set.
    """
    if language not in ('en', 'zh'):
        raise ValueError('Question context language must be en or zh.')
    if not isinstance(pieces, (list, tuple)) or not 1 <= len(pieces) <= 50:
        raise ValueError('Question context assembly requires 1–50 complete question assets.')
    if not isinstance(allowed_sources_by_slot, Mapping):
        raise ValueError('Question context assembly requires explicit per-worker source scopes.')
    expected_slots = [f'q{index+1}' for index in range(len(pieces))]
    if set(allowed_sources_by_slot) != set(expected_slots):
        raise ValueError('Question context scopes must match the complete ordered question set.')
    result = []
    for number, (piece, slot_id) in enumerate(zip(pieces, expected_slots), 1):
        if not isinstance(piece, dict):
            raise ValueError('Each question context must come from an accepted asset object.')
        questions = piece.get('questions')
        if (not isinstance(questions, list) or len(questions) != 1
                or not isinstance(questions[0], dict) or questions[0].get('slot_id') != slot_id):
            raise ValueError('Question context assets must contain exactly one question in q1..qN order.')
        sections = piece.get('sections')
        if not isinstance(sections, list) or not 1 <= len(sections) <= 10:
            raise ValueError('Each question context must retain 1–10 original sections.')
        allowed = _source_ids(allowed_sources_by_slot[slot_id], allow_duplicates=False)
        blocks, citations = [], []
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError('An original question section must be an object.')
            heading, text = section.get('heading'), section.get('text')
            if any(not isinstance(value, str) or not value.strip() for value in (heading, text)):
                raise ValueError('An original question section has an empty heading or text.')
            section_ids = section.get('citation_ids')
            if not isinstance(section_ids, list) or not _source_ids(section_ids, allow_duplicates=True) <= allowed:
                raise ValueError('An original section cites outside its own worker source scope.')
            # No stripping, rewriting, deduplication, renumbering or embedded
            # citation IDs in visible prose; local headings retain their meaning.
            blocks.append(heading + '\n\n' + text)
            for source_id in section_ids:
                if source_id not in citations:
                    citations.append(source_id)
        result.append({
            'heading': f'第{number}题 · 资料' if language == 'zh' else f'Question {number} · Supporting material',
            'text': SECTION_SEPARATOR.join(blocks),
            'citation_ids': citations,
        })
    return result
