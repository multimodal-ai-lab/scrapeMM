#!/usr/bin/env bash
# Starts the virtual display, the VNC server that exposes it, and then scrapeMM.
#
# The display is not optional dressing: several integrations drive a real headed
# Chromium (archives serve bot checks to headless ones), and the Archive.today CAPTCHA
# panel needs a human to be able to see and click that browser. x11vnc exposes the
# display on localhost only -- the web UI reaches it through the server's own
# authenticated WebSocket, so the VNC port is never published.
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
export DISPLAY

SCREEN_SIZE="${SCRAPEMM_SCREEN_SIZE:-1440x900x24}"
VNC_PORT="${SCRAPEMM_VNC_PORT:-5900}"
PORT="${SCRAPEMM_PORT:-8080}"

cleanup() {
    [[ -n "${XVFB_PID:-}" ]] && kill "$XVFB_PID" 2>/dev/null || true
    [[ -n "${VNC_PID:-}" ]] && kill "$VNC_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting Xvfb on ${DISPLAY} (${SCREEN_SIZE})..."
Xvfb "$DISPLAY" -screen 0 "$SCREEN_SIZE" -nolisten tcp &
XVFB_PID=$!

# Wait for the display to accept connections before anything tries to use it
for _ in $(seq 1 50); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
    sleep 0.2
done

echo "Starting x11vnc on 127.0.0.1:${VNC_PORT}..."
# -forever: keep serving after a viewer disconnects, so the panel can be reopened
# -shared: several viewers may watch at once
# -nopw with -localhost: the socket is reachable only from inside the container; the
#   web UI's own API key is what actually guards it
x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -localhost -forever -shared \
       -nopw -quiet -bg -o /tmp/x11vnc.log
VNC_PID=""

echo "Starting scrapeMM on port ${PORT}..."
exec uvicorn scrapemm.server.app:app \
    --host "${SCRAPEMM_HOST:-0.0.0.0}" \
    --port "$PORT" \
    --workers 1 \
    --log-level "${SCRAPEMM_UVICORN_LOG_LEVEL:-info}" \
    --timeout-keep-alive 75
