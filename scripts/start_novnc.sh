#!/usr/bin/env bash
# Starts x11vnc (localhost only) and websockify/noVNC.
# VNC is never exposed publicly: FastAPI proxies /websockify after auth.
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
PID_DIR="${PID_DIR:-/tmp}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"
export DISPLAY

mkdir -p "$PID_DIR"

if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  echo "[novnc] display ${DISPLAY} is not available" >&2
  exit 1
fi

if ! pgrep -f "x11vnc" >/dev/null 2>&1; then
  echo "[novnc] starting x11vnc on 127.0.0.1:${VNC_PORT}"
  x11vnc -display "$DISPLAY" \
    -rfbport "$VNC_PORT" \
    -localhost \
    -nopw \
    -forever \
    -shared \
    -repeat \
    -quiet \
    -o "${PID_DIR}/x11vnc.log" >/dev/null 2>&1 &
  echo $! >"${PID_DIR}/x11vnc.pid"
  sleep 1
fi

if ! pgrep -f "websockify" >/dev/null 2>&1; then
  if [ -d "$NOVNC_DIR" ]; then
    echo "[novnc] starting websockify (noVNC from ${NOVNC_DIR})"
    websockify --web "$NOVNC_DIR" "127.0.0.1:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
      >"${PID_DIR}/websockify.log" 2>&1 &
  else
    echo "[novnc] noVNC directory ${NOVNC_DIR} not found; running websockify only" >&2
    websockify "127.0.0.1:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
      >"${PID_DIR}/websockify.log" 2>&1 &
  fi
  echo $! >"${PID_DIR}/websockify.pid"
fi

echo "[novnc] ready (ws 127.0.0.1:${NOVNC_PORT} -> vnc 127.0.0.1:${VNC_PORT})"
