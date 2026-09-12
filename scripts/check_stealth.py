"""Check how detectable the running Chromium/Chrome instance is.

The script connects to the CDP endpoint published by the SeleniumBase
launcher (``CDP_URL_FILE``) and evaluates a set of JavaScript probes in the
active page, printing the same properties anti-bot systems commonly look at
(``navigator.webdriver``, plugins, WebGL renderer, timezone, ...).

Run it with the main application environment (it needs ``httpx`` and
``websockets``)::

    python scripts/check_stealth.py
    python scripts/check_stealth.py --url https://bot.sannysoft.com/

For a full third-party report also open one of these pages in the live
noVNC view: https://bot-detector.rebrowser.net/ , https://bot.sannysoft.com/ ,
https://browserscan.net/ , https://abrahamjuliot.github.io/creepjs/ .

Educational use only: this only reports what the browser exposes, it does
not modify or hide anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from websockets.sync.client import connect

JS_PROBES = """
(() => {
  const out = {};
  out.userAgent = navigator.userAgent;
  out.webdriver = navigator.webdriver;
  out.platform = navigator.platform;
  out.languages = (navigator.languages || []).join(', ');
  out.hardwareConcurrency = navigator.hardwareConcurrency;
  out.deviceMemory = navigator.deviceMemory;
  out.plugins = navigator.plugins.length;
  out.mimeTypes = navigator.mimeTypes.length;
  out.chromeObject = typeof window.chrome;
  out.chromeRuntime = !!(window.chrome && window.chrome.runtime);
  out.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.locale = Intl.DateTimeFormat().resolvedOptions().locale;
  out.screen = screen.width + 'x' + screen.height + '@' + screen.colorDepth;
  out.viewport = innerWidth + 'x' + innerHeight;
  out.outerSize = outerWidth + 'x' + outerHeight;
  out.pdfViewer = navigator.pdfViewerEnabled;
  out.cookieEnabled = navigator.cookieEnabled;
  out.connection = navigator.connection ? navigator.connection.effectiveType : null;
  out.userAgentData = navigator.userAgentData
    ? (navigator.userAgentData.brands || []).map(b => b.brand + ' ' + b.version).join(', ')
    : null;
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) {
        out.webglVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
        out.webglRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
      }
    }
  } catch (e) {
    out.webglError = String(e);
  }
  return JSON.stringify(out);
})()
"""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def cdp_base_url() -> str:
    url_file = Path(_env("CDP_URL_FILE", "/tmp/cdp_url"))
    try:
        candidate = url_file.read_text(encoding="utf-8").strip().rstrip("/")
    except OSError:
        candidate = ""
    if candidate.startswith(("http://", "https://")):
        return candidate
    return _env("CDP_URL", "http://127.0.0.1:9222").rstrip("/")


def _page_websocket(base_url: str) -> str:
    response = httpx.get(f"{base_url}/json", timeout=5.0)
    response.raise_for_status()
    for target in response.json():
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    raise SystemExit("[stealth] no page target found on the CDP endpoint")


class CdpConnection:
    def __init__(self, websocket_url: str) -> None:
        self._ws = connect(
            websocket_url,
            max_size=32 * 1024 * 1024,
            open_timeout=10,
            close_timeout=2,
        )
        self._next_id = 0

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self._next_id += 1
        message_id = self._next_id
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "CDP error"))
                return message.get("result", {})
        raise TimeoutError(f"CDP call timed out: {method}")


def _check(label: str, value, ok: bool) -> bool:
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {label:<22} {value}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="optional page to open before probing")
    parser.add_argument("--wait", type=float, default=2.0, help="seconds to wait after navigation")
    args = parser.parse_args()

    base_url = cdp_base_url()
    print(f"[stealth] CDP endpoint: {base_url}")
    try:
        connection = CdpConnection(_page_websocket(base_url))
    except Exception as exc:
        print(f"[stealth] cannot connect to the browser: {exc}", file=sys.stderr)
        return 1

    try:
        if args.url:
            print(f"[stealth] opening {args.url}")
            connection.call("Page.navigate", {"url": args.url})
            time.sleep(max(0.0, args.wait))
        result = connection.call(
            "Runtime.evaluate",
            {"expression": JS_PROBES, "returnByValue": True},
        )
    finally:
        connection.close()

    raw = result.get("result", {}).get("value")
    if not raw:
        print(f"[stealth] could not evaluate probes: {result}", file=sys.stderr)
        return 1
    data = json.loads(raw)

    renderer = str(data.get("webglRenderer") or "")
    software = any(token in renderer.lower() for token in ("swiftshader", "llvmpipe", "software"))

    print("[stealth] browser properties:")
    failures = 0
    failures += not _check("navigator.webdriver", data.get("webdriver"), data.get("webdriver") in (False, None))
    failures += not _check("plugins", data.get("plugins"), (data.get("plugins") or 0) > 0)
    failures += not _check("chrome.runtime", data.get("chromeRuntime"), bool(data.get("chromeRuntime")))
    failures += not _check("languages", data.get("languages"), bool(data.get("languages")))
    failures += not _check("webgl renderer", renderer or data.get("webglError"), bool(renderer) and not software)
    failures += not _check("hardwareConcurrency", data.get("hardwareConcurrency"), (data.get("hardwareConcurrency") or 0) >= 4)
    failures += not _check("timezone", data.get("timezone"), bool(data.get("timezone")))
    failures += not _check(
        "UA vs platform", f"{data.get('platform')} / {data.get('userAgent', '')[:60]}",
        bool(data.get("platform")) and bool(data.get("userAgent")),
    )

    print()
    print(f"[stealth] {failures} check(s) failed" if failures else "[stealth] basic checks passed")
    print(
        "[stealth] note: the server-side CDP leak (Runtime.enable) is triggered by the\n"
        "          automation client, not by the browser. Test it at\n"
        "          https://bot-detector.rebrowser.net/ and compare with/without the agent attached."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
