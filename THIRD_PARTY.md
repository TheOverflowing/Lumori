# Third-party components

Lumori installs these components from their upstream distributions; their licenses are not replaced by this repository. Model weights are fetched during deployment, not redistributed here. Check upstream terms before commercial use or redistribution.

- MinerU 4.0.2: https://github.com/opendatalab/MinerU
- MinerU ONNX models: https://huggingface.co/opendatalab/MinerU-4_models_onnx
- MinerU GGUF model: https://huggingface.co/jinzhenj/MinerU2.5-Pro-2605-1.2B-GGUF
- Docling 2.128.0: https://github.com/docling-project/docling
- Docling layout model: https://huggingface.co/docling-project/docling-layout-heron
- Qwen3 embedding tokenizer: https://huggingface.co/Qwen/Qwen3-Embedding-8B

Model revisions and SHA-256 values are pinned in `deploy/models.json` and `scripts/prepare_rag_tokenizer.py`. API providers are separate hosted services configured by the deployer.
