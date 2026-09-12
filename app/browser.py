"""Browser lifecycle management.

The container starts Chromium in headed mode inside Xvfb/XFCE via
SeleniumBase Pure CDP Mode (see ``scripts/start_browser.sh`` and
``scripts/start_browser.py``). This module connects Browser Use to that
already-running browser over CDP so the agent controls the *visible*
browser and the user can watch every action through noVNC.

SeleniumBase chooses the remote-debugging port dynamically and publishes
the real endpoint in ``CDP_URL_FILE``; ``settings.cdp_url`` is only the
fallback used when that file is absent (for example local development).

If no CDP endpoint is available (for example local development on a
desktop), it falls back to letting Browser Use launch a headed local
browser itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from browser_use import BrowserProfile, BrowserSession
from browser_use.browser.profile import ProxySettings

from app.config import settings
from app.cloudflare_bypass import CloudflareBypass, parse_proxy, playwright_cookies_to_cdp
from app.utils import safe_error_message

logger = logging.getLogger("browser_agent.browser")


class BrowserUnavailableError(RuntimeError):
    """Raised when no browser can be reached or launched."""


def runtime_cdp_url() -> str:
    """Return the CDP endpoint of the SeleniumBase-launched browser.

    The launcher writes the real URL (dynamic port) to ``CDP_URL_FILE``.
    When the file is missing or invalid, fall back to ``settings.cdp_url``.
    """
    path = Path(settings.cdp_url_file)
    try:
        candidate = path.read_text(encoding="utf-8").strip().rstrip("/")
    except OSError:
        candidate = ""
    if candidate.startswith(("http://", "https://")):
        return candidate
    return settings.cdp_url.rstrip("/")


def _proxy_settings() -> ProxySettings | None:
    """Build Browser Use proxy settings from ``BROWSER_PROXY``.

    Accepts "host:port", "user:pass@host:port" or a full URL with a scheme.
    Only used for the local-launch fallback: when attaching to the
    SeleniumBase browser over CDP, the proxy is already set at launch time.
    """
    raw = settings.browser_proxy.strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    parts = urlsplit(candidate)
    if not parts.hostname or not parts.port:
        logger.warning("Ignoring invalid BROWSER_PROXY value")
        return None
    server = f"{parts.scheme}://{parts.hostname}:{parts.port}"
    username = parts.username or None
    password = parts.password or None
    return ProxySettings(server=server, username=username, password=password)


def _profile(**overrides) -> BrowserProfile:
    data: dict = dict(
        headless=False,
        keep_alive=True,
        user_data_dir=str(settings.browser_profile_path),
        downloads_path=str(settings.downloads_path),
        accept_downloads=True,
        chromium_sandbox=False,
        proxy=_proxy_settings(),
        window_size={"width": settings.screen_width, "height": settings.screen_height},
        minimum_wait_page_load_time=0.5,
        wait_for_network_idle_page_load_time=1.0,
        wait_between_actions=0.5,
        executable_path=settings.resolved_chrome_bin,
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--password-store=basic",
            "--remote-allow-origins=*",
            "--start-maximized",
        ],
    )
    data.update(overrides)
    return BrowserProfile(**data)


class BrowserManager:
    """Owns the single shared :class:`BrowserSession` used by the agent."""

    def __init__(self) -> None:
        self._session: BrowserSession | None = None
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None
        self._stop_monitor = asyncio.Event()
        self._last_connected: bool | None = None
        # User-Agent that minted the current ``cf_clearance`` cookie (if any).
        # Re-applied after a reconnect so the cookie keeps validating.
        self._cf_user_agent: str | None = None

    # -- status ------------------------------------------------------------
    @property
    def cdp_url(self) -> str:
        """Current CDP endpoint (SeleniumBase publishes a dynamic port)."""
        return runtime_cdp_url()

    async def is_cdp_reachable(self) -> bool:
        url = self.cdp_url
        if not url:
            return False
        endpoint = url.rstrip("/") + "/json/version"
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                response = await client.get(endpoint)
                return response.status_code == 200
        except Exception:
            return False

    @property
    def session(self) -> BrowserSession | None:
        return self._session

    async def status(self) -> str:
        if self._session is not None and self._session.is_cdp_connected:
            return "connected"
        if await self.is_cdp_reachable():
            return "connected"
        return "disconnected"

    # -- lifecycle ---------------------------------------------------------
    async def ensure_connected(self) -> BrowserSession:
        """Return a connected browser session, creating/reconnecting if needed."""
        async with self._lock:
            if self._session is not None and self._session.is_cdp_connected:
                return self._session

            # Drop an existing but disconnected session before reconnecting.
            if self._session is not None:
                await self._silent_close(self._session)
                self._session = None

            # Preferred: connect to the Chromium instance started in the container.
            cdp_url = self.cdp_url
            if cdp_url and await self.is_cdp_reachable():
                logger.info("Connecting to SeleniumBase CDP browser at %s", cdp_url)
                session = BrowserSession(browser_profile=_profile(cdp_url=cdp_url))
                await session.start()
                self._session = session
                await self._restore_bypass_state(session)
                return session

            # Fallback: launch a headed local browser (local development).
            chrome = settings.resolved_chrome_bin
            if chrome and not settings.browser_headless:
                logger.info("CDP not reachable, launching local headed browser: %s", chrome)
                session = BrowserSession(browser_profile=_profile(is_local=True))
                await session.start()
                self._session = session
                await self._restore_bypass_state(session)
                return session

            raise BrowserUnavailableError(
                "Browser is not available. The SeleniumBase Chromium/CDP service is not "
                f"reachable at {self.cdp_url or settings.cdp_url} and no local Chrome binary was found."
            )

    async def restart(self) -> BrowserSession:
        """Drop the current session and connect again."""
        async with self._lock:
            if self._session is not None:
                await self._silent_close(self._session)
                self._session = None
        return await self.ensure_connected()

    async def shutdown(self) -> None:
        self._stop_monitor.set()
        if self._monitor and not self._monitor.done():
            self._monitor.cancel()
            try:
                await self._monitor
            except (asyncio.CancelledError, Exception):
                pass
        async with self._lock:
            if self._session is not None:
                try:
                    await self._session.kill()
                except Exception as exc:  # pragma: no cover
                    logger.debug("Error while killing browser session: %s", safe_error_message(exc))
                self._session = None

    @staticmethod
    async def _silent_close(session: BrowserSession) -> None:
        try:
            await session.kill()
        except Exception as exc:  # pragma: no cover
            logger.debug("Error while closing browser session: %s", safe_error_message(exc))

    # -- cloudflare bypass --------------------------------------------------
    async def bypass_cloudflare(self, url: str) -> dict:
        """Solve a Cloudflare interstitial in stealth Firefox and transplant it.

        Runs invisible_playwright (patched Firefox) against ``url``, then
        injects the resulting cookies and the matching User-Agent into the
        agent's visible Chromium over CDP and reloads the page.

        Returns the bypass result dict (``success``, ``cookies``,
        ``cf_clearance``, ``user_agent``, ``applied``, ...).
        """
        if not settings.cf_bypass_enabled:
            return {"success": False, "error": "CF bypass disabled"}

        profile_path = settings.cf_bypass_profile_path
        bypass = CloudflareBypass(
            proxy=parse_proxy(settings.browser_proxy),
            headless=settings.cf_bypass_headless,
            timeout=settings.cf_bypass_timeout,
            seed=settings.cf_bypass_seed or None,
            locale=settings.cf_bypass_locale,
            timezone=settings.browser_tz,
            profile_dir=str(profile_path) if profile_path else None,
            click_turnstile=settings.cf_bypass_click_turnstile,
        )
        result = await bypass.solve(url)
        if result.get("success"):
            try:
                await self.apply_bypass_state(result)
                result["applied"] = not result.get("apply_error")
            except Exception as exc:
                result["applied"] = False
                result["apply_error"] = safe_error_message(exc)
                logger.warning("Could not apply Cloudflare state: %s", result["apply_error"])
        return result

    async def apply_bypass_state(self, result: dict) -> None:
        """Inject bypass cookies + User-Agent (+ Turnstile token) and continue.

        Cloudflare binds ``cf_clearance`` to the User-Agent and egress IP, so
        the visible Chromium must present the same User-Agent the stealth
        browser used. The override is remembered and re-applied automatically
        if the CDP session is reconnected later.

        For an embedded Turnstile widget the solved response token is copied
        into the page's ``cf-turnstile-response`` field instead of reloading -
        a reload would throw away the token and the form state. For an
        interstitial the page is reloaded so it is fetched again with the
        clearance cookies.
        """
        session = self._session or await self.ensure_connected()
        user_agent = result.get("user_agent")
        cookies = playwright_cookies_to_cdp(result.get("cookies"))
        token = result.get("turnstile_token")
        cdp_session = await session.get_or_create_cdp_session()
        if user_agent:
            await cdp_session.cdp_client.send.Emulation.setUserAgentOverride(
                params={"userAgent": user_agent},
                session_id=cdp_session.session_id,
            )
            self._cf_user_agent = user_agent
        if cookies:
            await cdp_session.cdp_client.send.Storage.setCookies(
                params={"cookies": cookies},
                session_id=cdp_session.session_id,
            )
        injected = False
        if token:
            injection = await self._inject_turnstile_token(cdp_session, token)
            injected = bool(injection.get("injected"))
            result["turnstile_submitted"] = bool(injection.get("submitted"))
            if not injected:
                result["apply_error"] = (
                    "No cf-turnstile-response field found on the page; retry the login"
                )
        if not injected:
            await cdp_session.cdp_client.send.Page.reload(
                params={},
                session_id=cdp_session.session_id,
            )
        logger.info(
            "Cloudflare state applied to the visible browser "
            "(%d cookies, ua=%s, turnstile_token=%s, resubmitted=%s)",
            len(cookies),
            "yes" if user_agent else "unchanged",
            "injected" if injected else "none",
            result.get("turnstile_submitted", False),
        )

    @staticmethod
    async def _inject_turnstile_token(cdp_session: Any, token: str) -> dict:
        """Set the Turnstile response field and retry the form submission.

        Uses the native value setter plus input/change events so frameworks
        (React, Vue, ...) pick the value up as if the widget had solved, then
        clicks the page's login/submit control again so the user gets the
        site's verdict without waiting for another agent step. Returns
        ``{"injected": bool, "submitted": bool}``.
        """
        expression = """
        (() => {
          const value = %s;
          const nodes = document.querySelectorAll(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
            + 'input[name$="turnstile-response"]'
          );
          let updated = 0;
          for (const node of nodes) {
            try {
              const proto = node.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
              if (setter) setter.call(node, value); else node.value = value;
              node.dispatchEvent(new Event('input', { bubbles: true }));
              node.dispatchEvent(new Event('change', { bubbles: true }));
              updated += 1;
            } catch (e) {}
          }
          let submitted = false;
          if (updated > 0) {
            const controls = [...document.querySelectorAll('button, input[type=submit]')];
            const control = controls.find((el) =>
              /log.?in|sign.?in|continue|submit|verify/i.test((el.innerText || el.value || '').trim())
            ) || controls.find((el) => el.type === 'submit');
            if (control) {
              try { control.click(); submitted = true; } catch (e) {}
            }
          }
          return { injected: updated > 0, submitted };
        })()
        """ % json.dumps(token)
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        value = result.get("result", {}).get("value") or {}
        return {"injected": bool(value.get("injected")), "submitted": bool(value.get("submitted"))}

    async def _restore_bypass_state(self, session: BrowserSession) -> None:
        """Re-apply the remembered bypass User-Agent after a reconnect."""
        if not self._cf_user_agent:
            return
        with contextlib.suppress(Exception):
            cdp_session = await session.get_or_create_cdp_session()
            await cdp_session.cdp_client.send.Emulation.setUserAgentOverride(
                params={"userAgent": self._cf_user_agent},
                session_id=cdp_session.session_id,
            )
            logger.info("Restored Cloudflare User-Agent on the reconnected browser")

    # -- monitor -----------------------------------------------------------
    def start_monitor(self, interval: float = 8.0) -> None:
        if self._monitor and not self._monitor.done():
            return
        self._stop_monitor.clear()
        self._monitor = asyncio.create_task(self._monitor_loop(interval))

    async def _monitor_loop(self, interval: float) -> None:
        from app.websocket import emit

        while not self._stop_monitor.is_set():
            try:
                connected = await self.is_cdp_reachable()
                if connected != self._last_connected:
                    self._last_connected = connected
                    if connected:
                        await emit("browser_connected", "Browser connected.")
                        logger.info("Browser connected (CDP reachable)")
                    else:
                        await emit("browser_disconnected", "Browser disconnected.")
                        logger.warning("Browser disconnected (CDP unreachable)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.debug("Browser monitor error: %s", safe_error_message(exc))
            try:
                await asyncio.wait_for(self._stop_monitor.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


browser_manager = BrowserManager()
