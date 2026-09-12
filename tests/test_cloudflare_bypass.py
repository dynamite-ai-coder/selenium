"""Unit tests for the invisible_playwright Cloudflare bypass helpers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cloudflare_bypass import (  # noqa: E402
    CloudflareBypass,
    detect_challenge,
    parse_proxy,
    playwright_cookies_to_cdp,
)


# ---------------------------------------------------------------------------
# parse_proxy
# ---------------------------------------------------------------------------
def test_parse_proxy_empty():
    assert parse_proxy(None) is None
    assert parse_proxy("") is None
    assert parse_proxy("   ") is None


def test_parse_proxy_plain_host_port():
    assert parse_proxy("gate.example.com:1080") == {
        "server": "http://gate.example.com:1080"
    }


def test_parse_proxy_credentials_and_scheme():
    parsed = parse_proxy("socks5://user:p%40ss@gate.example.com:1080")
    assert parsed == {
        "server": "socks5://gate.example.com:1080",
        "username": "user",
        "password": "p%40ss",
    }


def test_parse_proxy_invalid_is_ignored():
    assert parse_proxy("not-a-proxy") is None


# ---------------------------------------------------------------------------
# playwright_cookies_to_cdp
# ---------------------------------------------------------------------------
def test_cookie_conversion_drops_session_expiry_and_normalises_same_site():
    cookies = [
        {
            "name": "cf_clearance",
            "value": "abc",
            "domain": ".example.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "__cf_bm",
            "value": "def",
            "domain": ".example.com",
            "path": "/",
            "expires": 1893456000.5,
            "httpOnly": False,
            "secure": True,
            "sameSite": "lax",
        },
        {"name": "", "value": "skip"},
        {"name": "no_value", "value": None},
    ]
    converted = playwright_cookies_to_cdp(cookies)
    assert converted == [
        {
            "name": "cf_clearance",
            "value": "abc",
            "domain": ".example.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "__cf_bm",
            "value": "def",
            "domain": ".example.com",
            "path": "/",
            "expires": 1893456000.5,
            "secure": True,
            "sameSite": "Lax",
        },
    ]


def test_cookie_conversion_handles_empty():
    assert playwright_cookies_to_cdp(None) == []
    assert playwright_cookies_to_cdp([]) == []


# ---------------------------------------------------------------------------
# challenge helpers
# ---------------------------------------------------------------------------
class FakePage:
    def __init__(self, title="", body="", html="", turnstile=False, token=""):
        self._title = title
        self._body = body
        self._html = html
        self._turnstile = turnstile
        self._token = token

    def title(self):
        return self._title

    def evaluate(self, script):
        if "innerText" in script:
            return self._body.lower()[:4000]
        if "cf-turnstile-response" in script:
            return self._token
        return None

    def content(self):
        return self._html

    def query_selector(self, _selector):
        return object() if self._turnstile else None


def test_interstitial_detection_title():
    assert CloudflareBypass._is_interstitial(FakePage(title="Just a moment...")) is True


def test_interstitial_detection_html_marker():
    page = FakePage(html="<html>var cf_chl_opt = {}</html>")
    assert CloudflareBypass._is_interstitial(page) is True


def test_interstitial_detection_normal_page():
    page = FakePage(title="Example Domain", body="This domain is for use in examples.")
    assert CloudflareBypass._is_interstitial(page) is False


def test_turnstile_helpers():
    pending = FakePage(turnstile=True, token="")
    solved = FakePage(turnstile=True, token="0.abc-token")
    assert CloudflareBypass._has_turnstile(pending) is True
    assert CloudflareBypass._turnstile_solved(pending) is False
    assert CloudflareBypass._read_turnstile_token(pending) is None
    assert CloudflareBypass._turnstile_solved(solved) is True
    assert CloudflareBypass._read_turnstile_token(solved) == "0.abc-token"


def test_site_error_detection():
    rejected = FakePage(body="Einloggen Ungültige Zugangsdaten angegeben Einloggen")
    assert CloudflareBypass._site_error(rejected) is not None
    assert "zugangsdaten" in CloudflareBypass._site_error(rejected).lower()
    assert CloudflareBypass._site_error(FakePage(body="Everything is fine")) is None


def test_click_turnstile_uses_bounding_box():
    class FakeElement:
        def scroll_into_view_if_needed(self, timeout=None):
            return None

        def bounding_box(self):
            return {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}

    class FakeMouse:
        def __init__(self):
            self.clicks = []

        def move(self, x, y):
            self.clicks.append(("move", x, y))

        def click(self, x, y):
            self.clicks.append(("click", x, y))

    class FakeMousePage(FakePage):
        def __init__(self):
            super().__init__(turnstile=True)
            self.mouse = FakeMouse()

        def query_selector(self, selector):
            return FakeElement() if "challenges.cloudflare.com" in selector else None

    page = FakeMousePage()
    bypass = CloudflareBypass()
    assert bypass._click_turnstile(page) is True
    assert ("move", 99.0, 217.5) in page.mouse.clicks
    assert ("click", 124.0, 232.5) in page.mouse.clicks


# ---------------------------------------------------------------------------
# detect_challenge (agent browser side)
# ---------------------------------------------------------------------------
class FakeCdpSession:
    class _Runtime:
        def __init__(self, value):
            self._value = value

        async def evaluate(self, params, session_id=None):
            return {"result": {"value": self._value}}

    class _Send:
        def __init__(self, value):
            self.Runtime = FakeCdpSession._Runtime(value)

    class _Client:
        def __init__(self, value):
            self.send = FakeCdpSession._Send(value)

    def __init__(self, value):
        self.cdp_client = FakeCdpSession._Client(value)
        self.session_id = "s1"


class FakeSession:
    def __init__(self, value):
        self._value = value

    async def get_or_create_cdp_session(self, target_id=None):
        return FakeCdpSession(self._value)


def test_detect_challenge_kinds():
    assert asyncio.run(detect_challenge(FakeSession("interstitial"))) == "interstitial"
    assert asyncio.run(detect_challenge(FakeSession("turnstile"))) == "turnstile"
    assert asyncio.run(detect_challenge(FakeSession(""))) is None
    assert asyncio.run(detect_challenge(FakeSession(None))) is None
    assert asyncio.run(detect_challenge(None)) is None


def test_detect_challenge_never_raises():
    class Broken:
        async def get_or_create_cdp_session(self, target_id=None):
            raise RuntimeError("no browser")

    assert asyncio.run(detect_challenge(Broken())) is None
