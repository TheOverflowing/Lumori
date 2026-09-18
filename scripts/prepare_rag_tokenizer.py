"""Install only the exact public tokenizer used by the selected R4 profile."""
import hashlib
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = '1d8ad4ca9b3dd8059ad90a75d4983776a23d44af'
SHA256 = '83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d'
URL = f'https://huggingface.co/Qwen/Qwen3-Embedding-8B/resolve/{REVISION}/tokenizer.json'


def main():
    path = ROOT/'.rag-models/embedding-tokenizer/tokenizer.json'
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == SHA256:
        print('Verified existing R4 tokenizer. No download needed.')
        return
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('Downloaded tokenizer does not match the R4 hash; existing file preserved.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.download')
    temporary.write_bytes(data)
    temporary.replace(path)
    print('Installed and verified R4 tokenizer; model weights are not needed by the app.')


if __name__ == '__main__':
    main()
