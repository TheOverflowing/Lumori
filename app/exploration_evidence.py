"""Resolve model passage choices to bounded, controller-owned source quotations.

Passage IDs identify exact substrings, not verified facts. Normal coverage and
selection validation must still check relevance, frozen requirements and source
provenance after resolving a response.
"""
import re


MAX_PASSAGE_CHARS = 900
MIN_PASSAGE_CHARS = 12
MAX_EVIDENCE_CHARS = 24000
MAX_PASSAGES = 80


def evidence_passages(sources):
    """Return deterministic paragraph excerpts without inventing or joining text."""
    if not isinstance(sources, list):
        return []
    result, identities = [], set()
    remaining = MAX_EVIDENCE_CHARS
    for source in sources:
        if not isinstance(source, dict):
            continue
        identity, text = source.get('id'), source.get('text')
        if not isinstance(identity, str) or not identity or identity in identities or not isinstance(text, str):
            continue
        identities.add(identity)
        # Splitting only removes paragraph separators. Trimming and slicing each
        # individual piece preserves exact contiguous original-source matches.
        for paragraph in re.split(r'\n[\t \r]*\n+', text):
            paragraph = paragraph.strip()
            # A standalone Markdown heading identifies a topic but cannot
            # supply its explanation. Keep headings that share actual body text.
            if re.fullmatch(r'#{1,6}[\t ]+[^\n]+', paragraph):
                continue
            while len(paragraph) >= MIN_PASSAGE_CHARS:
                if remaining < MIN_PASSAGE_CHARS or len(result) >= MAX_PASSAGES:
                    return result
                length = min(MAX_PASSAGE_CHARS, remaining, len(paragraph))
                # Keep a short final tail with its preceding words where possible
                # instead of discarding it merely because the preceding cut was 900.
                tail = len(paragraph) - length
                if 0 < tail < MIN_PASSAGE_CHARS and length == MAX_PASSAGE_CHARS:
                    length -= MIN_PASSAGE_CHARS - tail
                piece = paragraph[:length].strip()
                paragraph = paragraph[length:].strip()
                if len(piece) < MIN_PASSAGE_CHARS:
                    continue
                result.append({'id': f'p{len(result) + 1}', 'source_id': identity, 'text': piece})
                remaining -= len(piece)
    return result


def _passage_map(passages):
    if not isinstance(passages, list) or len(passages) > MAX_PASSAGES:
        return None
    result, total = {}, 0
    for passage in passages:
        if not isinstance(passage, dict) or set(passage) != {'id', 'source_id', 'text'}:
            return None
        identity, source, text = passage['id'], passage['source_id'], passage['text']
        if (not isinstance(identity, str) or not re.fullmatch(r'p[1-9]\d*', identity) or identity in result
                or not isinstance(source, str) or not source or not isinstance(text, str)
                or not MIN_PASSAGE_CHARS <= len(text) <= MAX_PASSAGE_CHARS):
            return None
        total += len(text)
        if total > MAX_EVIDENCE_CHARS:
            return None
        result[identity] = passage
    return result


def _fixed_map(fixed):
    if fixed is None:
        return {}
    if not isinstance(fixed, list) or len(fixed) > 6:
        return None
    result = {}
    for item in fixed:
        if not isinstance(item, dict) or set(item) != {'id', 'need'}:
            return None
        identity, need = item['id'], item['need']
        if (not isinstance(identity, str) or not re.fullmatch(r'g[1-6]', identity) or identity in result
                or not isinstance(need, str) or not 2 <= len(need.strip()) <= 300):
            return None
        result[identity] = need
    return result


