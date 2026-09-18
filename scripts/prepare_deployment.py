"""Download pinned deployment assets, verifying checksums before activation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request
from prepare_rag_tokenizer import URL, SHA256

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download(url, path, expected):
    if path.is_file() and digest(path) == expected:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.partial')
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as output:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                output.write(block)
        if digest(temporary) != expected:
            raise ValueError(f'Checksum mismatch: {path.name}')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['core', 'full'], default='full')
    parser.add_argument('--root', type=Path, default=Path('/srv/lumori/models'))
    args = parser.parse_args()
    root = args.root.resolve()
    download(URL, root/'tokenizer/tokenizer.json', SHA256)
    if args.mode == 'full':
        for model in json.loads((ROOT/'deploy/models.json').read_text()):
            directory = root/model['directory']
            for item in model['files']:
                url = f"https://huggingface.co/{model['repo']}/resolve/{model['revision']}/{item['path']}"
                print(f"Checking {model['repo']}: {item['path']}", flush=True)
                download(url, directory/item['path'], item['sha256'])
            if model['directory'].startswith('mineru/'):
                (directory/'.mineru_complete').touch()
        # JSON is valid YAML; absolute paths refer to the target runtime, never the source Mac.
        config = {'model': {'base_dir': str(root/'mineru/models'), 'source': 'local',
                           'small_backend': 'onnx', 'vlm': {'engine': 'llama-cpp', 'max_concurrency': 2}},
                  'llm_aided': {'features': {'title_leveling': False, 'cross_page_table_cell_merge': False}}}
        (root/'mineru/config.yaml').write_text(json.dumps(config, indent=2)+'\n')
    print(f'Deployment assets verified ({args.mode}).', flush=True)


if __name__ == '__main__':
    main()
