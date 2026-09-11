#!/usr/bin/env bash
# Launches Chromium in headed mode on the virtual display with remote
# debugging enabled, so Browser Use can attach over CDP and noVNC can show it.
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
PID_DIR="${PID_DIR:-/tmp}"
CDP_PORT="${CDP_PORT:-9222}"
SCREEN_WIDTH="${SCREEN_WIDTH:-1920}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-1080}"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-$PWD/browser_profile}"
export DISPLAY

mkdir -p "$PID_DIR" "$PROFILE_DIR"

CHROME_BIN="${CHROME_BIN:-}"
if [ -z "$CHROME_BIN" ]; then
  for candidate in google-chrome-stable google-chrome chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CHROME_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi
if [ -z "$CHROME_BIN" ] || [ ! -x "$CHROME_BIN" ]; then
  echo "[browser] no Chrome/Chromium binary found" >&2
  exit 1
fi

if pgrep -f "remote-debugging-port=${CDP_PORT}" >/dev/null 2>&1; then
  echo "[browser] Chromium already running"
  exit 0
fi

echo "[browser] launching ${CHROME_BIN} (CDP :${CDP_PORT})"
"$CHROME_BIN" \
  --user-data-dir="${PROFILE_DIR}" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="${CDP_PORT}" \
  --no-first-run \
  --no-default-browser-check \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-background-networking \
  --disable-sync \
  --disable-translate \
  --disable-features=Translate,MediaRouter,OptimizationHints \
  --password-store=basic \
  --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
  --window-position=0,0 \
  --start-maximized \
  about:blank >"${PID_DIR}/chromium.log" 2>&1 &
echo $! >"${PID_DIR}/chromium.pid"

for _ in $(seq 1 80); do
  if curl -sf "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
    echo "[browser] Chromium ready (CDP :${CDP_PORT})"
    exit 0
  fi
  if ! kill -0 "$(cat "${PID_DIR}/chromium.pid")" 2>/dev/null; then
    echo "[browser] Chromium exited during startup; see ${PID_DIR}/chromium.log" >&2
    exit 1
  fi
  sleep 0.5
done

echo "[browser] Chromium did not expose CDP in time" >&2
exit 1
