FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WATERMARK_DATA_DIR=/app/data

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static
COPY docker/entrypoint.sh /usr/local/bin/watermark-entrypoint

RUN mkdir -p /app/data/uploads /app/data/results \
    && useradd --create-home --uid 10001 watermark \
    && chmod 0755 /usr/local/bin/watermark-entrypoint \
    && chown -R watermark:watermark /app

VOLUME ["/app/data"]
EXPOSE 8655

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8655/api/health', timeout=3).read()"

ENTRYPOINT ["/usr/local/bin/watermark-entrypoint"]
CMD ["gunicorn", "--bind", "0.0.0.0:8655", "--workers", "2", "--timeout", "120", "app:app"]
