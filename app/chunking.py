"""Deterministic, page-local chunking with auditable character coordinates.

Offsets count Python string characters. Character strategies limit characters;
the token strategy counts a verified tokenizer while preserving exact source
slices. Legacy coordinates refer to the old normalized page, with separate
original-page bounds for migration and span evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


STRATEGIES = ("legacy_char_v1", "recursive_v1", "recursive_token_v1")
_PARAGRAPH = re.compile(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*")
_SENTENCE = re.compile(r'''[。！？；]+[”’」』"']*|[.!?;]+["')\]]*(?=\s|$)''')
_LINE = re.compile(r"\r?\n")
_WORD = re.compile(r"\s+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
_SETEXT = re.compile(r"^ {0,3}(={3,}|-{3,})\s*$")
_ENGLISH = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:[-.+/][A-Za-z0-9_]+)*")
_STOPWORDS = frozenset("the and for that this with from into are was were has have had not but can will would should could each such then than also when where which what how does using use used these those their there they them its our your you is an of to in on as by at or be it a".split())


def chunking_config(*, strategy="recursive_v1", max_chars=1200, overlap_chars=120,
                    max_tokens=256, tokenizer_path=None, tokenizer_sha256=None):
    """Validate the public settings and return a serializable configuration."""
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ValueError(f"Unknown chunk strategy: {strategy!r}")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if type(overlap_chars) is not int or not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be an integer in [0, max_chars)")
    if strategy == 'recursive_token_v1':
        if type(max_tokens) is not int or max_tokens < 8:
            raise ValueError('max_tokens must be an integer >= 8')
        if not tokenizer_path or not isinstance(tokenizer_sha256, str) or not re.fullmatch('[0-9a-f]{64}', tokenizer_sha256):
            raise ValueError('Token chunking requires a tokenizer path and SHA-256.')
        if overlap_chars != 0:
            raise ValueError('recursive_token_v1 uses no overlap; set overlap_chars=0.')
        return dict(strategy=strategy, max_chars=max_chars, overlap_chars=0, max_tokens=max_tokens,
                    tokenizer_path=str(tokenizer_path), tokenizer_sha256=tokenizer_sha256)
    return {"strategy": strategy, "max_chars": max_chars, "overlap_chars": overlap_chars}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    reason: str
    protected: str | None = None
    protected_split: bool = False
    complete: bool = True


def _parts(text, start, end, separator):
    parts = []
    previous = start
    for match in separator.finditer(text, start, end):
        bounds = _trim(text, previous, match.end())
        if bounds[0] < bounds[1]:
            parts.append(bounds)
        previous = match.end()
    bounds = _trim(text, previous, end)
    if bounds[0] < bounds[1]:
        parts.append(bounds)
    return parts


def _split(text, start, end, limit, levels=None, reason="paragraph", protected=None):
    start, end = _trim(text, start, end)
    if start == end:
        return []
    if levels is None:
        levels = ((_PARAGRAPH, "paragraph"), (_SENTENCE, "sentence"), (_LINE, "line"), (_WORD, "word"))
    if end - start <= limit:
        return [_Unit(start, end, reason, protected, bool(protected), reason not in ("word", "character") and not protected)]
    for index, (separator, boundary) in enumerate(levels):
        parts = _parts(text, start, end, separator)
        if len(parts) > 1:
            units = []
            for left, right in parts:
                units.extend(_split(text, left, right, limit, levels[index + 1:], boundary, protected))
            return units
    return [_Unit(left, min(left + limit, end), "character", protected, bool(protected), False)
            for left in range(start, end, limit)]


def _table_delimiter(line):
    cells = line.strip().strip("|").split("|")
    return len(cells) >= 2 and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def _structure(text):
    """Recognize explicit Markdown headings and protected blocks, not guessed PDF titles."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    headings, protected = [], []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            close = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
            last = index + 1
            while last < len(lines) and not close.fullmatch(lines[last].rstrip("\r\n")):
                last += 1
            stop = min(last + 1, len(lines))
            protected.append((offsets[index], offsets[stop], "code_fence"))
            index = stop
            continue
        if "|" in line and index + 1 < len(lines) and _table_delimiter(lines[index + 1]):
            stop = index + 2
            while stop < len(lines) and "|" in lines[stop] and lines[stop].strip():
                stop += 1
            protected.append((offsets[index], offsets[stop], "markdown_table"))
            index = stop
            continue
        atx = _ATX.match(line)
        if atx:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", atx.group(2)).strip()
            headings.append((offsets[index], len(atx.group(1)), title))
        elif line.strip() and index + 1 < len(lines):
            underline = _SETEXT.match(lines[index + 1].rstrip("\r\n"))
            if underline:
                headings.append((offsets[index], 1 if underline.group(1)[0] == "=" else 2, line.strip()))
                index += 1
        index += 1
    sections, stack, start, path = [], [], 0, []
    for position, level, title in headings:
        if position > start:
            sections.append((start, position, path))
        stack = [(old_level, old_title) for old_level, old_title in stack if old_level < level]
        stack.append((level, title))
        start, path = position, [title for _, title in stack]
    sections.append((start, len(text), path))
    return sections, protected


