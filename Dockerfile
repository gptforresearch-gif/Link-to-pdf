FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PDF_DIR=/tmp/linkpdf

WORKDIR /srv

# --- Browser layer -----------------------------------------------------------
# Chromium plus its system libraries is a few hundred MB and rarely changes.
# It is installed on its own, BEFORE requirements.txt is copied, so that adding
# or changing a Python package no longer forces the browser to download again.
# Keep this version in step with the playwright pin in requirements.txt.
RUN pip install --no-cache-dir playwright==1.49.1 \
 && playwright install --with-deps chromium \
 && apt-get update \
 && apt-get install -y --no-install-recommends fonts-indic fonts-noto-core \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# --- Application dependencies ------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 75
