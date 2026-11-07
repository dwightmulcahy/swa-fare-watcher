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

ARG TARGETARCH
WORKDIR /app
COPY requirements.txt ./
# Browser + its OS libraries, and Xvfb so it runs headed (far less likely to be blocked).
# amd64 gets real Google Chrome (the app prefers it); Chrome has no Linux arm64 build, so
# arm64 uses Playwright's Chromium. The app starts its own Xvfb display per run.
# pip is removed afterwards: the runtime doesn't need it, and its vendored packages trip
# the vulnerability scan.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends xvfb tzdata \
 && pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && if [ "${TARGETARCH:-$(dpkg --print-architecture)}" = "amd64" ]; then playwright install --with-deps chrome; fi \
 && apt-get install -y --no-install-recommends fonts-liberation fonts-noto-color-emoji \
 && { pip uninstall -y setuptools wheel || true; } \
 && pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* /root/.cache \
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
