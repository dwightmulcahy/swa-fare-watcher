# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SWA_DATA_DIR=/data \
    HOME=/tmp \
    PORT=8080 \
    TZ=UTC

WORKDIR /app
COPY requirements.txt ./
# Chromium + its OS libraries, and Xvfb so the browser runs headed (far less likely to be blocked).
# The app starts its own Xvfb display per run.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends xvfb tzdata \
 && pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --home-dir /home/app app \
 && mkdir -p /data && chown app /data

# Files owned by root, process runs as non-root: app cannot modify itself
COPY app ./app
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status != 200)"
ENTRYPOINT ["python", "-m", "app"]
CMD ["serve"]
