"""Parsing provenance, scan fallbacks, visual preservation and resource boundaries.

OCR recognition quality is evaluated in the separate annotated benchmark. These
tests verify behavior and invariants, not a claimed recognition accuracy.
"""
import io
import json
import struct
import subprocess
import zlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import document_parsing as parsing


TEXT = 'Binary search repeatedly halves the sorted search interval. Its time complexity is logarithmic.'


def pdf_bytes(texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=600, height=400)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 14 Tf 40 350 Td ({text}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def png_bytes(width=64, height=32):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\0' + b'\xff' * width * 3) * height)) + chunk(b'IEND', b''))


def fake_raster(source, number, target, deadline, max_pixels):
    target.write_bytes(png_bytes())
    return target


def test_native_pdf_text_stays_exact_and_page_identity_survives():
    raw = pdf_bytes([TEXT, TEXT + ' More evidence.'])
    baseline = parsing.parse_document_structured('notes.pdf', raw, strategy='pypdf_v1', preserve_visuals=False)
    adaptive = parsing.parse_document_structured('notes.pdf', raw, preserve_visuals=False)
    assert baseline.text_pages == adaptive.text_pages
    assert [page.number for page in adaptive.pages] == [1, 2]
    assert all(page.method == 'pypdf_v1' for page in adaptive.pages)
    assert adaptive.status == 'good'
    assert adaptive.report()['metrics']['recognition_accuracy'] is None
    json.dumps(adaptive.report(), ensure_ascii=False)


def test_utf8_and_markdown_are_not_normalized():
    text = '\ufeff# 二分查找\n\n  空白及来源偏移应该保留。\n' + TEXT
    parsed = parsing.parse_document_structured('notes.md', text.encode())
    assert parsed.text_pages == [text[1:]]
    assert parsed.pages[0].method == 'utf8-sig-v1'
    assert parsed.pages[0].blocks[0]['char_end'] == len(text) - 1


def test_mixed_pdf_only_ocr_sparse_page_and_keep_all_pages(monkeypatch):
    calls = []
    monkeypatch.setattr(parsing, '_render_page', fake_raster)
    def ocr(path, deadline, languages, psm):
        calls.append((path.name, psm))
        return TEXT + ' Recovered.', [], {'ocr_mean_word_confidence': 87}
    monkeypatch.setattr(parsing, '_ocr', ocr)
    result = parsing.parse_document_structured('mixed.pdf', pdf_bytes([TEXT, '2']), preserve_visuals=False)
    assert len(calls) == 1
    assert result.pages[0].method == 'pypdf_v1'
    assert result.pages[1].method == 'tesseract_v1'
    assert 'Recovered' in result.pages[1].text
    assert 'ocr_text_requires_validation' in result.pages[1].warnings


def test_unreadable_photo_is_retained_with_preview_and_no_fake_text(monkeypatch):
    monkeypatch.setattr(parsing, '_ocr', lambda *args: ('', [], {'ocr_mean_word_confidence': 0}))
    raw = png_bytes()
    result = parsing.parse_document_structured('scan.png', raw)
    assert result.status == 'unreadable'
    assert result.text_pages == ['']
    assert result.assets[0].data == raw
    assert result.assets[0].kind == 'source_image'
    assert 'no_text_extracted' in result.pages[0].warnings


def test_image_native_baseline_does_not_secretly_run_ocr(monkeypatch):
    monkeypatch.setattr(parsing, '_ocr', lambda *args: pytest.fail('native baseline invoked OCR'))
    result = parsing.parse_document_structured('scan.png', png_bytes(), strategy='pypdf_v1')
    assert result.text_pages == ['']
    assert 'image_has_no_native_text_layer' in result.pages[0].warnings


def test_ocr_dependency_failure_is_explicit(monkeypatch):
    monkeypatch.setattr(parsing, '_tool', lambda _: None)
    result = parsing.parse_document_structured('scan.png', png_bytes())
    assert result.status == 'unreadable'
    assert 'ocr_dependency_missing:tesseract' in result.pages[0].warnings


def test_ocr_limit_keeps_unprocessed_pages_in_report(monkeypatch):
    rendered = []
    def raster(*args):
        rendered.append(args[1])
        return fake_raster(*args)
    monkeypatch.setattr(parsing, '_render_page', raster)
    monkeypatch.setattr(parsing, '_ocr', lambda *args: (TEXT, [], {'ocr_mean_word_confidence': 90}))
    result = parsing.parse_document_structured('scan.pdf', pdf_bytes(['', '', '']),
        max_ocr_pages=1, preserve_visuals=False)
    assert len(result.pages) == 3
    assert result.pages[0].text == TEXT
    assert result.pages[1].text == result.pages[2].text == ''
    assert all('ocr_page_limit' in p.warnings for p in result.pages[1:])
    assert result.status == 'needs_review'
    assert result.report()['metrics']['text_extraction_coverage'] == pytest.approx(1 / 3)
    assert rendered == [1]


def test_preview_preserves_vector_page_without_claiming_understanding(monkeypatch):
    monkeypatch.setattr(parsing, '_render_page', fake_raster)
    monkeypatch.setattr(parsing, '_embedded_images', lambda *args: None)
    result = parsing.parse_document_structured('figures.pdf', pdf_bytes([TEXT, TEXT]), max_asset_pages=1)
    assert len(result.assets) == 1
    assert result.assets[0].kind == 'page_preview'
    assert result.pages[0].asset_ids == [result.assets[0].id]
    assert 'page_visuals_preserved_not_semantically_interpreted' in result.pages[0].warnings
    assert 'preview_page_limit_use_original_pdf' in result.pages[1].warnings
    assert 'data' not in result.report()['assets'][0]


def test_low_confidence_is_warning_not_accuracy(monkeypatch):
    monkeypatch.setattr(parsing, '_ocr', lambda *args: (TEXT, [], {'ocr_mean_word_confidence': 32,
        'ocr_confidence_is_accuracy': False}))
    result = parsing.parse_document_structured('photo.png', png_bytes())
    assert result.status == 'needs_review'
    assert 'low_ocr_confidence_requires_review' in result.pages[0].warnings
    assert result.report()['metrics']['recognition_accuracy'] is None


def test_adaptive_keeps_native_if_ocr_recovers_less(monkeypatch):
    monkeypatch.setattr(parsing, '_render_page', fake_raster)
    monkeypatch.setattr(parsing, '_ocr', lambda *args: ('X', [], {'ocr_mean_word_confidence': 90}))
    result = parsing.parse_document_structured('short.pdf', pdf_bytes(['A short native title']), preserve_visuals=False)
    assert result.pages[0].text == 'A short native title'
    assert result.pages[0].method == 'pypdf_v1'
    assert 'ocr_did_not_improve_sparse_native_text' in result.pages[0].warnings


def test_forced_psm6_and_rapidocr_do_not_silently_select_different_engine(monkeypatch):
    psm_values = []
    monkeypatch.setattr(parsing, '_ocr', lambda path, deadline, languages, psm:
        (psm_values.append(psm) or TEXT, [], {'ocr_mean_word_confidence': 99}))
    monkeypatch.setattr(parsing, '_rapidocr', lambda *args: (TEXT, [], {'ocr_engine': {'version': 'fixture'}}))
    assert parsing.parse_document_structured('x.png', png_bytes(), strategy='tesseract_psm6_v1').pages[0].method == 'tesseract_psm6_v1'
    assert psm_values == [6]
    assert parsing.parse_document_structured('x.png', png_bytes(), strategy='tesseract_v1', ocr_engine='rapidocr').pages[0].method == 'tesseract_v1'
    assert parsing.parse_document_structured('x.png', png_bytes(), strategy='rapidocr_v1').pages[0].method == 'rapidocr_v1'
    assert parsing.parse_document_structured('x.png', png_bytes(), ocr_engine='rapidocr').pages[0].method == 'rapidocr_v1'


@pytest.mark.parametrize('name,raw,kwargs', [
    ('bad.png', b'not image data', {}),
    ('huge.png', png_bytes(200, 200), {'max_pixels': 65536 - 1}),
    ('wrong.pdf', b'%NOTPDF', {}),
    ('secret.svg', b'<svg></svg>', {}),
    ('text.txt', b'\xff\xfe\xff', {}),
    ('empty.txt', b'', {}),
    ('pages.pdf', pdf_bytes(['', '']), {'max_pages': 1}),
    ('text.txt', b'valid text', {'strategy': 'made_up'}),
])
def test_invalid_inputs_are_rejected(name, raw, kwargs):
    with pytest.raises(ValueError):
        parsing.parse_document_structured(name, raw, **kwargs)


def test_huge_image_header_rejected_before_decoder(monkeypatch):
    raw = bytearray(png_bytes())
    raw[16:24] = struct.pack('>II', 100_000, 100_000)
    monkeypatch.setattr(parsing, '_ocr', lambda *args: pytest.fail('unbounded image decoded'))
    with pytest.raises(ValueError, match='像素'):
        parsing.parse_document_structured('huge.png', bytes(raw))


def test_encrypted_pdf_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.encrypt('fixture password')
    target = io.BytesIO()
    writer.write(target)
    with pytest.raises(ValueError, match='加密'):
        parsing.parse_document_structured('secret.pdf', target.getvalue())


def test_total_deadline_failure_is_visible_and_preserves_photo(monkeypatch):
    def fail(*args):
        raise TimeoutError('document_time_limit')
    monkeypatch.setattr(parsing, '_ocr', fail)
    result = parsing.parse_document_structured('photo.png', png_bytes())
    assert result.status == 'unreadable'
    assert result.assets and 'document_time_limit' in result.pages[0].warnings


def test_tesseract_tsv_line_boxes_and_offsets(monkeypatch, tmp_path):
    tsv = ('level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
           '5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t90\tBinary\n'
           '5\t1\t1\t1\t1\t2\t45\t20\t35\t12\t70\tsearch\n'
           '5\t1\t1\t1\t2\t1\t10\t40\t40\t10\t80\tSorted\n')
    monkeypatch.setattr(parsing, '_tool', lambda _: '/fake/tesseract')
    class Deadline:
        def run(self, *args, **kwargs):
            return tsv.encode()
    text, blocks, metrics = parsing._ocr(tmp_path / 'x.png', Deadline(), 'chi_sim+eng', 3)
    assert text == 'Binary search\nSorted'
    assert blocks[0]['bbox'] == [10, 20, 70, 12]
    assert all(text[b['char_start']:b['char_end']] == b['text'] for b in blocks)
    assert metrics['ocr_mean_word_confidence'] == 80
    assert metrics['ocr_confidence_is_accuracy'] is False


@pytest.mark.skipif(not all(parsing._tool(x) for x in ('pdftotext', 'pdftoppm', 'tesseract')),
                    reason='Optional local OCR tools not installed')
def test_real_local_tools_extract_native_layout_and_raster_text():
    raw = pdf_bytes([TEXT])
    layout = parsing.parse_document_structured('notes.pdf', raw, strategy='poppler_layout_v1', preserve_visuals=False)
    scan = parsing.parse_document_structured('notes.pdf', raw, strategy='tesseract_v1', ocr_languages='eng')
    assert 'Binary search' in layout.text_pages[0]
    assert 'Binary search' in scan.text_pages[0]
    assert scan.pages[0].method == 'tesseract_v1'
    assert scan.assets and scan.assets[0].kind == 'page_preview'
    assert scan.pages[0].blocks[0]['bbox']


@pytest.mark.skipif(not parsing._tool('pdfimages') or not parsing._tool('pdftoppm'),
                    reason='Optional Poppler tools not installed')
def test_real_embedded_image_and_vector_preview_preserved(monkeypatch):
    from pypdf.generic import NumberObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    embedded = DecodedStreamObject()
    embedded.set_data(b'\xff\x10\x10' * 32 * 32)
    embedded.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
        NameObject('/Width'): NumberObject(32), NameObject('/Height'): NumberObject(32),
        NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'):
        DictionaryObject({NameObject('/Im1'): writer._add_object(embedded)})})
    stream = DecodedStreamObject()
    # Raster image plus vector diagonal: the page preview preserves both.
    stream.set_data(b'q 100 0 0 100 20 20 cm /Im1 Do Q 0 0 m 300 300 l S')
    page[NameObject('/Contents')] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    monkeypatch.setattr(parsing, '_ocr', lambda *args: ('', [], {'ocr_mean_word_confidence': 0}))
    result = parsing.parse_document_structured('diagram.pdf', target.getvalue())
    assert {asset.kind for asset in result.assets} == {'embedded_image', 'page_preview'}
    assert next(asset for asset in result.assets if asset.kind == 'embedded_image').width == 32
    assert result.text_pages == ['']
    assert 'embedded_images_not_semantically_interpreted' in result.pages[0].warnings


def test_native_timeout_becomes_actionable_validation_error(tmp_path):
    class Deadline:
        def run(self, *args, **kwargs):
            raise TimeoutError('document_time_limit')
    with pytest.raises(ValueError, match='解析超时'):
        parsing._native_pdf(tmp_path / 'bad.pdf', 30, Deadline())
