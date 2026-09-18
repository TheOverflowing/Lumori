"""Bounded, local document extraction with inspectable page-level provenance.

Operational extraction is not measured OCR accuracy. Text order, formula semantics,
and chart understanding must be evaluated separately against annotated references.
External executables are optional: their absence is reported rather than hidden.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field

from pypdf import PdfReader, __version__ as PYPDF_VERSION

PARSER_VERSION = 'local-document-parser-v1'
STRATEGIES = ('pypdf_v1', 'poppler_layout_v1', 'tesseract_v1',
              'tesseract_psm6_v1', 'rapidocr_v1', 'adaptive_local_v1')
_PARSE_SLOTS = threading.BoundedSemaphore(2)
_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_ASSET_BYTES = 32 * 1024 * 1024
_MAX_TEXT_CHARS = 4_000_000


@dataclass
class VisualAsset:
    id: str
    page: int
    kind: str
    mime_type: str
    data: bytes = field(repr=False)
    width: int | None = None
    height: int | None = None

    provenance: dict = field(default_factory=dict)

    def metadata(self):
        return {'id': self.id, 'page': self.page, 'kind': self.kind,
                'mime_type': self.mime_type, 'width': self.width,
                'height': self.height, 'byte_size': len(self.data),
                'sha256': hashlib.sha256(self.data).hexdigest(),
                'interpretation': 'visual preserved; semantic understanding not performed', **self.provenance}


@dataclass
class ParsedPage:
    number: int
    text: str = ''
    method: str = 'none'
    status: str = 'unreadable'
    warnings: list[str] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class ParsedDocument:
    name: str
    strategy: str
    pages: list[ParsedPage]
    assets: list[VisualAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tools: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    @property
    def text_pages(self):
        return [page.text for page in self.pages]

    @property
    def status(self):
        if not any(page.text.strip() for page in self.pages):
            return 'unreadable'
        return 'good' if all(page.status == 'good' for page in self.pages) else 'needs_review'

    def report(self):
        readable = sum(bool(p.text.strip()) for p in self.pages)
        good = sum(p.status == 'good' for p in self.pages)
        return {'version': PARSER_VERSION, 'name': self.name, 'strategy': self.strategy,
                'status': self.status, 'pages': [asdict(p) for p in self.pages],
                'assets': [a.metadata() for a in self.assets],
                'warnings': self.warnings, 'tools': self.tools,
                'elapsed_seconds': round(self.elapsed_seconds, 4),
                'metrics': {'page_count': len(self.pages), 'pages_with_text': readable,
                            'pages_passing_heuristics': good,
                            'text_extraction_coverage': readable / len(self.pages) if self.pages else 0,
                            'recognition_accuracy': None,
                            'accuracy_basis': 'No ground truth supplied. Coverage and confidence are not accuracy.'},
                'limitations': ['Reading order and table structure are not guaranteed.',
                                'Formula transcription has not been semantically validated.',
                                'Images and vector diagrams are preserved visually; no image captions are invented.']}


def _tool(name):
    return shutil.which(name) or (str(Path('/opt/homebrew/bin') / name)
                                 if (Path('/opt/homebrew/bin') / name).is_file() else None)


def parser_capabilities():
    return {'version': PARSER_VERSION, 'strategies': list(STRATEGIES),
            'pypdf': PYPDF_VERSION,
            **{name: bool(_tool(name)) for name in ('pdftotext', 'pdftoppm', 'pdfimages', 'tesseract')},
            'rapidocr': _rapidocr_paths()[0].is_file() and _rapidocr_paths()[1].is_file(),
            'vision': False, 'vision_note': 'Apple Vision is not integrated; no remote OCR is used.'}


def _rapidocr_paths():
    root = Path(__file__).resolve().parents[1]
    return root / '.venv-parsing/bin/python', root / 'scripts/rapidocr_worker.py'


class _Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def run(self, command, *, timeout=45, **kwargs):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('document_time_limit')
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=min(timeout, remaining),
                                    check=False, env={**os.environ, 'OMP_THREAD_LIMIT': '1'}, **kwargs)
        except subprocess.TimeoutExpired:
            raise TimeoutError('document_time_limit') from None
        if result.returncode:
            # Executable stderr can include source text and local paths. Keep it out of reports.
            raise ValueError('local_tool_failed:' + Path(command[0]).name)
        return result.stdout


def _text_metrics(text):
    meaningful = sum(ch.isalnum() for ch in text)
    nonspace = sum(not ch.isspace() for ch in text)
    invalid = sum(ch == '\ufffd' or unicodedata.category(ch) in ('Co', 'Cs') or
                  (unicodedata.category(ch) == 'Cc' and ch not in '\n\r\t') for ch in text)
    return {'character_count': len(text), 'meaningful_characters': meaningful,
            'replacement_or_control_ratio': invalid / max(nonspace, 1)}


def _adequate(text):
    metrics = _text_metrics(text)
    return metrics['meaningful_characters'] >= 36 and metrics['replacement_or_control_ratio'] <= 0.02


def _assess(page):
    page.metrics.update(_text_metrics(page.text))
    if not page.text.strip():
        page.status = 'unreadable'
        page.warnings.append('no_text_extracted')
    elif not _adequate(page.text):
        page.status = 'needs_review'
        page.warnings.append('sparse_or_suspicious_text_requires_review')
    elif page.metrics.get('ocr_mean_word_confidence', 100) < 50:
        page.status = 'needs_review'
        page.warnings.append('low_ocr_confidence_requires_review')
    else:
        page.status = 'good'
    page.warnings = list(dict.fromkeys(page.warnings))


def _image_dimensions(raw):
    """Read PNG/JPEG dimensions before any decoder is started."""
    if raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw) >= 33 and raw[12:16] == b'IHDR':
        return (*struct.unpack('>II', raw[16:24]), 'image/png')
    if raw.startswith(b'\xff\xd8'):
        pos = 2
        while pos + 3 < len(raw):
            if raw[pos] != 255:
                pos += 1
                continue
            while pos < len(raw) and raw[pos] == 255:
                pos += 1
            if pos >= len(raw):
                break
            marker = raw[pos]
            pos += 1
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xDA or pos + 2 > len(raw):
                break
            length = struct.unpack('>H', raw[pos:pos + 2])[0]
            if length < 2 or pos + length > len(raw):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                height, width = struct.unpack('>HH', raw[pos + 3:pos + 7])
                return width, height, 'image/jpeg'
            pos += length
    raise ValueError('照片格式无法识别，请使用完整的 PNG 或 JPEG 文件。')


def _native_pdf(path, max_pages, deadline):
    try:
        output = deadline.run([sys.executable, str(Path(__file__).resolve()), '--native',
                               str(path), str(max_pages)], timeout=60)
    except TimeoutError:
        raise ValueError('PDF 解析超时，请按章节拆分资料后重试。') from None
    result = json.loads(output)
    if 'error' in result:
        raise ValueError(result['error'])
    return result['pages']


def _native_worker(path, max_pages):
    # Isolate potentially expensive/deformed PDF text streams from the web worker.
    # macOS does not reliably implement RLIMIT_AS; parent-enforced timeout and input,
    # page, decoded-stream (pypdf), and output caps remain active on every platform.
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise ValueError('暂不支持加密 PDF。')
        if not 0 < len(reader.pages) <= max_pages:
            raise ValueError(f'单份资料需要包含 1 至 {max_pages} 页。')
        pages, text_budget = [], _MAX_TEXT_CHARS
        for page in reader.pages:
            warnings = []
            try:
                text = page.extract_text() or ''
            except Exception:
                text = ''
                warnings.append('native_text_extraction_failed')
            if len(text) > min(250_000, text_budget):
                text = text[:min(250_000, text_budget)]
                warnings.append('text_output_truncated')
            text_budget -= len(text)
            pages.append({'text': text, 'width': float(page.mediabox.width),
                          'height': float(page.mediabox.height), 'warnings': warnings})
        return {'pages': pages}
    except ValueError as error:
        return {'error': str(error)}
    except Exception:
        return {'error': 'PDF 无法解析，请检查文件是否完整。'}


def _ocr(image_path, deadline, languages, psm):
    executable = _tool('tesseract')
    if not executable:
        raise ValueError('ocr_dependency_missing:tesseract')
    if not re.fullmatch(r'[A-Za-z0-9_+-]{1,120}', languages):
        raise ValueError('invalid_ocr_languages')
    output = deadline.run([executable, str(image_path), 'stdout', '-l', languages,
                           '--psm', str(psm), 'tsv'], timeout=45).decode('utf-8', 'replace')
    lines, confidences = {}, []
    for row in csv.DictReader(io.StringIO(output), delimiter='\t', quoting=csv.QUOTE_NONE):
        word = (row.get('text') or '').strip()
        if not word or row.get('level') != '5':
            continue
        try:
            confidence = float(row['conf'])
            box = [int(row[k]) for k in ('left', 'top', 'width', 'height')]
        except (ValueError, KeyError):
            continue
        key = tuple(row.get(k) for k in ('page_num', 'block_num', 'par_num', 'line_num'))
        lines.setdefault(key, []).append((word, box))
        if confidence >= 0:
            confidences.append(confidence)
    blocks, texts, offset = [], [], 0
    for words in lines.values():
        text = ' '.join(word for word, _ in words)
        left = min(box[0] for _, box in words)
        top = min(box[1] for _, box in words)
        right = max(box[0] + box[2] for _, box in words)
        bottom = max(box[1] + box[3] for _, box in words)
        blocks.append({'kind': 'ocr_line', 'text': text,
                       'bbox': [left, top, right - left, bottom - top],
                       'coordinate_system': 'image_pixels_top_left',
                       'char_start': offset, 'char_end': offset + len(text)})
        texts.append(text)
        offset += len(text) + 1
    return '\n'.join(texts), blocks, {
        'ocr_mean_word_confidence': round(sum(confidences) / len(confidences), 3) if confidences else 0,
        'ocr_word_count': len(confidences), 'ocr_confidence_is_accuracy': False,
        'ocr_languages': languages, 'ocr_psm': psm}


def _rapidocr(image_path, deadline, max_pixels):
    python, worker = _rapidocr_paths()
    if not python.is_file() or not worker.is_file():
        raise ValueError('ocr_dependency_missing:rapidocr')
    target = image_path.with_name(image_path.stem + '-rapidocr.json')
    deadline.run([str(python), str(worker), '--input', str(image_path), '--output', str(target),
                  '--max-pixels', str(max_pixels)], timeout=60)
    if target.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('ocr_output_limit')
    result = json.loads(target.read_text(encoding='utf-8'))
    blocks, offset = [], 0
    for entry in result['blocks']:
        text = entry['text']
        left, top, right, bottom = entry['bbox']
        blocks.append({'kind': 'ocr_line', 'text': text, 'bbox': [left, top, right - left, bottom - top],
                       'coordinate_system': 'image_pixels_top_left', 'polygon': entry['polygon'],
                       'char_start': offset, 'char_end': offset + len(text),
                       'recognizer_confidence': entry['confidence']})
        offset += len(text) + 1
    return result['text'], blocks, {
        'ocr_mean_word_confidence': 100 * (result['confidence'] or 0),
        'ocr_confidence_aggregation': 'character-weighted line confidence scaled to 0-100',
        'ocr_confidence_is_accuracy': False, 'ocr_engine': result['versions'],
        'ocr_image_width': result['width'], 'ocr_image_height': result['height'],
        'ocr_engine_seconds': result['duration_seconds'], 'ocr_cold_engine': result['cold_engine']}


def _render_page(source, number, target, deadline, max_pixels):
    executable = _tool('pdftoppm')
    if not executable:
        raise ValueError('preview_dependency_missing:pdftoppm')
    # A square bound limits all aspect ratios, including malformed giant media boxes.
    side = min(2400, int(math.sqrt(max_pixels)))
    prefix = target.with_suffix('')
    deadline.run([executable, '-f', str(number), '-l', str(number), '-singlefile',
                  '-r', '220', '-scale-to', str(side), '-png', str(source), str(prefix)], timeout=30)
    return prefix.with_suffix('.png')


def _add_asset(document, page, raw, kind, width=None, height=None, mime='image/png'):
    if len(document.assets) >= 120 or sum(len(a.data) for a in document.assets) + len(raw) > _MAX_ASSET_BYTES:
        page.warnings.append('visual_asset_storage_limit')
        return
    key = hashlib.sha256(f'{page.number}:{kind}:'.encode() + raw).hexdigest()[:32]
    if any(a.id == key for a in document.assets):
        return
    document.assets.append(VisualAsset(key, page.number, kind, mime, raw, width, height))
    page.asset_ids.append(key)


def _embedded_images(source, document, temp, deadline, max_pixels, max_asset_pages):
    executable = _tool('pdfimages')
    if not executable:
        document.warnings.append('embedded_image_dependency_missing:pdfimages')
        return
    try:
        inventory = deadline.run([executable, '-list', str(source)], timeout=15).decode('utf-8', 'replace')
    except (ValueError, TimeoutError) as error:
        document.warnings.append(str(error))
        return
    entries = {}
    for line in inventory.splitlines():
        fields = line.split()
        if len(fields) < 5 or not fields[0].isdigit():
            continue
        try:
            entries.setdefault(int(fields[0]), []).append((int(fields[3]), int(fields[4])))
        except ValueError:
            continue
    for number, dimensions in entries.items():
        if not 1 <= number <= len(document.pages):
            continue
        page = document.pages[number - 1]
        page.warnings.append('embedded_images_not_semantically_interpreted')
        if sum(len(asset.data) for asset in document.assets) >= _MAX_ASSET_BYTES or len(document.assets) >= 120:
            page.warnings.append('visual_asset_storage_limit')
            continue
        if (number > max_asset_pages or len(dimensions) > 8 or
                sum(w * h for w, h in dimensions) > max_pixels * 2 or
                any(w <= 0 or h <= 0 or w * h > max_pixels for w, h in dimensions)):
            page.warnings.append('embedded_image_extraction_limit_use_original_pdf')
            continue
        prefix = temp / f'embedded-{number}'
        try:
            deadline.run([executable, '-f', str(number), '-l', str(number), '-png',
                          str(source), str(prefix)], timeout=20)
            for image_path in sorted(temp.glob(prefix.name + '-*'))[:8]:
                if image_path.suffix != '.png' or image_path.stat().st_size > _MAX_ASSET_BYTES:
                    continue
                raw = image_path.read_bytes()
                w, h, mime = _image_dimensions(raw)
                _add_asset(document, page, raw, 'embedded_image', w, h, mime)
        except (ValueError, TimeoutError, OSError) as error:
            page.warnings.append(str(error) if isinstance(error, (ValueError, TimeoutError)) else 'embedded_image_extraction_failed')
        finally:
            for image_path in temp.glob(prefix.name + '-*'):
                image_path.unlink(missing_ok=True)


def parse_document_structured(name, raw, max_pages=300, *, strategy='adaptive_local_v1',
                              ocr_engine='tesseract', ocr_languages='chi_sim+eng',
                              max_ocr_pages=30, max_asset_pages=30, max_pixels=16_000_000,
                              timeout_seconds=120, preserve_visuals=True):
    """Extract pages without silently discarding unreadable pages or figures.

    ``status=good`` means heuristic extraction checks passed, not that the OCR is
    correct. PNG/JPEG inputs remain inspectable even when no OCR tool is installed.
    The PDF's original text remains unchanged on the adaptive native fast path.
    """
    if strategy not in STRATEGIES:
        raise ValueError('未知文档解析策略。')
    if ocr_engine not in ('tesseract', 'rapidocr'):
        raise ValueError('当前本地 OCR 引擎支持 tesseract 和 rapidocr；未调用远程识别服务。')
    if not raw or len(raw) > _MAX_INPUT_BYTES:
        raise ValueError('资料不能为空，且大小不能超过 25 MiB。')
    if not 1 <= max_pages <= 1000 or not 0 <= max_ocr_pages <= 100 or not 0 <= max_asset_pages <= 100:
        raise ValueError('无效的解析页数限制。')
    if not 65_536 <= max_pixels <= 32_000_000 or not 1 <= timeout_seconds <= 600:
        raise ValueError('无效的解析资源限制。')
    started = time.monotonic()
    if not _PARSE_SLOTS.acquire(timeout=min(timeout_seconds, 10)):
        raise ValueError('文档解析繁忙，请稍后重试。')
    try:
        with tempfile.TemporaryDirectory(prefix='fyp-parse-') as directory:
            return _parse(name, raw, max_pages, strategy, ocr_engine, ocr_languages, max_ocr_pages,
                          max_asset_pages, max_pixels, timeout_seconds, preserve_visuals,
                          Path(directory), started)
    finally:
        _PARSE_SLOTS.release()


def _parse(name, raw, max_pages, strategy, ocr_engine, languages, max_ocr_pages, max_asset_pages,
           max_pixels, timeout_seconds, preserve_visuals, temp, started):
    suffix = Path(name).suffix.lower()
    deadline = _Deadline(timeout_seconds)
    document = ParsedDocument(Path(name).name, strategy, [], tools=parser_capabilities())
    selected_ocr = ('rapidocr' if strategy == 'rapidocr_v1' else 'tesseract'
                    if strategy in ('tesseract_v1', 'tesseract_psm6_v1') else ocr_engine)
    if suffix in ('.txt', '.md'):
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('文本文件请保存为 UTF-8 编码。') from None
        page = ParsedPage(1, text[:_MAX_TEXT_CHARS], 'utf8-sig-v1')
        if len(text) > _MAX_TEXT_CHARS:
            page.warnings.append('text_output_truncated')
        _assess(page)
        document.pages = [page]
    elif suffix in ('.png', '.jpg', '.jpeg'):
        width, height, mime = _image_dimensions(raw)
        if width <= 0 or height <= 0 or width * height > max_pixels:
            raise ValueError(f'照片像素数不能超过 {max_pixels:,}，请先缩小图片。')
        source = temp / ('source.png' if mime == 'image/png' else 'source.jpg')
        source.write_bytes(raw)
        page = ParsedPage(1)
        document.pages = [page]
        if preserve_visuals:
            _add_asset(document, page, raw, 'source_image', width, height, mime)
        if strategy in ('pypdf_v1', 'poppler_layout_v1'):
            page.method = strategy
            page.warnings.append('image_has_no_native_text_layer')
        elif max_ocr_pages == 0:
            page.warnings.append('ocr_page_limit')
        else:
            _apply_ocr(page, source, deadline, languages, 6 if strategy == 'tesseract_psm6_v1' else 3,
                       selected_ocr, max_pixels)
        page.warnings.append('image_semantics_not_interpreted')
        _assess(page)
    elif suffix == '.pdf':
        if not raw.lstrip()[:1024].startswith(b'%PDF-'):
            raise ValueError('PDF 文件头无法识别，请检查文件是否完整。')
        source = temp / 'source.pdf'
        source.write_bytes(raw)
        native = _native_pdf(source, max_pages, deadline)
        document.pages = [ParsedPage(i + 1, entry['text'], 'pypdf_v1',
                                     warnings=list(entry['warnings'])) for i, entry in enumerate(native)]
        if strategy == 'poppler_layout_v1':
            executable = _tool('pdftotext')
            if not executable:
                for page in document.pages:
                    page.text = ''
                    page.method = strategy
                    page.warnings.append('native_dependency_missing:pdftotext')
            else:
                try:
                    text = deadline.run([executable, '-layout', '-enc', 'UTF-8', str(source), '-'], timeout=60).decode('utf-8', 'replace')
                    texts = text.split('\f')
                    for index, page in enumerate(document.pages):
                        page.text = texts[index] if index < len(texts) else ''
                        page.method = strategy
                except (ValueError, TimeoutError) as error:
                    for page in document.pages:
                        page.text, page.method = '', strategy
                        page.warnings.append(str(error))
        ocr_used = 0
        for page in document.pages:
            force_ocr = strategy in ('tesseract_v1', 'tesseract_psm6_v1', 'rapidocr_v1')
            needs_ocr = force_ocr or (strategy == 'adaptive_local_v1' and not _adequate(page.text))
            if force_ocr:
                page.text = ''
                page.method = strategy
            raster = None
            if (needs_ocr and ocr_used < max_ocr_pages) or (preserve_visuals and page.number <= max_asset_pages):
                try:
                    raster = _render_page(source, page.number, temp / f'page-{page.number}.png', deadline, max_pixels)
                except (ValueError, TimeoutError, OSError) as error:
                    page.warnings.append(str(error) if isinstance(error, (ValueError, TimeoutError)) else 'page_render_failed')
            if needs_ocr:
                if ocr_used >= max_ocr_pages:
                    page.warnings.append('ocr_page_limit')
                elif raster:
                    ocr_used += 1
                    original = (page.text, page.method)
                    _apply_ocr(page, raster, deadline, languages, 6 if strategy == 'tesseract_psm6_v1' else 3,
                               selected_ocr, max_pixels)
                    if not force_ocr and (not page.text.strip() or _text_metrics(page.text)['meaningful_characters'] < _text_metrics(original[0])['meaningful_characters']):
                        page.text, page.method = original
                        page.blocks = []
                        page.warnings.append('ocr_did_not_improve_sparse_native_text')
            if preserve_visuals:
                if raster and page.number <= max_asset_pages:
                    preview = raster.read_bytes()
                    width, height, mime = _image_dimensions(preview)
                    _add_asset(document, page, preview, 'page_preview', width, height, mime)
                    page.warnings.append('page_visuals_preserved_not_semantically_interpreted')
                elif page.number > max_asset_pages:
                    page.warnings.append('preview_page_limit_use_original_pdf')
            if raster:
                raster.unlink(missing_ok=True)
                raster.with_name(raster.stem + '-rapidocr.json').unlink(missing_ok=True)
            _assess(page)
        if preserve_visuals:
            _embedded_images(source, document, temp, deadline, max_pixels, max_asset_pages)
    else:
        raise ValueError('支持 PDF、TXT、Markdown、PNG 和 JPEG。')
    remaining_text = _MAX_TEXT_CHARS
    for page in document.pages:
        text_limit = min(250_000, remaining_text)
        if len(page.text) > text_limit:
            page.text = page.text[:text_limit]
            page.blocks = []
            page.warnings.append('text_output_truncated')
            _assess(page)
        remaining_text -= len(page.text)
        if page.text and not page.blocks:
            page.blocks = [{'kind': 'page_text', 'text': page.text,
                            'char_start': 0, 'char_end': len(page.text), 'bbox': None}]
        if 'text_output_truncated' in page.warnings and page.status == 'good':
            page.status = 'needs_review'
        page.warnings = list(dict.fromkeys(page.warnings))
    document.elapsed_seconds = time.monotonic() - started
    return document


def _apply_ocr(page, source, deadline, languages, psm, engine='tesseract', max_pixels=16_000_000):
    try:
        text, blocks, metrics = (_rapidocr(source, deadline, max_pixels) if engine == 'rapidocr'
                                 else _ocr(source, deadline, languages, psm))
        page.text, page.blocks = text, blocks
        page.method = 'rapidocr_v1' if engine == 'rapidocr' else ('tesseract_psm6_v1' if psm == 6 else 'tesseract_v1')
        page.metrics.update(metrics)
        page.warnings.extend(['ocr_text_requires_validation', 'table_formula_structure_unverified'])
    except (ValueError, TimeoutError, OSError) as error:
        page.warnings.append(str(error) if isinstance(error, (ValueError, TimeoutError)) else 'ocr_failed')


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--native':
    print(json.dumps(_native_worker(sys.argv[2], int(sys.argv[3])), ensure_ascii=False))
