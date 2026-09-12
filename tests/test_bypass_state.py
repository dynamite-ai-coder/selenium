"""Tests for the Cloudflare solve state machine and the CDP state transfer."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import BrowserManager  # noqa: E402
from app.cloudflare_bypass import CloudflareBypass  # noqa: E402


# ---------------------------------------------------------------------------
# Fake invisible_playwright browser for the solve loop
# ---------------------------------------------------------------------------
class FakeElement:
    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def bounding_box(self):
        return {"x": 10.0, "y": 20.0, "width": 300.0, "height": 65.0}


class FakeMouse:
    def move(self, x, y):
        return None

    def click(self, x, y):
        return None


class FakePage:
    def __init__(self, mode: str):
        self.mode = mode
        self.token = ""
        self.polls = 0
        self.mouse = FakeMouse()

    def goto(self, url, **kwargs):
        return None

    def title(self):
        self.polls += 1
        if self.mode == "interstitial" and self.polls <= 2:
            return "Just a moment..."
        return "Log in"

    def evaluate(self, script):
        if "innerText" in script:
            if self.mode == "interstitial" and self.polls <= 2:
                return "checking your browser"
            return "log in form"
        if "__ipwCaptureArmed" in script:
            return None
        if "api.execute" in script:
            self.token = "TS-TOKEN-123"
            return "api"
        if "cf-turnstile-response" in script:
            return self.token
        return None

    def content(self):
        if self.mode == "interstitial" and self.polls <= 2:
            return "<html>cf_chl_opt</html>"
        return "<html>login</html>"

    def query_selector(self, selector):
        if self.mode == "turnstile":
            return FakeElement()
        return None

    def frame_locator(self, selector):
        raise RuntimeError("no frames")


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page

    def cookies(self):
        return [
            {
                "name": "cf_clearance",
                "value": "clearance-value",
                "domain": ".eneba.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        ]


class FakeDriver:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.seed = 4242
        self._page = FakePage(kwargs.pop("_mode", "turnstile"))

    def __enter__(self):
        browser = types.SimpleNamespace(contexts=[FakeContext(self._page)])
        return browser

    def __exit__(self, *args):
        return False


def _install_fake_driver(monkeypatch, mode: str):
    module = types.ModuleType("invisible_playwright")

    def factory(**kwargs):
        kwargs["_mode"] = mode
        return FakeDriver(**kwargs)

    module.InvisiblePlaywright = factory
    monkeypatch.setitem(sys.modules, "invisible_playwright", module)


def test_solve_captures_turnstile_token(monkeypatch):
    _install_fake_driver(monkeypatch, "turnstile")
    result = CloudflareBypass(timeout=15)._solve_sync("https://my.eneba.com/login")
    assert result["success"] is True
    assert result["mode"] == "turnstile_token"
    assert result["turnstile_token"] == "TS-TOKEN-123"
    assert result["cf_clearance"] == "clearance-value"
    assert result["seed"] == 4242


def test_solve_interstitial_clears(monkeypatch):
    _install_fake_driver(monkeypatch, "interstitial")
    result = CloudflareBypass(timeout=15)._solve_sync("https://www.eneba.com/")
    assert result["success"] is True
    assert result["mode"] == "interstitial"
    assert result["turnstile_token"] is None
    assert result["cf_clearance"] == "clearance-value"


# ---------------------------------------------------------------------------
# apply_bypass_state (CDP transfer into the agent browser)
# ---------------------------------------------------------------------------
class FakeSend:
    def __init__(self):
        self.calls = []

        outer = self

        class Emulation:
            async def setUserAgentOverride(self, params, session_id=None):
                outer.calls.append(("ua", params["userAgent"]))

        class Storage:
            async def setCookies(self, params, session_id=None):
                outer.calls.append(("cookies", len(params["cookies"])))

        class Page:
            async def reload(self, params=None, session_id=None):
                outer.calls.append(("reload", None))

        class Runtime:
            async def evaluate(self, params, session_id=None):
                outer.calls.append(("evaluate", "turnstile" if "turnstile" in params["expression"] else "other"))
                return {"result": {"value": {"injected": True, "submitted": True}}}

        self.Emulation = Emulation()
        self.Storage = Storage()
        self.Page = Page()
        self.Runtime = Runtime()


class FakeCdpSession:
    def __init__(self):
        self.session_id = "session-1"
        self.cdp_client = types.SimpleNamespace(send=FakeSend())


class FakeBrowserSession:
    def __init__(self):
        self.cdp = FakeCdpSession()

    async def get_or_create_cdp_session(self, target_id=None):
        return self.cdp


COOKIES = [
    {
        "name": "cf_clearance",
        "value": "abc",
        "domain": ".eneba.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
]


def test_apply_bypass_state_injects_token_without_reload():
    manager = BrowserManager()
    manager._session = FakeBrowserSession()
    result = {
        "user_agent": "Mozilla/5.0 Firefox/151.0",
        "cookies": COOKIES,
        "turnstile_token": "T",
    }
    asyncio.run(manager.apply_bypass_state(result))
    calls = manager._session.cdp.cdp_client.send.calls
    assert ("ua", "Mozilla/5.0 Firefox/151.0") in calls
    assert ("cookies", 1) in calls
    assert any(name == "evaluate" and kind == "turnstile" for name, kind in calls)
    assert all(name != "reload" for name, _ in calls)
    assert manager._cf_user_agent == "Mozilla/5.0 Firefox/151.0"
    assert result["turnstile_submitted"] is True


def test_apply_bypass_state_reloads_for_interstitial():
    manager = BrowserManager()
    manager._session = FakeBrowserSession()
    asyncio.run(
        manager.apply_bypass_state(
            {"user_agent": "UA", "cookies": COOKIES, "turnstile_token": None}
        )
    )
    calls = manager._session.cdp.cdp_client.send.calls
    assert ("reload", None) in calls
