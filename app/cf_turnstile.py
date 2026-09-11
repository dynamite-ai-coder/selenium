"""Cloudflare Turnstile solver using CDP + OS-level clicks.

Connects to the existing Chrome via CDP, detects the Turnstile checkbox,
gets its screen position, and clicks using pyautogui (OS-level, not CDP
Input events - harder for Cloudflare to detect).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Optional

import httpx
import websockets

logger = logging.getLogger("browser_agent.cf_turnstile")

TURNSTILE_INDICATORS = (
    "challenges.cloudflare.com",
    "turnstile",
    "cf-turnstile",
    "Just a moment",
    "Verify you are human",
)


async def _get_page_targets(cdp_url: str) -> list[dict]:
    """Get list of browser targets from CDP."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{cdp_url}/json")
        return resp.json()


async def _find_turnstile(cdp_url: str) -> Optional[dict]:
    """Find Turnstile iframe position via CDP Runtime.evaluate."""
    targets = await _get_page_targets(cdp_url)
    page_target = None
    for t in targets:
        if t.get("type") == "page":
            page_target = t
            break
    if not page_target:
        return None

    ws_url = page_target["webSocketDebuggerUrl"]
    if not ws_url:
        return None

    try:
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            # Find Turnstile iframe and get its position
            cmd = {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": """
                        (() => {
                            // Check for Turnstile iframes
                            const iframes = document.querySelectorAll('iframe');
                            for (const iframe of iframes) {
                                const src = iframe.src || '';
                                if (src.includes('challenges.cloudflare') || src.includes('turnstile')) {
                                    const rect = iframe.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {
                                        return JSON.stringify({
                                            found: true,
                                            x: rect.x + rect.width / 2,
                                            y: rect.y + rect.height / 2,
                                            width: rect.width,
                                            height: rect.height,
                                            src: src.substring(0, 100)
                                        });
                                    }
                                }
                            }
                            // Check for Turnstile div
                            const divs = document.querySelectorAll('div.cf-turnstile, [data-sitekey]');
                            for (const div of divs) {
                                const rect = div.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    return JSON.stringify({
                                        found: true,
                                        x: rect.x + rect.width / 2,
                                        y: rect.y + rect.height / 2,
                                        width: rect.width,
                                        height: rect.height,
                                        src: 'div.cf-turnstile'
                                    });
                                }
                            }
                            return JSON.stringify({found: false});
                        })()
                    """,
                    "returnByValue": True,
                },
            }
            await ws.send(json.dumps(cmd))
            result = json.loads(await ws.recv())
            value = result.get("result", {}).get("result", {}).get("value")
            if value:
                data = json.loads(value)
                if data.get("found"):
                    return data
    except Exception as e:
        logger.debug("CDP find turnstile failed: %s", e)

    return None


async def _is_cloudflare_page(cdp_url: str) -> bool:
    """Check if current page is a Cloudflare challenge."""
    targets = await _get_page_targets(cdp_url)
    page_target = None
    for t in targets:
        if t.get("type") == "page":
            page_target = t
            break
    if not page_target:
        return False

    ws_url = page_target["webSocketDebuggerUrl"]
    if not ws_url:
        return False

    try:
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            cmd = {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": """
                        (() => {
                            const title = (document.title || '').toLowerCase();
                            const body = (document.body ? document.body.innerText : '').toLowerCase().substring(0, 2000);
                            const html = (document.documentElement ? document.documentElement.outerHTML : '').toLowerCase().substring(0, 5000);
                            const indicators = %s;
                            for (const ind of indicators) {
                                if (title.includes(ind) || body.includes(ind) || html.includes(ind)) {
                                    return true;
                                }
                            }
                            // Also check for Turnstile iframes
                            const iframes = document.querySelectorAll('iframe');
                            for (const iframe of iframes) {
                                const src = iframe.src || '';
                                if (src.includes('challenges.cloudflare') || src.includes('turnstile')) {
                                    return true;
                                }
                            }
                            return false;
                        })()
                    """ % json.dumps(list(TURNSTILE_INDICATORS)),
                    "returnByValue": True,
                },
            }
            await ws.send(json.dumps(cmd))
            result = json.loads(await ws.recv())
            return result.get("result", {}).get("result", {}).get("value", False)
    except Exception as e:
        logger.debug("CDP CF check failed: %s", e)
        return False


def _os_click(x: float, y: float) -> None:
    """Click at screen coordinates using pyautogui (OS-level)."""
    os.environ.setdefault("DISPLAY", ":99")
    import pyautogui
    pyautogui.FAILSAFE = False
    # Human-like: add small random offset
    jitter_x = random.uniform(-2, 2)
    jitter_y = random.uniform(-2, 2)
    final_x = max(0, x + jitter_x)
    final_y = max(0, y + jitter_y)
    pyautogui.click(final_x, final_y)
    logger.info("OS click at (%.1f, %.1f)", final_x, final_y)


async def solve_turnstile(cdp_url: str, max_attempts: int = 5, timeout: float = 30.0) -> dict:
    """Detect and solve Cloudflare Turnstile on the current page.

    Returns dict with: solved (bool), attempts (int), error (str|None).
    """
    result = {"solved": False, "attempts": 0, "error": None}
    deadline = time.monotonic() + timeout

    for attempt in range(1, max_attempts + 1):
        if time.monotonic() >= deadline:
            result["error"] = f"Timeout after {timeout}s"
            break

        result["attempts"] = attempt

        # Check if still on CF page
        is_cf = await _is_cloudflare_page(cdp_url)
        if not is_cf:
            result["solved"] = True
            logger.info("No Cloudflare challenge detected (attempt %d)", attempt)
            break

        # Find Turnstile element
        turnstile = await _find_turnstile(cdp_url)
        if not turnstile:
            # CF page but no Turnstile element yet - might be JS challenge
            logger.info("CF page but no Turnstile element, waiting (attempt %d)", attempt)
            await asyncio.sleep(random.uniform(2, 4))
            continue

        x = turnstile["x"]
        y = turnstile["y"]
        logger.info(
            "Turnstile found at (%.1f, %.1f), size=%dx%d (attempt %d)",
            x, y, turnstile.get("width", 0), turnstile.get("height", 0), attempt,
        )

        # Click with OS-level events
        try:
            _os_click(x, y)
        except Exception as e:
            logger.warning("OS click failed: %s", e)
            # Fallback: try CDP Input events
            try:
                await _cdp_click(cdp_url, x, y)
            except Exception as e2:
                logger.warning("CDP click also failed: %s", e2)

        # Wait for Cloudflare to process
        await asyncio.sleep(random.uniform(3, 5))

    if not result["solved"] and not result["error"]:
        result["error"] = f"Failed after {max_attempts} attempts"

    return result


async def _cdp_click(cdp_url: str, x: float, y: float) -> None:
    """Fallback: click using CDP Input.dispatchMouseEvent."""
    targets = await _get_page_targets(cdp_url)
    page_target = None
    for t in targets:
        if t.get("type") == "page":
            page_target = t
            break
    if not page_target:
        return

    ws_url = page_target["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url, open_timeout=5) as ws:
        msg_id = 1
        for event_type in ["mousePressed", "mouseReleased"]:
            cmd = {
                "id": msg_id,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
            }
            await ws.send(json.dumps(cmd))
            await ws.recv()
            msg_id += 1
