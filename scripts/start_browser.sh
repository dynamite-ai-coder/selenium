#!/usr/bin/env bash
# Launches Chromium in headed mode through SeleniumBase Pure CDP Mode.
# SeleniumBase starts the browser over the Chrome DevTools Protocol (no
# chromedriver); the real debugging port is written to CDP_URL_FILE so
# Browser Use can attach to the visible browser shown in noVNC.
set -euo pipefail

cd "$(dirname "$0")/.."

DISPLAY="${DISPLAY:-:99}"
PID_DIR="${PID_DIR:-/tmp}"
CDP_URL_FILE="${CDP_URL_FILE:-/tmp/cdp_url}"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-$PWD/browser_profile}"
export DISPLAY CDP_URL_FILE BROWSER_PROFILE_DIR="$PROFILE_DIR"

mkdir -p "$PID_DIR" "$PROFILE_DIR"

# SeleniumBase lives in its own environment in the Docker image because its
# dependency pins conflict with Browser Use. Locally, any interpreter with
# SeleniumBase installed can be selected with SELENIUMBASE_PYTHON.
PYTHON="${SELENIUMBASE_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x /opt/seleniumbase-venv/bin/python ]; then
    PYTHON=/opt/seleniumbase-venv/bin/python
  else
    PYTHON="$(command -v python3 || command -v python)"
  fi
fi
if [ -z "$PYTHON" ]; then
  echo "[browser] no python interpreter found" >&2
  exit 1
fi

if [ -f "${PID_DIR}/chromium.pid" ] && kill -0 "$(cat "${PID_DIR}/chromium.pid")" 2>/dev/null; then
  echo "[browser] SeleniumBase browser already running"
  exit 0
fi

rm -f "$CDP_URL_FILE"

echo "[browser] starting SeleniumBase CDP launcher (${PYTHON})"
"$PYTHON" scripts/start_browser.py >"${PID_DIR}/chromium.log" 2>&1 &
echo $! >"${PID_DIR}/chromium.pid"

for _ in $(seq 1 120); do
  if [ -f "$CDP_URL_FILE" ]; then
    CDP_URL="$(cat "$CDP_URL_FILE")"
    if curl -sf "${CDP_URL}/json/version" >/dev/null 2>&1; then
      echo "[browser] SeleniumBase browser ready at ${CDP_URL}"
      exit 0
    fi
  fi
  if ! kill -0 "$(cat "${PID_DIR}/chromium.pid")" 2>/dev/null; then
    echo "[browser] browser exited during startup; see ${PID_DIR}/chromium.log" >&2
    exit 1
  fi
  sleep 0.5
done

echo "[browser] browser did not expose CDP in time; see ${PID_DIR}/chromium.log" >&2
exit 1
