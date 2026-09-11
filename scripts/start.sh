#!/usr/bin/env bash
# Container entrypoint: boots the graphical stack and then uvicorn (the main
# process Render expects). Cleanly stops every child on SIGTERM/SIGINT.
set -euo pipefail

cd "$(dirname "$0")/.."

export DISPLAY="${DISPLAY:-:99}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-10000}"
export PYTHONUNBUFFERED=1
PID_DIR="${PID_DIR:-/tmp}"
export PID_DIR

mkdir -p "$PID_DIR" browser_profile uploads downloads logs

cleanup() {
  trap - TERM INT EXIT
  echo "[start] shutting down"
  if [ -f "${PID_DIR}/uvicorn.pid" ]; then
    kill "$(cat "${PID_DIR}/uvicorn.pid")" 2>/dev/null || true
  fi
  for name in websockify x11vnc chromium xfce xvfb; do
    if [ -f "${PID_DIR}/${name}.pid" ]; then
      kill "$(cat "${PID_DIR}/${name}.pid")" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup TERM INT EXIT

bash scripts/start_desktop.sh
bash scripts/start_novnc.sh

if ! bash scripts/start_browser.sh; then
  echo "[start] warning: browser did not start; the app will report degraded status" >&2
fi

echo "[start] starting uvicorn on ${HOST}:${PORT}"
python -m uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --forwarded-allow-ips='*' \
  --no-access-log &
echo $! >"${PID_DIR}/uvicorn.pid"

wait "$(cat "${PID_DIR}/uvicorn.pid")"
