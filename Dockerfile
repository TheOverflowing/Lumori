FROM python:3.12-slim-bookworm AS core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/srv/lumori/data RAG_TOKENIZER_PATH=/srv/lumori/models/tokenizer/tokenizer.json \
    MINERU_PYTHON=/opt/mineru/bin/python MINERU_RUNTIME=/srv/lumori/models/mineru \
    DOCLING_PYTHON=/opt/docling/bin/python DOCLING_MODELS=/srv/lumori/models/docling
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libgl1 libglib2.0-0 libgomp1 poppler-utils tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/lumori
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt
COPY app ./app
COPY scripts ./scripts
COPY deploy ./deploy
RUN useradd --uid 10001 --create-home lumori \
    && mkdir -p /srv/lumori/data /srv/lumori/models \
    && chown -R lumori:lumori /srv/lumori
USER lumori
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]

FROM core AS full
USER root
RUN python -m venv /opt/mineru && /opt/mineru/bin/pip install \
    mineru==4.0.2 mineru-llama-cpp==0.1.2 mineru-vl-utils==2.0.4 onnxruntime==1.30.0
RUN python -m venv /opt/docling \
    && /opt/docling/bin/pip install torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cpu \
    && /opt/docling/bin/pip install docling==2.128.0
USER lumori
