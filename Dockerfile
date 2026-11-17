# ---- build stage: resolve and install dependencies into a virtualenv --------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[local-embeddings,ocr]"

# ---- runtime stage: slim image, non-root user ----------------------------------------
# One image, several roles (see docker-compose.yml):
#   api      uvicorn corpus.main:app
#   worker   corpus-worker
#   migrate  alembic upgrade head
#   demo     corpus-demo (one-off: index sample documents, print a demo link)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PATH="/venv/bin:$PATH" HF_HOME=/models FASTEMBED_CACHE_PATH=/models
# Tesseract for scanned PDFs (English, French, German models).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-fra tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 corpus && mkdir /models && chown corpus /models
COPY --from=build /venv /venv
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY eval/dataset ./eval/dataset
USER corpus
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8000\")}/health')"
# PORT is honoured for platforms that assign one (Render, Fly, Railway...).
CMD ["sh", "-c", "exec uvicorn corpus.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
