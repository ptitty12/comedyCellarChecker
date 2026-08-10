FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    # optional: browser TLS fingerprint, helps if Cloudflare gets picky.
    # The checker falls back to plain requests if this isn't installed.
    && (pip install 'curl_cffi>=0.6' || echo "curl_cffi unavailable, continuing without it")

COPY checker.py .

RUN useradd --create-home runner \
    && mkdir -p /data \
    && chown runner:runner /data
USER runner

VOLUME ["/data"]

# Healthy = the main loop wrote its state file recently.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=5 \
    CMD ["python", "checker.py", "--health"]

CMD ["python", "-u", "checker.py"]
