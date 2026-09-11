"""Launch the visible Chromium with SeleniumBase in Pure CDP Mode.

SeleniumBase starts Chrome and talks to it exclusively over the Chrome
DevTools Protocol (no chromedriver). The real remote-debugging port is
written to ``CDP_URL_FILE`` so the application (Browser Use) can attach to
the very same browser and the user can watch it through noVNC.

This script is intentionally standalone: it only imports the standard
library and SeleniumBase, so it can run with an isolated Python
environment (SeleniumBase's dependency pins conflict with Browser Use).
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
from pathlib import Path

CHROME_CANDIDATES = (
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/local/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/local/bin/chromium",
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _find_chrome() -> str:
    configured = _env("CHROME_BIN")
    if configured:
        return configured
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not found:
        raise SystemExit("[browser] no Chrome/Chromium binary found")
    return found


def _write_url(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(url + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    try:
        from seleniumbase import sb_cdp
    except Exception as exc:  # pragma: no cover - depends on the interpreter
        print(f"[browser] SeleniumBase is not available: {exc}", file=sys.stderr)
        return 1

    display = _env("DISPLAY", ":99")
    os.environ["DISPLAY"] = display
    width = _env("SCREEN_WIDTH", "1920")
    height = _env("SCREEN_HEIGHT", "1080")
    profile_dir = Path(_env("BROWSER_PROFILE_DIR", "browser_profile")).resolve()
    downloads_dir = Path(_env("DOWNLOADS_DIR", "downloads")).resolve()
    url_file = Path(_env("CDP_URL_FILE", "/tmp/cdp_url")).resolve()
    chrome_bin = _find_chrome()

    profile_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    browser_args = [
        "--remote-allow-origins=*",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-sync",
        "--start-maximized",
        f"--window-size={width},{height}",
    ]

    print(f"[browser] launching {chrome_bin} with SeleniumBase Pure CDP Mode (DISPLAY={display})")
    try:
        sb = sb_cdp.Chrome(
            headless=False,
            headed=True,
            sandbox=False,
            user_data_dir=str(profile_dir),
            browser_executable_path=chrome_bin,
            downloads_path=str(downloads_dir),
            browser_args=browser_args,
        )
    except Exception as exc:
        print(f"[browser] SeleniumBase failed to launch Chromium: {exc}", file=sys.stderr)
        return 1

    try:
        port = sb.get_rd_port()
        url = f"http://127.0.0.1:{port}"
        _write_url(url_file, url)
        print(f"[browser] SeleniumBase CDP browser ready at {url} (profile={profile_dir})")
    except Exception as exc:
        print(f"[browser] could not determine the CDP port: {exc}", file=sys.stderr)
        try:
            sb.quit()
        except Exception:
            pass
        return 1

    stop = threading.Event()

    def _handle_signal(signum, _frame):  # pragma: no cover - signal path
        print(f"[browser] received signal {signum}, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    stop.wait()

    try:
        sb.quit()
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[browser] error while closing Chromium: {exc}", file=sys.stderr)
    try:
        url_file.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
