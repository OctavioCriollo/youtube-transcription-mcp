#!/bin/sh
# authgate container entrypoint.
#
# Brings up the display stack the remote-login flow needs, then runs the app:
#
#   Xvfb (:99)  <- headed Chromium renders here (launched on demand by the app)
#     |
#   x11vnc      <- mirrors :99 as VNC on localhost:5900 (no host exposure)
#     |
#   websockify  <- serves noVNC web client + VNC-over-WebSocket on :6080
#                  (Traefik reaches this; a ForwardAuth check gates every hit)
#     |
#   authgate app (PID 1) <- internal API for the MCP + /auth for Traefik
#
# x11vnc has no VNC password on purpose: the ONLY path to :6080 is through
# Traefik, which enforces the capability token via ForwardAuth. Binding x11vnc
# to localhost keeps the raw VNC port off every network.
set -eu

DISPLAY_NUM="${AUTHGATE_DISPLAY:-:99}"
GEOMETRY="1280x800x24"
export DISPLAY="$DISPLAY_NUM"

# 1. Virtual display. Wait for its socket before anything attaches to it.
Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp &
SOCK="/tmp/.X11-unix/X${DISPLAY_NUM#:}"
i=0
while [ ! -S "$SOCK" ] && [ "$i" -lt 100 ]; do
    i=$((i + 1))
    sleep 0.1
done
if [ ! -S "$SOCK" ]; then
    echo "[supervisor] FATAL: Xvfb display $DISPLAY_NUM never came up" >&2
    exit 1
fi
echo "[supervisor] Xvfb ready on $DISPLAY_NUM"

# 2. VNC mirror, localhost-only, background.
x11vnc -display "$DISPLAY_NUM" -forever -shared -nopw -localhost -quiet -bg -rfbport 5900
echo "[supervisor] x11vnc mirroring $DISPLAY_NUM on localhost:5900"

# 3. noVNC web client + WebSocket bridge, reachable by Traefik on :6080.
websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 &
echo "[supervisor] websockify serving noVNC on :6080"

# 4. The app owns PID 1 so Docker signals reach it.
exec python -m authgate.app