def _overlap_units(text, units):
    """Only repeat whole trailing paragraphs/sentences/lines or whole protected blocks."""
    result = []
    for unit in units:
        if unit.protected or not unit.complete:
            result.append(unit)
            continue
        for start, end in _parts(text, unit.start, unit.end, _PARAGRAPH):
            sentences = _parts(text, start, end, _SENTENCE)
            pieces = sentences if len(sentences) > 1 else _parts(text, start, end, _LINE)
            result.extend(_Unit(left, right, unit.reason) for left, right in pieces)
    return result


def _tail(text, units, budget, next_unit, limit):
    tail = []
    for unit in reversed(_overlap_units(text, units)):
        if not unit.complete or units[-1].end - unit.start > budget or next_unit.end - unit.start > limit:
            break
        tail.insert(0, unit)
    return tail


def _keywords(text, path):
    candidates = list(reversed(path))
    candidates.extend(match.group(1) for match in re.finditer(r"(?<!`)`([^`\n]{2,80})`(?!`)", text))
    for match in re.finditer(r"\*\*([^*\n]{2,48})\*\*|__([^_\n]{2,48})__", text):
        candidates.append(match.group(1) or match.group(2))
    terms = {}
    for match in _ENGLISH.finditer(text):
        term = match.group()
        if len(term) >= 3 and term.casefold() not in _STOPWORDS:
            key = term.casefold()
            if key not in terms:
                terms[key] = [term, 0, match.start()]
            terms[key][1] += 1
    candidates.extend(value[0] for value in sorted(terms.values(), key=lambda row: (-row[1], row[2])))
    selected, seen = [], set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not 2 <= len(candidate) <= 80 or candidate.casefold() in seen:
            continue
        if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", candidate):
            continue
        selected.append(candidate)
        seen.add(candidate.casefold())
        if len(selected) == 8:
            break
    return selected


def _language(text):
    chinese = bool(re.search(r"[\u3400-\u9fff]", text))
    english = bool(re.search(r"[A-Za-z]", text))
    return "mixed" if chinese and english else "zh" if chinese else "en" if english else "und"


def _record(chunks, raw, coordinate_text, page, start, end, config, path, reason, units=(), original_bounds=None):
    text = coordinate_text[start:end]
    metadata = {
        "chunking_version": config["strategy"], "chunking_config": dict(config),
        "chunk_index": len(chunks), "page_char_start": start, "page_char_end": end,
        "source_page_sha256": _sha(raw),
        "coordinate_system": "parsed_page_python_chars_v1" if config["strategy"] == "recursive_v1" else "normalized_page_python_chars_v1",
        "heading_path": list(path), "keywords": _keywords(text, path), "language": _language(text),
        "split_reason": reason, "protected_block_split": any(unit.protected_split for unit in units),
        "protected_block_types": sorted({unit.protected for unit in units if unit.protected}),
    }
    if original_bounds is not None:
        metadata.update(original_page_char_start=original_bounds[0], original_page_char_end=original_bounds[1],
                        coordinate_page_sha256=_sha(coordinate_text), normalization="remove_nul_then_strip_v1")
    identity = json.dumps({"page": page, "source": metadata["source_page_sha256"], "start": start, "end": end,
                           "config": config}, sort_keys=True, separators=(",", ":"))
    chunks.append({"id": "chunk_" + _sha(identity), "page": page, "text": text, "metadata": metadata})


