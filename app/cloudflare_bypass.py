"""Cloudflare Turnstile bypass using SeleniumBase UC Mode.

Based on https://github.com/1837620622/cloudflare-bypass-2026
Provides automatic detection and OS-level click handling for Cloudflare challenges.

Usage:
    from app.cloudflare_bypass import CloudflareBypass

    async def handle_cf(url: str, proxy: str = None):
        bypass = CloudflareBypass(proxy=proxy)
        result = await bypass.solve(url)
        if result["success"]:
            return result["cookies"]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("browser_agent.cloudflare_bypass")

CF_INDICATORS = (
    "turnstile",
    "challenges.cloudflare",
    "just a moment",
    "verify you are human",
    "checking your browser",
    "cf-browser-verification",
    "cf-challenge",
)


class CloudflareBypass:
    """SeleniumBase UC Mode bypass for Cloudflare Turnstile challenges."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        headless: bool = False,
        timeout: float = 60.0,
        reconnect_time: float = 5.0,
        incognito: bool = False,
        session_name: str = "agent_bypass",
    ):
        self.proxy = proxy
        self.headless = headless
        self.timeout = timeout
        self.reconnect_time = reconnect_time
        self.incognito = incognito
        self.session_name = session_name

    async def solve(self, url: str) -> Dict[str, Any]:
        """Attempt to bypass Cloudflare on the given URL.

        Returns a dict with: success, cookies, cf_clearance, user_agent, error.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._solve_sync, url)

    def _solve_sync(self, url: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "cookies": {},
            "cf_clearance": None,
            "user_agent": None,
            "error": None,
            "method": "seleniumbase_uc",
        }

        try:
            from seleniumbase import SB
        except ImportError:
            result["error"] = "seleniumbase not installed"
            return result

        deadline = time.monotonic() + max(5.0, self.timeout)

        sb_kwargs: Dict[str, Any] = {
            "uc": True,
            "test": True,
            "locale": "en",
            "proxy": self.proxy,
        }
        if self.headless:
            sb_kwargs["headless"] = True
        if self.incognito:
            sb_kwargs["incognito"] = True
        if self._is_linux() and not os.environ.get("DISPLAY"):
            sb_kwargs["xvfb"] = True

        try:
            with SB(**sb_kwargs) as sb:
                logger.info("Opening %s with UC bypass", url)
                sb.uc_open_with_reconnect(url, reconnect_time=self.reconnect_time)
                time.sleep(2)

                attempt = 0
                while time.monotonic() < deadline:
                    attempt += 1
                    cookies_list = sb.get_cookies()
                    cookies = {c["name"]: c["value"] for c in cookies_list}
                    cf_clearance = cookies.get("cf_clearance")
                    page_source = sb.get_page_source()

                    if cf_clearance and not self._has_cf_challenge(page_source):
                        result["cookies"] = cookies
                        result["cf_clearance"] = cf_clearance
                        result["user_agent"] = sb.execute_script(
                            "return navigator.userAgent"
                        )
                        result["success"] = True
                        logger.info("cf_clearance obtained on attempt %d", attempt)
                        self._save_cookies(url, cookies_list, result["user_agent"])
                        return result

                    if cf_clearance:
                        logger.info("Have cf_clearance, waiting for page to stabilize")
                        result["cookies"] = cookies
                        result["cf_clearance"] = cf_clearance
                        result["user_agent"] = sb.execute_script(
                            "return navigator.userAgent"
                        )
                        time.sleep(2)
                        page_source = sb.get_page_source()
                        if not self._has_cf_challenge(page_source):
                            result["success"] = True
                            self._save_cookies(
                                url, sb.get_cookies(), result["user_agent"]
                            )
                            return result

                    if self._has_cf_challenge(page_source):
                        logger.info("Challenge detected, clicking captcha (attempt %d)", attempt)
                        self._try_click(sb)
                        time.sleep(3)
                    else:
                        result["cookies"] = cookies
                        result["user_agent"] = sb.execute_script(
                            "return navigator.userAgent"
                        )
                        if cookies:
                            logger.info("No challenge detected; site may not use Turnstile")
                            result["error"] = (
                                "No cf_clearance (site may not require Cloudflare)"
                            )
                            return result
                        time.sleep(1.5)

                result["error"] = f"Timeout ({self.timeout}s), no cf_clearance"
                logger.warning(result["error"])

        except Exception as e:
            result["error"] = str(e)
            logger.error("Bypass error: %s", e)

        return result

    def _has_cf_challenge(self, page_source: str) -> bool:
        text = (page_source or "").lower()
        return any(x in text for x in CF_INDICATORS)

    def _try_click(self, sb) -> None:
        try:
            sb.uc_gui_click_captcha()
            return
        except Exception as e:
            logger.debug("uc_gui_click_captcha failed: %s", e)
        try:
            if hasattr(sb, "uc_gui_handle_captcha"):
                sb.uc_gui_handle_captcha()
        except Exception as e:
            logger.debug("uc_gui_handle_captcha failed: %s", e)

    def _save_cookies(
        self, url: str, cookies_list: list, user_agent: Optional[str]
    ) -> None:
        save_dir = Path("output/cookies")
        save_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cookies_dict = {c["name"]: c["value"] for c in cookies_list}
        path = save_dir / f"cookies_bypass_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "url": url,
                    "cookies": cookies_dict,
                    "user_agent": user_agent,
                    "timestamp": ts,
                    "method": "seleniumbase_uc",
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        logger.info("Cookies saved to %s", path)

    @staticmethod
    def _is_linux() -> bool:
        import platform

        return platform.system().lower() == "linux"


async def bypass_url(
    url: str,
    proxy: Optional[str] = None,
    timeout: float = 60.0,
    session_name: str = "agent_bypass",
) -> Dict[str, Any]:
    """Convenience async wrapper to solve a single URL."""
    bypass = CloudflareBypass(
        proxy=proxy,
        timeout=timeout,
        session_name=session_name,
    )
    return await bypass.solve(url)
