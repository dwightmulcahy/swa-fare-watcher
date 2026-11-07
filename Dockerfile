# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SWA_DATA_DIR=/data \
    HOME=/tmp \
    TZ=UTC

WORKDIR /app
COPY requirements.txt ./
# Chromium + its OS libraries, and Xvfb so the browser runs headed (far less likely to be blocked)
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends xvfb xauth tzdata \
 && pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --home-dir /home/app app \
 && mkdir -p /data && chown app /data

# Files owned by root, process runs as non-root: app cannot modify itself
COPY swa_watch.py config.example.yaml ./
USER app
VOLUME ["/data"]
HEALTHCHECK NONE
ENTRYPOINT ["xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", "python", "/app/swa_watch.py"]
CMD ["--daily-at", "07:20"]
