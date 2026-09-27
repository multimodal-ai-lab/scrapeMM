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

# A restarted container keeps its filesystem, and with it the lock of the Xvfb that ran
# before. Xvfb then refuses to start ("Server is already active for display 99"). This
# script is the container's first process, so no display of ours can be running yet:
# whatever lock is there is stale.
DISPLAY_NUMBER="${DISPLAY#:}"
DISPLAY_NUMBER="${DISPLAY_NUMBER%%.*}"
rm -f "/tmp/.X${DISPLAY_NUMBER}-lock" "/tmp/.X11-unix/X${DISPLAY_NUMBER}"

# A container that is restarted (rather than recreated), e.g. when Docker itself
# restarts, keeps /tmp -- including the lock of the Xvfb that died with it. Xvfb then
# refuses to start ("Server is already active") and the container restart-loops. No
# other X server ever runs in here, so any lock left over is stale.
rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}"

echo "Starting Xvfb on ${DISPLAY} (${SCREEN_SIZE})..."
# Xvfb recompiles its keymap on start and whenever a client (x11vnc) connects, and
# xkbcomp complains each time about keysyms the image's keyboard data lacks. Harmless,
# so those lines are filtered out; anything else Xvfb reports still gets through.
Xvfb "$DISPLAY" -screen 0 "$SCREEN_SIZE" -nolisten tcp \
    2> >(grep --line-buffered -vE '^(The XKEYBOARD keymap compiler|> |Errors from xkbcomp)' >&2) &
XVFB_PID=$!

# Wait for the display to accept connections before anything tries to use it
DISPLAY_READY=false
for _ in $(seq 1 50); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then DISPLAY_READY=true; break; fi
    sleep 0.2
done

# Neither the display nor VNC may keep the server from starting: without them only the
# headed browser and the CAPTCHA panel are unavailable, and the dashboard says so.
# Failing here instead (as `set -e` would) makes the container restart forever
# without ever serving the UI or the API.
if [[ "$DISPLAY_READY" == true ]]; then
    echo "Starting x11vnc on 127.0.0.1:${VNC_PORT}..."
    # -forever: keep serving after a viewer disconnects, so the panel can be reopened
    # -shared: several viewers may watch at once
    # -nopw with -localhost: the socket is reachable only from inside the container; the
    #   web UI's own API key is what actually guards it
    x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -localhost -forever -shared \
           -nopw -quiet -bg -o /tmp/x11vnc.log >/dev/null \
        || echo "WARNING: x11vnc failed to start (see /tmp/x11vnc.log); the CAPTCHA panel is unavailable." >&2
else
    echo "WARNING: The display ${DISPLAY} did not come up; the headed browser and the CAPTCHA panel are unavailable." >&2
fi
VNC_PID=""

echo "Starting scrapeMM on port ${PORT}..."
exec uvicorn scrapemm.server.app:app \
    --host "${SCRAPEMM_HOST:-0.0.0.0}" \
    --port "$PORT" \
    --workers 1 \
    --log-level "${SCRAPEMM_UVICORN_LOG_LEVEL:-info}" \
    --timeout-keep-alive 75