def resolve_coverage(value, passages, fixed=None):
    """Translate passage choices into the existing quote-based coverage schema.

Historical quote responses retain their original source IDs and quotations;
the caller must validate them against their corresponding original excerpts.
"""
    if not isinstance(value, dict) or set(value) != {'status', 'requirements'}:
        return None
    if not isinstance(value['status'], str) or value['status'] not in {'sufficient', 'missing_knowledge', 'needs_user_input'}:
        return None
    requirements = value['requirements']
    frozen = _fixed_map(fixed)
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 6 or frozen is None:
        return None
    mapped = None
    result, identities = [], set()
    response_mode = None
    for item in requirements:
        if not isinstance(item, dict):
            return None
        # Queries only request missing knowledge. Their omission for an already
        # covered item has exactly one safe meaning; never invent queries for an
        # uncovered item or discard an explicitly supplied value.
        if 'queries' not in item and item.get('covered') is True:
            item = dict(item, queries=[])
        if set(item) not in (
                {'id', 'need', 'covered', 'supports', 'queries'}, {'id', 'covered', 'supports', 'queries'}):
            return None
        identity = item['id']
        if not isinstance(identity, str) or not re.fullmatch(r'g[1-6]', identity) or identity in identities:
            return None
        identities.add(identity)
        need = item.get('need', frozen.get(identity))
        if not isinstance(need, str) or not 2 <= len(need.strip()) <= 300 or type(item['covered']) is not bool:
            return None
        if not isinstance(item['supports'], list) or len(item['supports']) > 4:
            return None
        supports, seen = [], set()
        for support in item['supports']:
            if not isinstance(support, dict):
                return None
            if set(support) == {'passage_id'}:
                mode = 'passage'
                if mapped is None:
                    mapped = _passage_map(passages)
                passage_id = support['passage_id']
                if mapped is None or not isinstance(passage_id, str) or passage_id not in mapped:
                    return None
                resolved = {'source_id': passage_id, 'quote': mapped[passage_id]['text']}
            elif set(support) == {'source_id', 'quote'}:
                mode = 'quote'
                source, quote = support['source_id'], support['quote']
                if not isinstance(source, str) or not source or not isinstance(quote, str) or not 12 <= len(quote) <= 1000:
                    return None
                resolved = {'source_id': source, 'quote': quote}
            else:
                return None
            if response_mode is not None and response_mode != mode:
                return None
            response_mode = mode
            key = (resolved['source_id'], resolved['quote'])
            if key in seen:
                return None
            seen.add(key)
            supports.append(resolved)
        queries = item['queries']
        if not isinstance(queries, list) or len(queries) > 2:
            return None
        for query in queries:
            if (not isinstance(query, dict) or set(query) != {'query', 'language'}
                    or not isinstance(query['query'], str) or not isinstance(query['language'], str)
                    or query['language'] not in {'en', 'zh'}):
                return None
        result.append({'id': identity, 'need': need, 'covered': item['covered'],
                       'supports': supports, 'queries': [dict(query) for query in queries]})
    return {'status': value['status'], 'requirements': result}


def resolve_selection(value, passages):
    """Resolve chosen passage IDs; accept/reject semantics remain caller-checked."""
    if not isinstance(value, dict) or set(value) != {'accept', 'reason_code', 'supports'}:
        return None
    if (type(value['accept']) is not bool or not isinstance(value['reason_code'], str)
            or value['reason_code'] not in {'supports_gap', 'irrelevant', 'insufficient', 'conflicting', 'unreadable'}
            or not isinstance(value['supports'], list) or len(value['supports']) > 12):
        return None
    result, seen = [], set()
    mapped, response_mode = None, None
    for support in value['supports']:
        if not isinstance(support, dict):
            return None
        gap = support.get('gap_id')
        if not isinstance(gap, str) or not re.fullmatch(r'g[1-6]', gap):
            return None
        if set(support) == {'gap_id', 'passage_id'}:
            mode = 'passage'
            if mapped is None:
                mapped = _passage_map(passages)
            identity = support['passage_id']
            if mapped is None or not isinstance(identity, str) or identity not in mapped:
                return None
            quote = mapped[identity]['text']
        elif set(support) == {'gap_id', 'quote'}:
            mode = 'quote'
            quote = support['quote']
            if not isinstance(quote, str) or not 12 <= len(quote) <= 1000:
                return None
        else:
            return None
        if response_mode is not None and response_mode != mode:
            return None
        response_mode = mode
        if (gap, quote) in seen:
            return None
        seen.add((gap, quote))
        result.append({'gap_id': gap, 'quote': quote})
    return {'accept': value['accept'], 'reason_code': value['reason_code'], 'supports': result}
