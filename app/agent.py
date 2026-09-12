"""Browser agent runner.

Runs Browser Use tasks asynchronously (never blocking the FastAPI event
loop), emits safe status events over the WebSocket and supports clean
cancellation. Only one task may run against the shared browser at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from browser_use import Agent

from app.browser import BrowserUnavailableError, browser_manager
from app.cloudflare_bypass import detect_challenge
from app.config import settings
from app.llm import LLMConfigError, get_llm
from app.utils import register_secret, safe_error_message, truncate
from app.websocket import emit

logger = logging.getLogger("browser_agent.agent")

ACTION_LABELS: dict[str, str] = {
    "navigate": "Opening website...",
    "search": "Searching...",
    "click": "Clicking...",
    "input": "Typing...",
    "send_keys": "Pressing keys...",
    "scroll": "Scrolling...",
    "wait": "Waiting for page...",
    "extract": "Reading page content...",
    "find_text": "Searching on the page...",
    "search_page": "Searching page content...",
    "find_elements": "Inspecting page elements...",
    "screenshot": "Taking a screenshot...",
    "select_dropdown": "Selecting an option...",
    "dropdown_options": "Reading available options...",
    "go_back": "Going back...",
    "switch": "Switching tab...",
    "close": "Closing tab...",
    "done": "Finishing task...",
    "evaluate": "Running page script...",
    "upload_file": "Uploading file...",
    "save_as_pdf": "Saving page as PDF...",
    "read_file": "Reading file...",
    "write_file": "Writing file...",
    "replace_file": "Updating file...",
}


class TaskBusyError(RuntimeError):
    """Raised when a task is requested while another one is running."""


def _safe_url(url: str | None) -> str:
    """Return only scheme://host of a URL (never tokens/query strings)."""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(str(url))
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:
        pass
    return ""


def _action_labels(agent_output: Any) -> list[str]:
    labels: list[str] = []
    try:
        actions = getattr(agent_output, "action", None) or []
        for action in actions:
            data = action.model_dump(exclude_none=True) if hasattr(action, "model_dump") else {}
            for name, payload in data.items():
                label = ACTION_LABELS.get(name, f"Performing {name.replace('_', ' ')}...")
                if name == "navigate" and isinstance(payload, dict):
                    url = _safe_url(payload.get("url"))
                    if url:
                        label = f"Opening {url}..."
                elif name == "go_back":
                    label = "Going back..."
                labels.append(label)
    except Exception:  # pragma: no cover - never break the agent step
        pass
    return labels


class AgentRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._agent: Agent | None = None
        self._stop_requested = False
        self._task_id: str | None = None
        self._started_at: float = 0.0
        # host -> last bypass attempt (monotonic-ish epoch), avoids loops.
        self._bypass_attempts: dict[str, float] = {}
        # Verdict from the site when it rejected the credentials outright.
        self._last_login_error: str | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def task_id(self) -> str | None:
        return self._task_id

    # -- public API --------------------------------------------------------
    async def start(self, task_text: str, files: list[str] | None = None) -> str:
        if self.busy:
            raise TaskBusyError("Agent is currently busy.")
        register_secret(None)
        task_id = uuid.uuid4().hex[:12]
        self._task_id = task_id
        self._stop_requested = False
        self._last_login_error = None
        self._started_at = time.time()
        self._task = asyncio.create_task(
            self._run(task_id, task_text, files or []), name=f"agent-task-{task_id}"
        )
        logger.info("Task %s started", task_id)
        return task_id

    async def stop(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._stop_requested = True
        agent = self._agent
        if agent is not None:
            with contextlib.suppress(Exception):
                agent.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Graceful stop timed out; cancelling task")
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    # -- internals ---------------------------------------------------------
    async def _run(self, task_id: str, task_text: str, files: list[str]) -> None:
        await emit("task_started", "Task started.", task_id=task_id, task=truncate(task_text, 500))
        try:
            llm = get_llm()
            browser_session = await browser_manager.ensure_connected()
            await emit("browser_connected", "Browser connected.")

            self._agent = Agent(
                task=task_text,
                llm=llm,
                browser_session=browser_session,
                register_new_step_callback=self._on_step,
                register_done_callback=self._on_done,
                register_should_stop_callback=self._should_stop,
                available_file_paths=files or None,
                use_vision=settings.agent_use_vision,
                use_judge=settings.agent_use_judge,
                enable_planning=settings.agent_enable_planning,
                enable_signal_handler=False,
                max_actions_per_step=3,
                step_timeout=180,
                directly_open_url=True,
            )

            await emit("agent_thinking", "Understanding the task...", task_id=task_id)
            history = await asyncio.wait_for(
                self._agent.run(max_steps=settings.agent_max_steps),
                timeout=settings.agent_task_timeout,
            )
            await self._finish(task_id, history)

        except LLMConfigError as exc:
            await emit("error", safe_error_message(exc))
            await emit("task_failed", "LLM is not configured.", task_id=task_id)
        except BrowserUnavailableError as exc:
            await emit("browser_disconnected", safe_error_message(exc))
            await emit("task_failed", "Browser is unavailable.", task_id=task_id)
        except asyncio.CancelledError:
            self._stop_requested = True
            await emit("task_stopped", "Task stopped.", task_id=task_id)
            logger.info("Task %s cancelled", task_id)
        except asyncio.TimeoutError:
            if self._agent is not None:
                with contextlib.suppress(Exception):
                    self._agent.stop()
            await emit("task_failed", "Task timed out.", task_id=task_id)
            logger.warning("Task %s timed out", task_id)
        except Exception as exc:
            message = safe_error_message(exc)
            logger.error("Task %s failed: %s", task_id, message, exc_info=True)
            await emit("error", message)
            await emit("task_failed", message, task_id=task_id)
        finally:
            if self._agent is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._agent.close(), timeout=20)
            self._agent = None
            self._task = None
            self._task_id = None
            self._stop_requested = False
            logger.info("Task %s finished", task_id)

    async def _finish(self, task_id: str, history: Any) -> None:
        steps = 0
        result = ""
        success: bool | None = False
        try:
            steps = history.number_of_steps()
        except Exception:
            pass
        try:
            result = history.final_result() or ""
        except Exception:
            pass
        try:
            success = history.is_successful()
        except Exception:
            success = None

        await self._emit_new_downloads(self._started_at)

        if success:
            await emit("task_completed", result or "Task completed.", task_id=task_id, steps=steps)
            logger.info("Task %s completed in %d steps", task_id, steps)
        else:
            message = result or self._last_login_error or "The agent could not complete the task."
            await emit("task_failed", message, task_id=task_id, steps=steps)
            logger.warning("Task %s did not complete successfully", task_id)

    async def _on_step(self, browser_state_summary: Any, agent_output: Any, step_number: int) -> None:
        labels = _action_labels(agent_output)
        url = _safe_url(getattr(browser_state_summary, "url", ""))
        title = truncate(getattr(browser_state_summary, "title", "") or "", 80)
        for label in labels or [f"Step {step_number}"]:
            await emit("agent_action", label, step=step_number, url=url, title=title)
        with contextlib.suppress(Exception):
            await self._emit_new_downloads(self._started_at)

        # Auto-solve Cloudflare interstitials/Turnstile with invisible_playwright.
        if settings.cf_bypass_enabled:
            with contextlib.suppress(Exception):
                await self._maybe_bypass_cloudflare(
                    browser_state_summary,
                    agent_output,
                    step_number,
                    url,
                    title,
                )

    async def _maybe_bypass_cloudflare(
        self,
        browser_state_summary: Any,
        agent_output: Any,
        step_number: int,
        display_url: str,
        title: str,
    ) -> None:
        """Detect Cloudflare, solve it in stealth Firefox, transplant the result."""
        session = browser_manager.session
        if session is None:
            return
        kind = await detect_challenge(session)
        target_url = getattr(browser_state_summary, "url", "") or display_url
        host = urlsplit(target_url).netloc.lower()
        if kind is None:
            logger.debug("Cloudflare probe on %s: no challenge", target_url)
            return
        logger.info("Cloudflare probe on %s: %s", target_url, kind)

        now = time.time()
        last = self._bypass_attempts.get(host, 0.0)
        if now - last < settings.cf_bypass_cooldown:
            return
        self._bypass_attempts[host] = now

        login = None
        if kind == "turnstile":
            login = await browser_manager.read_login_fields()
        if kind == "turnstile" and login:
            message = "Turnstile challenge detected. Logging in through a stealth browser..."
        elif kind == "turnstile":
            message = "Turnstile challenge detected. Solving in a stealth browser..."
        else:
            message = "Cloudflare challenge detected. Solving in a stealth browser..."
        await emit("agent_action", message, step=step_number, url=display_url, title=title)

        try:
            result = await asyncio.wait_for(
                browser_manager.bypass_cloudflare(target_url, login=login),
                timeout=settings.cf_bypass_timeout + 60,
            )
        except asyncio.TimeoutError:
            result = {
                "success": False,
                "error": f"bypass timed out after {settings.cf_bypass_timeout + 60:.0f}s",
            }
        if result.get("success") and result.get("applied"):
            logger.info("Cloudflare bypass applied for %s (mode=%s)", host, result.get("mode"))
            if result.get("mode") == "turnstile_token":
                if result.get("turnstile_submitted"):
                    message = "Turnstile solved and login resubmitted. Watching the response..."
                else:
                    message = "Turnstile solved and injected. Retry the action..."
            elif result.get("mode") == "login":
                message = "Logged in through the stealth browser. Session transplanted."
            else:
                message = "Cloudflare bypassed. Continuing..."
            await emit("agent_action", message, step=step_number, url=display_url, title=title)
        elif result.get("success"):
            logger.warning("Cloudflare bypass not applied for %s: %s", host, result.get("apply_error"))
            await emit(
                "agent_action",
                "Challenge solved, but session transfer failed.",
                step=step_number,
                url=display_url,
                title=title,
            )
        else:
            if result.get("mode") == "login_error":
                # The site answered with its own verdict; retrying the same
                # credentials will not change it, so stop the task now.
                self._bypass_attempts[host] = time.time() + 86_400
                message = result.get("site_error") or result.get("error") or "login rejected"
                self._last_login_error = f"Login failed: {message}"
                self._stop_requested = True
                if self._agent is not None:
                    with contextlib.suppress(Exception):
                        self._agent.stop()
                logger.warning("Login rejected for %s: %s", host, message)
                await emit(
                    "agent_action",
                    self._last_login_error,
                    step=step_number,
                    url=display_url,
                    title=title,
                )
                return
            message = result.get("error") or "unknown error"
            logger.warning("Cloudflare bypass failed for %s: %s", host, message)
            await emit(
                "agent_action",
                f"Cloudflare bypass failed: {message}",
                step=step_number,
                url=display_url,
                title=title,
            )

    async def _on_done(self, history: Any) -> None:
        logger.debug("Agent signalled completion")

    async def _should_stop(self) -> bool:
        return self._stop_requested

    async def _emit_new_downloads(self, since: float) -> None:
        seen: set[str] = getattr(self, "_seen_downloads", set())  # type: ignore[attr-defined]
        downloads_dir = settings.downloads_path
        if not downloads_dir.exists():
            return
        try:
            entries = sorted(downloads_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in entries:
            try:
                if not path.is_file() or path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            if path.name in seen:
                continue
            seen.add(path.name)
            await emit(
                "file",
                f"File available: {path.name}",
                name=path.name,
                url=f"/api/download/{path.name}",
                size=path.stat().st_size,
            )
        self._seen_downloads = seen  # type: ignore[attr-defined]


runner = AgentRunner()
