# neonize ships a prebuilt Go/whatsmeow binary, so no Chromium/Selenium is needed.
# It is built against glibc -> use a Debian-based image (not Alpine).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libmagic1: file-type detection used by neonize (python-magic).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

# Render injects $PORT (default 10000); main.py reads it.
EXPOSE 10000
CMD ["python", "main.py"]
