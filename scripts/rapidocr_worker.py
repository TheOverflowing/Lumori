"""Pinned, local OCR worker for the optional .venv-parsing runtime.

Uses packaged PP-OCRv6 small ONNX models, with no runtime downloads or API calls.
Line boxes are observations, not table structure, formula markup or figure meaning.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import time

_ENGINE = None
_IDENTITY = None


def engine_identity():
    global _IDENTITY
    if _IDENTITY is None:
        import rapidocr
        folder = Path(rapidocr.__file__).parent / 'models'
        names = ['PP-OCRv6_det_small.onnx', 'PP-OCRv6_rec_small.onnx', 'ch_ppocr_mobile_v2.0_cls_mobile.onnx']
        if version('rapidocr') != '3.9.2' or any(not (folder / name).is_file() for name in names):
            raise RuntimeError('Install the pinned RapidOCR 3.9.2 package with bundled models first.')
        _IDENTITY = {'engine': 'rapidocr', 'version': version('rapidocr'), 'onnxruntime': version('onnxruntime'),
            'models': {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in names},
            'execution_provider': 'CPUExecutionProvider', 'intra_op_threads': 2, 'inter_op_threads': 1,
            'text_score_filter': 0.5, 'max_side_len': 2000}
    return _IDENTITY


def recognize(image_path, max_pixels=16_000_000):
    global _ENGINE
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr import RapidOCR
    import onnxruntime
    onnxruntime.disable_telemetry_events()
    started = time.perf_counter()
    identity = engine_identity()
    Image.MAX_IMAGE_PIXELS = max_pixels
    with Image.open(image_path) as source:
        if source.width * source.height > max_pixels or source.width < 1 or source.height < 1:
            raise ValueError('Image exceeds the configured pixel limit.')
        if getattr(source, 'n_frames', 1) != 1:
            raise ValueError('Only single-frame document photos are supported.')
        image = ImageOps.exif_transpose(source).convert('RGB')
        width, height = image.size
        pixels = np.asarray(image)[:, :, ::-1].copy()
    cold = _ENGINE is None
    if _ENGINE is None:
        import rapidocr
        folder = Path(rapidocr.__file__).parent / 'models'
        _ENGINE = RapidOCR(params={
            'Global.log_level': 'error', 'EngineConfig.onnxruntime.intra_op_num_threads': 2,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            'Det.model_path': str(folder / 'PP-OCRv6_det_small.onnx'),
            'Rec.model_path': str(folder / 'PP-OCRv6_rec_small.onnx'),
            'Cls.model_path': str(folder / 'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),
        })
    result = _ENGINE(pixels)
    blocks = []
    if result.txts is not None:
        for order, (text, score, quad) in enumerate(zip(result.txts, result.scores, result.boxes)):
            points = np.asarray(quad).tolist()
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            blocks.append({'text': text, 'confidence': float(score), 'bbox': [min(xs), min(ys), max(xs), max(ys)],
                'polygon': points, 'order': order, 'type': 'text_line', 'coordinate_system': 'pixels_top_left'})
    count = sum(len(block['text']) for block in blocks)
    confidence = sum(len(block['text']) * block['confidence'] for block in blocks) / count if count else None
    return {'text': '\n'.join(block['text'] for block in blocks), 'blocks': blocks,
        'confidence': confidence, 'width': width, 'height': height, 'versions': identity,
        'cold_engine': cold, 'duration_seconds': time.perf_counter() - started,
        'warnings': ['ocr_confidence_is_not_accuracy', 'reading_order_tables_formulas_and_figures_unverified']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-pixels', type=int, default=16_000_000)
    args = parser.parse_args()
    result = recognize(args.input, args.max_pixels)
    args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
