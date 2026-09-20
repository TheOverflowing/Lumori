"""Chunk source preservation, structural boundaries, and migration regressions."""
from copy import deepcopy
import hashlib
import random

import pytest

from app.chunking import chunk_pages, chunking_config


def assert_source_contract(pages, chunks, limit):
    covered = [set() for _ in pages]
    for index, chunk in enumerate(chunks):
        meta = chunk["metadata"]
        raw = pages[chunk["page"] - 1]
        start, end = meta["page_char_start"], meta["page_char_end"]
        assert 0 <= start < end <= len(raw)
        assert chunk["text"] == raw[start:end]
        assert len(chunk["text"]) <= limit
        assert meta["chunk_index"] == index
        assert meta["source_page_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert meta["coordinate_system"] == "parsed_page_python_chars_v1"
        covered[chunk["page"] - 1].update(range(start, end))
    for raw, positions in zip(pages, covered):
        assert all(index in positions for index, char in enumerate(raw) if not char.isspace())


@pytest.mark.parametrize("config", [
    {"strategy": "unknown"}, {"strategy": None}, {"strategy": []},
    {"max_chars": 0}, {"max_chars": -1}, {"max_chars": True}, {"max_chars": 4.0},
    {"overlap_chars": -1}, {"overlap_chars": False}, {"overlap_chars": "1"},
    {"max_chars": 20, "overlap_chars": 20}, {"max_chars": 20, "overlap_chars": 21},
])
def test_invalid_configuration_is_rejected_even_for_empty_pages(config):
    with pytest.raises(ValueError):
        chunking_config(**config)
    with pytest.raises(ValueError):
        chunk_pages([], **config)


@pytest.mark.parametrize("pages", ["a page", [b"bytes"], [None], [(1, "text")], ("text",)])
def test_pages_requires_strings_in_a_list(pages):
    with pytest.raises(ValueError, match="list of strings"):
        chunk_pages(pages)


def test_empty_pages_preserve_later_page_numbers():
    chunks = chunk_pages(["", " \n\t", " 中文原文。 "])
    assert [(item["page"], item["text"]) for item in chunks] == [(3, "中文原文。")]
    assert chunk_pages([]) == []


def test_raw_offsets_preserve_nul_crlf_unicode_and_surrounding_whitespace():
    pages = [" \r\n# 标题\r\n\r\n甲\x00乙😊。\r\n后一句文本。 \t", "é漢字"]
    original = deepcopy(pages)
    chunks = chunk_pages(pages, max_chars=18, overlap_chars=4)
    assert_source_contract(pages, chunks, 18)
    assert any("\x00" in chunk["text"] for chunk in chunks)
    assert pages == original


def test_limits_count_characters_not_utf8_bytes_or_tokens():
    pages = ["汉" * 21, "a" * 21]
    chunks = chunk_pages(pages, max_chars=10, overlap_chars=0)
    assert [len(chunk["text"]) for chunk in chunks] == [10, 10, 1, 10, 10, 1]
    assert_source_contract(pages, chunks, 10)


def test_ids_are_deterministic_distinguish_pages_and_depend_on_config():
    pages = ["The same source.", "The same source."]
    first = chunk_pages(pages)
    assert first == chunk_pages(pages)
    assert len({chunk["id"] for chunk in first}) == 2
    assert first[0]["id"] != chunk_pages(pages, overlap_chars=0)[0]["id"]
    assert first[0]["metadata"]["chunking_config"] == chunking_config()


def test_no_cross_page_content_or_overlap():
    pages = ["First page has a sentence.", "Second page has its own sentence."]
    chunks = chunk_pages(pages)
    assert [chunk["text"] for chunk in chunks] == pages
    assert all(chunk["metadata"]["page_char_start"] == 0 for chunk in chunks)


def test_atx_headings_are_hard_boundaries_and_paths_follow_hierarchy():
    pages = ["Intro.\n\n# Parent\nAlpha.\n\n## Child\nBeta.\n\n# Other\nGamma."]
    chunks = chunk_pages(pages, max_chars=100, overlap_chars=50)
    assert [chunk["metadata"]["heading_path"] for chunk in chunks] == [[], ["Parent"], ["Parent", "Child"], ["Other"]]
    assert [chunk["text"] for chunk in chunks] == ["Intro.", "# Parent\nAlpha.", "## Child\nBeta.", "# Other\nGamma."]
    assert [chunk["metadata"]["split_reason"] for chunk in chunks[:-1]] == ["heading_boundary"] * 3
    assert_source_contract(pages, chunks, 100)


def test_setext_headings_are_recognized_without_treating_table_alignment_as_title():
    pages = ["Main\n====\nText.\n\nSub\n---\nMore.\n\n| A | B |\n| --- | --- |\n| x | y |"]
    chunks = chunk_pages(pages)
    assert [chunk["metadata"]["heading_path"] for chunk in chunks] == [["Main"], ["Main", "Sub"]]
    assert chunks[-1]["metadata"]["protected_block_types"] == ["markdown_table"]


def test_heading_closing_markers_do_not_remove_literal_hash_in_term():
    chunks = chunk_pages(["# C#\nA language.\n\n## APIs ##\nA section."])
    assert [chunk["metadata"]["heading_path"] for chunk in chunks] == [["C#"], ["C#", "APIs"]]


@pytest.mark.parametrize("punctuation", ["。", "？", "！", "；", ". ", "? ", "! ", "; "])
def test_sentence_boundaries_are_used_before_character_fallback(punctuation):
    sentence = "abcdef" + punctuation
    pages = [sentence * 3]
    chunks = chunk_pages(pages, max_chars=10, overlap_chars=0)
    assert [chunk["text"] for chunk in chunks] == [sentence.strip()] * 3
    assert chunks[0]["metadata"]["split_reason"] == "sentence"
    assert_source_contract(pages, chunks, 10)


def test_paragraph_boundaries_are_preferred_over_shorter_sentence_boundaries():
    first = "First sentence. Second sentence."
    second = "Third sentence. Fourth sentence."
    pages = [first + "\n\n" + second]
    chunks = chunk_pages(pages, max_chars=48, overlap_chars=0)
    assert [chunk["text"] for chunk in chunks] == [first, second]
    assert chunks[0]["metadata"]["split_reason"] == "paragraph"


def test_overlap_repeats_complete_final_sentence_within_budget():
    pages = ["Alpha one. Beta two. Gamma three. Delta four."]
    chunks = chunk_pages(pages, max_chars=33, overlap_chars=13)
    assert chunks[0]["text"] == "Alpha one. Beta two. Gamma three."
    assert chunks[1]["text"] == "Gamma three. Delta four."
    overlap = chunks[0]["metadata"]["page_char_end"] - chunks[1]["metadata"]["page_char_start"]
    assert overlap == len("Gamma three.") <= 13
    assert_source_contract(pages, chunks, 33)


def test_oversized_sentence_is_not_partially_repeated_to_fill_overlap_budget():
    pages = ["A long complete sentence. Another long complete sentence."]
    chunks = chunk_pages(pages, max_chars=35, overlap_chars=6)
    assert [chunk["text"] for chunk in chunks] == ["A long complete sentence.", "Another long complete sentence."]
    assert chunks[1]["metadata"]["page_char_start"] >= chunks[0]["metadata"]["page_char_end"]


def test_word_and_character_fallback_do_not_repeat_partial_sentences():
    pages = ["one two three four five six", "abcdefghijklmnop"]
    chunks = chunk_pages(pages, max_chars=10, overlap_chars=4)
    assert_source_contract(pages, chunks, 10)
    for previous, current in zip(chunks, chunks[1:]):
        if previous["page"] == current["page"]:
            assert current["metadata"]["page_char_start"] >= previous["metadata"]["page_char_end"]


@pytest.mark.parametrize("block", [
    "```python\n# This is code, not a heading\nprint('a')\n```",
    "~~~text\n## Also code\n~~~",
    "| A | B |\n| :--- | ---: |\n| one | two |",
])
def test_short_protected_blocks_remain_whole_even_near_capacity(block):
    pages = ["# Topic\n" + "Introduction. " * 4 + "\n\n" + block + "\n\nConclusion."]
    chunks = chunk_pages(pages, max_chars=max(65, len(block)), overlap_chars=10)
    holders = [chunk for chunk in chunks if block in chunk["text"]]
    assert len(holders) == 1
    assert holders[0]["metadata"]["protected_block_split"] is False
    assert all(chunk["metadata"]["heading_path"] == ["Topic"] for chunk in chunks)
    assert_source_contract(pages, chunks, max(65, len(block)))


@pytest.mark.parametrize("block,kind", [
    ("```python\n" + "value = 123456789\n" * 10 + "```", "code_fence"),
    ("| A | B |\n| --- | --- |\n" + "| item | value |\n" * 10, "markdown_table"),
])
def test_long_protected_blocks_are_explicitly_marked_and_never_exceed_limit(block, kind):
    chunks = chunk_pages([block], max_chars=48, overlap_chars=10)
    assert len(chunks) > 1
    assert all(chunk["metadata"]["protected_block_split"] for chunk in chunks)
    assert all(chunk["metadata"]["protected_block_types"] == [kind] for chunk in chunks)
    assert_source_contract([block], chunks, 48)


def test_unclosed_fence_protects_to_page_end_and_does_not_create_false_heading():
    text = "# Real\n\n```text\n# Not a heading\nbody"
    chunks = chunk_pages([text], max_chars=40, overlap_chars=4)
    assert all(chunk["metadata"]["heading_path"] == ["Real"] for chunk in chunks)
    assert any(chunk["metadata"]["protected_block_types"] == ["code_fence"] for chunk in chunks)


def test_keywords_use_explicit_terms_without_chinese_single_character_fragments():
    text = "# 数据库索引\n\n**覆盖索引**支持 `BTreeIndex`，transaction transaction locks。"
    chunk = chunk_pages([text])[0]
    keywords = chunk["metadata"]["keywords"]
    assert keywords[:3] == ["数据库索引", "BTreeIndex", "覆盖索引"]
    assert "transaction" in keywords
    assert len(keywords) <= 8
    assert all(len(term) >= 2 and term in text for term in keywords)
    assert chunk["metadata"]["language"] == "mixed"
    assert chunk_pages(["没有显式标题和强调词的中文说明。"])[0]["metadata"]["keywords"] == []


def test_legacy_default_text_and_pages_match_old_fixed_examples():
    pages = ["\n\x00" + "A" * 700 + "\n" + "B" * 700 + "  ", "x" * 1300, " \n"]
    chunks = chunk_pages(pages, strategy="legacy_char_v1")
    assert [(chunk["page"], chunk["text"]) for chunk in chunks] == [
        (1, "A" * 700), (1, "A" * 120 + "\n" + "B" * 700),
        (2, "x" * 1200), (2, "x" * 220),
    ]
    assert all(chunk["metadata"]["coordinate_system"] == "normalized_page_python_chars_v1" for chunk in chunks)


def test_legacy_mapping_exposes_original_bounds_when_nul_is_removed():
    raw = " \n甲\x00乙\x00丙 \n"
    chunk = chunk_pages([raw], strategy="legacy_char_v1")[0]
    meta = chunk["metadata"]
    assert chunk["text"] == "甲乙丙"
    assert (meta["page_char_start"], meta["page_char_end"]) == (0, 3)
    assert raw[meta["original_page_char_start"]:meta["original_page_char_end"]] == "甲\x00乙\x00丙"
    assert meta["normalization"] == "remove_nul_then_strip_v1"
    assert meta["source_page_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert meta["coordinate_page_sha256"] == hashlib.sha256("甲乙丙".encode()).hexdigest()


def test_legacy_searches_only_last_half_of_window_and_excludes_endpoint():
    raw = "A" * 400 + "\n" + "B" * 799 + "\nTAIL"
    chunks = chunk_pages([raw], strategy="legacy_char_v1")
    assert chunks[0]["text"] == raw[:1200]
    assert chunks[0]["metadata"]["split_reason"] == "character"


def test_randomized_source_coverage_length_and_repeatability():
    rng = random.Random(1200)
    alphabet = "甲乙丙😀\x00abc. ;?\n\r\t#`|"
    for _ in range(100):
        pages = ["".join(rng.choices(alphabet, k=rng.randint(0, 350))) for _ in range(2)]
        limit = rng.randint(1, 60)
        overlap = rng.randrange(limit)
        chunks = chunk_pages(pages, max_chars=limit, overlap_chars=overlap)
        assert_source_contract(pages, chunks, limit)
        assert chunks == chunk_pages(pages, max_chars=limit, overlap_chars=overlap)
