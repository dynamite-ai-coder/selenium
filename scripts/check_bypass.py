"""End-to-end check of the invisible_playwright Cloudflare bypass.

Run this inside the container (or locally on a glibc Linux/macOS/Windows host)
to verify the stealth browser and the engine download:

    python scripts/check_bypass.py                     # opens nowsecure.nl
    python scripts/check_bypass.py https://example.com
    python scripts/check_bypass.py --headless false    # watch it on DISPLAY

It prints the same result the agent uses: success, cf_clearance, cookies and
the User-Agent. It does not touch the visible Chromium.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cloudflare_bypass import CloudflareBypass, parse_proxy  # noqa: E402


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


async def _run(url: str, headless: bool, timeout: float) -> int:
    proxy = parse_proxy(_env("BROWSER_PROXY"))
    if proxy:
        print(f"[bypass] using proxy {proxy['server']}")
    bypass = CloudflareBypass(
        proxy=proxy,
        headless=headless,
        timeout=timeout,
        seed=int(_env("CF_BYPASS_SEED", "0")) or None,
        locale=_env("CF_BYPASS_LOCALE", "auto") or "auto",
        timezone=_env("BROWSER_TZ"),
        click_turnstile=_env("CF_BYPASS_CLICK_TURNSTILE", "true").lower() != "false",
    )
    result = await bypass.solve(url)
    summary = {
        "success": result.get("success"),
        "cf_clearance": "yes" if result.get("cf_clearance") else "no",
        "cookie_count": len(result.get("cookies") or []),
        "user_agent": result.get("user_agent"),
        "seed": result.get("seed"),
        "error": result.get("error"),
        "note": result.get("note"),
    }
    print(json.dumps(summary, indent=2))
    return 0 if result.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://nowsecure.nl")
    parser.add_argument(
        "--headless",
        default=_env("CF_BYPASS_HEADLESS", "true"),
        help="true (default) or false to render on DISPLAY",
    )
    parser.add_argument("--timeout", type=float, default=float(_env("CF_BYPASS_TIMEOUT", "90") or 90))
    args = parser.parse_args()
    return asyncio.run(_run(args.url, args.headless.lower() != "false", args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
