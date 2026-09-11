#!/usr/bin/env bash
# Starts the virtual X display (Xvfb) and the XFCE desktop on $DISPLAY.
# Safe to run repeatedly: it skips services that are already running.
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
WIDTH="${SCREEN_WIDTH:-1920}"
HEIGHT="${SCREEN_HEIGHT:-1080}"
PID_DIR="${PID_DIR:-/tmp}"
export DISPLAY

mkdir -p "$PID_DIR"

if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  echo "[desktop] starting Xvfb on ${DISPLAY} (${WIDTH}x${HEIGHT}x24)"
  Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac -nolisten tcp \
    +extension GLX +render -noreset >"${PID_DIR}/xvfb.log" 2>&1 &
  echo $! >"${PID_DIR}/xvfb.pid"
fi

ready=0
for _ in $(seq 1 60); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.25
done
if [ "$ready" -ne 1 ]; then
  echo "[desktop] Xvfb did not start on ${DISPLAY}" >&2
  exit 1
fi

if ! pgrep -f "xfce4-session" >/dev/null 2>&1; then
  echo "[desktop] starting XFCE"
  if command -v dbus-launch >/dev/null 2>&1; then
    eval "$(dbus-launch --sh-syntax)"
    export DBUS_SESSION_BUS_ADDRESS
  fi
  setsid startxfce4 >"${PID_DIR}/xfce.log" 2>&1 &
  echo $! >"${PID_DIR}/xfce.pid"
  sleep 2
fi

echo "[desktop] ready on ${DISPLAY}"
