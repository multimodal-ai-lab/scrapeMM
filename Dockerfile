# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------------
# Stage 1: build the Nuxt UI into static files.
#
# This is why the server is not a pip install: the UI is a build artifact, and nobody
# should need a Node toolchain to run scrapeMM.
# ---------------------------------------------------------------------------------
# The registry prefix is spelled out on purpose. Docker silently expands a bare
# `node:22-alpine` to `docker.io/library/...`; Podman instead walks the
# `unqualified-search-registries` list from registries.conf, which on RHEL/Fedora starts
# with registry.redhat.io -- a registry that rejects anonymous pulls. The build then dies
# on "unable to retrieve auth token: invalid username/password" for an image that is
# public, and never reaches Docker Hub at all. A fully qualified name removes the guess.
FROM docker.io/library/node:22-alpine AS ui

WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
# The committed lockfile pins the whole dependency tree, so a build resolves to the
# same versions rather than to whatever the registry happens to serve that minute --
# which is how an upstream republish once broke this stage mid-build.
#
# `npm install` rather than `npm ci` on purpose: it honours the lockfile when the two
# agree and repairs it when they drift, where `npm ci` fails the build outright. npm
# has been known to emit a lockfile its own `ci` then rejects, and a build that stops
# for that is worse than one that resolves a package differently.
RUN npm install --no-audit --no-fund

COPY ui/ ./
RUN npm run generate


# ---------------------------------------------------------------------------------
# Stage 2: the server itself.
#
# Playwright's image is the base because it already carries Chromium and every system
# library it needs -- the part a pip install cannot provide.
# ---------------------------------------------------------------------------------
# The JavaScript runtime yt-dlp needs for YouTube, as a stage of its own. A named stage
# rather than `COPY --from=<image>`: Podman's builder does not pull an image referenced
# only there, and fails with "image not found", while a FROM image it pulls like any
# other. The `bin` flavour holds nothing but the static binary.
FROM docker.io/denoland/deno:bin-2.6.0 AS deno

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

# A JavaScript runtime for yt-dlp: YouTube hides its video formats behind signature and
# "n" challenges that yt-dlp solves by running YouTube's own player code. Without one,
# formats go missing ("n challenge solving failed") or only thumbnails remain. Deno is
# the runtime yt-dlp uses by default; its solver scripts come with yt-dlp[default].
# Copied as the single static binary from the official image (the `deno` stage above).
COPY --from=deno /deno /usr/local/bin/deno
RUN deno --version

# PO tokens for YouTube, minted locally by the bgutil provider's script, which yt-dlp's
# plugin (bgutil-ytdlp-pot-provider in requirements-server.txt, same version) runs with
# Deno. They unlock the mobile web client, the fallback when YouTube asks the default
# clients to prove they are no bot. Only the runtime dependencies are kept: the linters,
# TypeScript and SWC builds among the installed packages are dead weight (115 MB).
# DENO_WEBGPU_BACKEND: without it, Deno's WebGPU setup panics on a machine without a GPU.
ARG BGUTIL_VERSION=2.0.0
ENV BGUTIL_SERVER_HOME=/opt/bgutil-ytdlp-pot-provider/server \
    DENO_WEBGPU_BACKEND=vulkan
RUN git clone --quiet --depth 1 --branch ${BGUTIL_VERSION} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd ${BGUTIL_SERVER_HOME} \
    && deno install --frozen --entrypoint src/generate_once.ts --allow-scripts=npm:canvas@3.2.3 \
    && cd node_modules/.deno \
    && rm -rf @swc+* typescript@* prettier@* eslint@* eslint-* @eslint* @typescript-eslint* \
              typescript-eslint* swc-node*
# Deno's cache of the compiled script stays (in /root/.cache/deno): without it, the
# first token of a fresh container took long enough that yt-dlp gave up on it and fell
# back to a 360p format.

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
