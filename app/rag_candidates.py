"""Versioned R4 candidates; no provider calls or relevance-label access."""
from functools import lru_cache
import hashlib
import re

from .chunking import _structure, _parts, _trim, _PARAGRAPH, _SENTENCE, _LINE, _WORD

QUERY_INSTRUCTION = 'Given a question about computer science, retrieve passages that provide evidence to answer the question.'


def query_input(text, instruction=False):
    return f'Instruct: {QUERY_INSTRUCTION}\nQuery: {text}' if instruction else text


def title_input(text, title='', headings=()):
    parts = list(dict.fromkeys(p.strip() for p in [title, *headings] if p and p.strip()))
    return (' > '.join(parts) + '\n' if parts else '') + text


class TokenCounter:
    """Exact local tokenizer units, not a claim about the generation provider's tokenizer."""
    def __init__(self, path):
        from tokenizers import Tokenizer
        self.tokenizer = Tokenizer.from_file(str(path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    @lru_cache(maxsize=32768)
    def count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


def token_chunks(parent, counter, limit=256):
    """Greedy same-section merging of recursively token-bounded exact source spans.

    No overlap. Whole recognized code/table units are kept when they fit. Oversize
    protected units degrade explicitly. Titles are encoded later as an ablation.
    """
    if type(limit) is not int or limit < 8:
        raise ValueError('Token chunk limit must be an integer >= 8.')
    text = parent['text']
    sections, protected = _structure(text)
    levels = (_PARAGRAPH, _SENTENCE, _LINE, _WORD)

    def split(start, end, level=0):
        start, end = _trim(text, start, end)
        if start == end:
            return []
        if counter.count(text[start:end]) <= limit:
            return [(start, end)]
        for i in range(level, len(levels)):
            parts = _parts(text, start, end, levels[i])
            if len(parts) > 1:
                return [span for a, b in parts for span in split(a, b, i + 1)]
        result = []
        while start < end:
            lo, hi = start + 1, end
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if counter.count(text[start:mid]) <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            if counter.count(text[start:lo]) > limit:
                raise ValueError('A single source character exceeds the token limit.')
            result.append((start, lo))
            start = lo
        return result

    result = []
    for section_start, section_end, path in sections:
        pieces, cursor = [], section_start
        for a, b, kind in protected:
            if b <= section_start or a >= section_end:
                continue
            a, b = max(a, section_start), min(b, section_end)
            pieces.extend((x, y, False) for x, y in split(cursor, a))
            units = split(a, b)
            pieces.extend((x, y, len(units) > 1) for x, y in units)
            cursor = b
        pieces.extend((x, y, False) for x, y in split(cursor, section_end))
        merged = []
        for a, b, broken in pieces:
            if merged and counter.count(text[merged[-1][0]:b]) <= limit:
                old = merged.pop()
                merged.append((old[0], b, old[2] or broken))
            else:
                merged.append((a, b, broken))
        for a, b, broken in merged:
            raw = text[a:b]
            result.append({'id': f"{parent['id']}:t{limit}:{a}:{b}", 'text': raw,
                           'parent_id': parent['id'], 'source_start': a, 'source_end': b,
                           'language': parent.get('language', ''), 'page': 1,
                           'text_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                           'metadata': {'heading_path': path, 'keywords': [], 'token_count': counter.count(raw),
                                        'token_limit': limit, 'protected_block_split': broken,
                                        'section_start': section_start, 'section_end': section_end,
                                        'chunk_strategy': 'recursive_token_v1_no_overlap'}})
    return result


def assemble_context(ranked, parents, counter, budget=1024, expansion='none', max_hits=5):
    """Add top leaves, optionally expand within their section, and union source spans.

    The shared budget counts the final plain-source serialization (Qwen tokenizer).
    Expansion is best-effort: fall back to the leaf when its parent is too long.
    It never drops an already accepted hit to make room for expansion.
    """
    if expansion not in ('none', 'window', 'parent') or type(budget) is not int or budget < 1:
        raise ValueError('Invalid context policy.')
    selected = []

    def normalized(rows):
        # Preserve first-hit parent order; order intervals inside a parent as source text.
        by_parent = {}
        for row in rows:
            by_parent.setdefault(row['parent_id'], []).append(row)
        result = []
        for pid, items in by_parent.items():
            for row in sorted(items, key=lambda r: r['source_start']):
                if result and result[-1]['parent_id'] == pid and row['source_start'] <= result[-1]['source_end']:
                    prev = result[-1]
                    prev['source_end'] = max(prev['source_end'], row['source_end'])
                    prev['text'] = parents[pid]['text'][prev['source_start']:prev['source_end']]
                else:
                    result.append(dict(row))
        return result

    def cost(rows):
        return counter.count('\n\n'.join(row['text'] for row in rows))

    # Reserve the retrieved evidence before spending the remaining budget on neighbors.
    accepted = []
    for leaf in ranked[:max_hits]:
        rows = normalized(selected + [leaf])
        if cost(rows) <= budget:
            selected = rows
            accepted.append(leaf)
    if expansion != 'none':
        for leaf in accepted:
            pid = leaf['parent_id']
            text = parents[pid]['text']
            sections, _ = _structure(text)
            a, b = next((a, b) for a, b, _ in sections if a <= leaf['source_start'] < b)
            if expansion == 'window':
                a, b = max(a, leaf['source_start'] - 160), min(b, leaf['source_end'] + 160)
            candidate = dict(leaf, source_start=a, source_end=b, text=text[a:b])
            rows = normalized(selected + [candidate])
            if cost(rows) <= budget:
                selected = rows
    return selected, cost(selected)
