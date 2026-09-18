"""Isolated production adapters. No API credentials are passed to this process."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path


def mineru(source, output, tier):
    from mineru.parser import parse
    from mineru.parser.writer import FileBasedDataWriter
    import onnxruntime
    if importlib.metadata.version('mineru')!='4.0.2':raise RuntimeError('MinerU version must be revalidated before upgrade')
    onnxruntime.disable_telemetry_events()
    result = parse(source, tier=tier,
                   ocr_mode='auto', image_analysis=False)
    result.save(FileBasedDataWriter(str(output)))


def figures(source, output, models, limit):
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.datamodel.base_models import InputFormat, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    from docling_core.types.doc import PictureItem
    options = PdfPipelineOptions(artifacts_path=Path(models), do_ocr=False, do_table_structure=False,
        generate_page_images=True, generate_picture_images=True, images_scale=1.5,
        enable_remote_services=False, allow_external_plugins=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4))
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.IMAGE], format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        InputFormat.IMAGE: ImageFormatOption(pipeline_cls=StandardPdfPipeline, pipeline_options=options)})
    result = converter.convert(source, raises_on_error=False)
    if result.status != ConversionStatus.SUCCESS:
        raise RuntimeError('Incomplete Docling conversion')
    doc = result.document
    output.mkdir(parents=True, exist_ok=True)
    assets = []
    total = 0
    for item, _ in doc.iterate_items():
        if not isinstance(item, PictureItem) or not item.prov:
            continue
        total += 1
        if len(assets) >= limit:
            continue
        picture = item.get_image(doc)
        if picture is None:
            continue
        prov = item.prov[0]
        page = doc.pages[prov.page_no]
        box = prov.bbox.to_top_left_origin(page.size.height)
        picture.thumbnail((1600, 1600))
        filename = f'figure-{len(assets)+1}.png'
        picture.convert('RGB').save(output / filename)
        assets.append(dict(file=filename, page=prov.page_no, width=picture.width, height=picture.height,
            bbox=[box.l/page.size.width, box.t/page.size.height, box.r/page.size.width, box.b/page.size.height],
            original_caption=item.caption_text(doc)[:4000], kind='figure'))
    # A standalone photograph is itself a useful visual source even if layout
    # detection finds no embedded picture region.
    if source.suffix.lower() != '.pdf' and not assets:
        from PIL import Image, ImageOps
        with Image.open(source) as raw:
            picture = ImageOps.exif_transpose(raw).convert('RGB')
            picture.thumbnail((1600, 1600))
            picture.save(output/'figure-1.png')
            assets.append(dict(file='figure-1.png',page=1,width=picture.width,height=picture.height,
                bbox=[0,0,1,1],original_caption='',kind='figure'))
    (output/'figures.json').write_text(json.dumps(dict(assets=assets,detected=total,
        truncated=total>limit,version=importlib.metadata.version('docling'))))


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=['mineru','figures']);p.add_argument('source',type=Path)
    p.add_argument('output',type=Path);p.add_argument('--tier',default='standard',choices=['standard','advanced'])
    p.add_argument('--models');p.add_argument('--limit',type=int,default=24)
    a=p.parse_args()
    if a.mode=='mineru':mineru(a.source,a.output,a.tier)
    else:figures(a.source,a.output,a.models,a.limit)