def chunk_pages(pages: list[str], *, strategy="recursive_v1", max_chars=1200, overlap_chars=120,
                max_tokens=256, tokenizer_path=None, tokenizer_sha256=None) -> list[dict]:
    """Split each page independently; headings prohibit cross-section overlap.

    ``recursive_v1`` keeps raw source coordinates, including NUL characters.
    ``legacy_char_v1`` reproduces old text/page output at the default settings.
    An empty or whitespace-only input yields an empty list.
    """
    config = chunking_config(strategy=strategy, max_chars=max_chars, overlap_chars=overlap_chars,
                             max_tokens=max_tokens, tokenizer_path=tokenizer_path, tokenizer_sha256=tokenizer_sha256)
    if not isinstance(pages, list) or any(not isinstance(page, str) for page in pages):
        raise ValueError("pages must be a list of strings")
    chunks = []
    if strategy == 'recursive_token_v1':
        from .rag_candidates import token_chunks
        from .rag_runtime import verified_counter
        counter = verified_counter(tokenizer_path, tokenizer_sha256)
        for number, raw in enumerate(pages, 1):
            for c in token_chunks({'id': str(number), 'text': raw}, counter, max_tokens):
                a, b = c['source_start'], c['source_end']
                meta = c['metadata'] | {'chunking_version': strategy, 'chunking_config': config,
                    'chunk_index': len(chunks), 'page_char_start': a, 'page_char_end': b,
                    'source_page_sha256': _sha(raw), 'coordinate_system': 'parsed_page_python_chars_v1',
                    'keywords': _keywords(c['text'], c['metadata']['heading_path']), 'language': _language(c['text']),
                    'split_reason': 'recursive_token_budget', 'protected_block_types': []}
                identity = json.dumps({'page': number, 'source': _sha(raw), 'start': a, 'end': b, 'config': config}, sort_keys=True)
                chunks.append({'id': 'chunk_' + _sha(identity), 'page': number, 'text': c['text'], 'metadata': meta})
        return chunks
    for page_number, raw in enumerate(pages, 1):
        if strategy == "legacy_char_v1":
            positions = [index for index, char in enumerate(raw) if char != "\x00"]
            normalized = raw.replace("\x00", "")
            left, right = _trim(normalized, 0, len(normalized))
            normalized, positions = normalized[left:right], positions[left:right]
            start = 0
            while start < len(normalized):
                end = min(start + max_chars, len(normalized))
                reason = "page_end" if end == len(normalized) else "character"
                if end < len(normalized):
                    boundary = normalized.rfind("\n", start + max_chars // 2, end)
                    if boundary > start:
                        end, reason = boundary, "line"
                trimmed_start, trimmed_end = _trim(normalized, start, end)
                if trimmed_start < trimmed_end:
                    _record(chunks, raw, normalized, page_number, trimmed_start, trimmed_end, config, [], reason,
                            original_bounds=(positions[trimmed_start], positions[trimmed_end - 1] + 1))
                if end == len(normalized):
                    break
                start = max(start + 1, end - overlap_chars)
            continue
        sections, protected = _structure(raw)
        for section_start, section_end, path in sections:
            units, cursor = [], section_start
            for left, right, kind in protected:
                if left < section_start or left >= section_end:
                    continue
                units.extend(_split(raw, cursor, left, max_chars))
                block_start, block_end = _trim(raw, left, right)
                if block_end - block_start <= max_chars:
                    units.append(_Unit(block_start, block_end, kind, kind))
                else:
                    units.extend(_split(raw, block_start, block_end, max_chars,
                                        ((_LINE, "line"), (_WORD, "word")), protected=kind))
                cursor = right
            units.extend(_split(raw, cursor, section_end, max_chars))
            current = []
            for unit in units:
                if current and unit.end - current[0].start > max_chars:
                    _record(chunks, raw, raw, page_number, current[0].start, current[-1].end, config, path, current[-1].reason, current)
                    current = _tail(raw, current, overlap_chars, unit, max_chars)
                current.append(unit)
            if current:
                reason = "heading_boundary" if section_end < len(raw) else "page_end"
                _record(chunks, raw, raw, page_number, current[0].start, current[-1].end, config, path, reason, current)
    return chunks
