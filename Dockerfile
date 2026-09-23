# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------------
# Stage 1: build the Nuxt UI into static files.
#
# This is why the server is not a pip install: the UI is a build artifact, and nobody
# should need a Node toolchain to run scrapeMM.
# ---------------------------------------------------------------------------------
FROM node:22-alpine AS ui

WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY ui/ ./
RUN npm run generate


# ---------------------------------------------------------------------------------
# Stage 2: the server itself.
#
# Playwright's image is the base because it already carries Chromium and every system
# library it needs -- the part a pip install cannot provide.
# ---------------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble AS server

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCRAPEMM_CONFIG_DIR=/data/config \
    EZMM=/data/media \
    DISPLAY=:99

# ffmpeg merges the separate audio and video streams that YouTube and Facebook serve,
# and normalizes videos for browser playback (which additionally needs ffprobe).
# xvfb and x11vnc are what let a human solve Archive.today's CAPTCHA through the web
# UI: the browser must stay on this machine, because the session it earns is bound to
# this browser and this IP address.
# python3-tk is needed by SeleniumBase's CDP driver, which imports tkinter on its way
# to starting the undetected browser. x11-utils provides xdpyinfo, which the entrypoint
# uses to wait until the display actually accepts connections.
RUN apt-get update && apt-get install --no-install-recommends -y \
        ffmpeg \
        xvfb \
        x11vnc \
        x11-utils \
        python3-tk \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-server.txt pyproject.toml README.md ./
COPY scrapemm/ ./scrapemm/
# Resolved in one go: the server's dependencies and the package's own (which the client
# half needs too -- ezmm, aiohttp, tqdm) share constraints, so letting pip see both at
# once is what keeps them consistent.
#
# `pip install .` is what pulls those dependencies in; the package it installs alongside
# them holds the client only, because the distribution deliberately excludes the server
# (see pyproject.toml). PYTHONPATH then puts this source tree ahead of site-packages, so
# `scrapemm` resolves here -- one copy of the code, client and server both present --
# rather than depending on how a particular setuptools release maps an editable install.
RUN pip install --no-cache-dir . -r requirements-server.txt
ENV PYTHONPATH=/app

# The built UI, served by the same FastAPI app at /
COPY --from=ui /ui/.output/public ./scrapemm/server/ui

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /data/config /data/media

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# tini reaps the Xvfb and x11vnc children, which would otherwise pile up as zombies
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
