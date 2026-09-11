"""Browser lifecycle management.

The container starts Chromium in headed mode inside Xvfb/XFCE (see
``scripts/start_browser.sh``). This module connects Browser Use to that
already-running browser over CDP so the agent controls the *visible*
browser and the user can watch every action through noVNC.

If no CDP endpoint is available (for example local development on a
desktop), it falls back to letting Browser Use launch a headed local
browser itself.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from browser_use import BrowserProfile, BrowserSession

from app.config import settings
from app.utils import safe_error_message

logger = logging.getLogger("browser_agent.browser")


class BrowserUnavailableError(RuntimeError):
    """Raised when no browser can be reached or launched."""


def _profile(**overrides) -> BrowserProfile:
    data: dict = dict(
        headless=False,
        keep_alive=True,
        user_data_dir=str(settings.browser_profile_path),
        downloads_path=str(settings.downloads_path),
        accept_downloads=True,
        chromium_sandbox=False,
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

    # -- status ------------------------------------------------------------
    async def is_cdp_reachable(self) -> bool:
        if not settings.cdp_url:
            return False
        url = settings.cdp_url.rstrip("/") + "/json/version"
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                response = await client.get(url)
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
            if await self.is_cdp_reachable():
                logger.info("Connecting to CDP browser at %s", settings.cdp_url)
                session = BrowserSession(browser_profile=_profile(cdp_url=settings.cdp_url))
                await session.start()
                self._session = session
                return session

            # Fallback: launch a headed local browser (local development).
            chrome = settings.resolved_chrome_bin
            if chrome and not settings.browser_headless:
                logger.info("CDP not reachable, launching local headed browser: %s", chrome)
                session = BrowserSession(browser_profile=_profile(is_local=True))
                await session.start()
                self._session = session
                return session

            raise BrowserUnavailableError(
                "Browser is not available. The Chromium/CDP service is not reachable "
                f"at {settings.cdp_url} and no local Chrome binary was found."
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
