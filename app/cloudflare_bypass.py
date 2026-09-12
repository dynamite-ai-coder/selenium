"""Cloudflare bypass powered by invisible_playwright.

`invisible_playwright <https://github.com/feder-cr/invisible_playwright>`_ is a
patched Firefox with a coherent stealth fingerprint and humanised input that
passes Cloudflare's challenges. This module uses it in two modes, because
Cloudflare protects sites in two different ways:

**Interstitial challenge** ("Just a moment..." / "Verify you are human")

    The whole page is replaced by a Cloudflare challenge. invisible_playwright
    solves it, then the resulting cookies (``cf_clearance``, ``__cf_bm``, ...)
    and the User-Agent that minted them are transplanted into the agent's
    visible Chromium over CDP and the page is reloaded.

**Embedded Turnstile widget** (a form - typically a login - that runs a
Turnstile check after submitting)

    A cookie cannot carry the widget's response token, so the stealth browser
    triggers the widget itself, captures the ``cf-turnstile-response`` token
    and hands it to the agent page. The token stays valid for the egress IP for
    a few minutes and is single-use, so the agent should retry the submit right
    away (the UI reports when the token has been injected).

Both modes require the stealth browser and the agent browser to use the same
``BROWSER_PROXY`` (same exit IP), which ``BrowserManager`` enforces by reusing
``BROWSER_PROXY`` for both.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit

from app.utils import safe_error_message

logger = logging.getLogger("browser_agent.cloudflare_bypass")

# Interstitial challenge markers (title / body). Kept deliberately narrow so a
# normal page that merely embeds a Turnstile widget does not match.
TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "checking if the site connection is secure",
    "cf-challenge",
    "enable javascript and cookies",
)
BODY_MARKERS = (
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "checking if the site connection is secure",
    "just a moment",
    "enable javascript and cookies to continue",
    "needs to review the security of your connection",
    "cdn-cgi/challenge-platform",
    "challenge-platform/h/b",
)
TURNSTILE_FRAME_SELECTORS = (
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[title*="cloudflare" i]',
)
TURNSTILE_WIDGET_SELECTORS = (
    ".cf-turnstile",
    "#cf-turnstile",
    "[data-sitekey]",
    "[class*='turnstile' i]",
    'iframe[src*="challenges.cloudflare.com"]',
    'script[src*="challenges.cloudflare.com"]',
)
TURNSTILE_TOKEN_SELECTOR = (
    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
    'input[name$="turnstile-response"]'
)

# JavaScript evaluated in the agent's Chromium (through browser-use's CDP
# session). Returns "interstitial", "turnstile" or "" so the runner can decide
# whether the page is blocked outright or a form widget still needs a token.
CHALLENGE_JS = """
(() => {
  try {
    const title = (document.title || '').toLowerCase();
    const titleMarkers = %s;
    if (titleMarkers.some((m) => title.includes(m))) return 'interstitial';
    if ((location.href || '').includes('/cdn-cgi/')) return 'interstitial';
    if (document.querySelector(
      '#challenge-running, #challenge-stage, #cf-challenge-running, .challenge-running'
    )) return 'interstitial';
    const html = (document.documentElement && document.documentElement.outerHTML)
      ? document.documentElement.outerHTML : '';
    if (html.includes('cf_chl_opt') || html.includes('cf-browser-verification')
        || html.includes('challenge-platform/h/b/orchestrate')) return 'interstitial';
    const body = (document.body && document.body.innerText)
      ? document.body.innerText.toLowerCase().slice(0, 4000) : '';
    const bodyMarkers = %s;
    if (bodyMarkers.some((m) => body.includes(m))) return 'interstitial';

    const tokenNode = document.querySelector(%s);
    const widgetSelectors = %s;
    const widget = widgetSelectors.some((sel) => {
      try { return !!document.querySelector(sel); } catch (e) { return false; }
    });
    // Invisible/interaction-only Turnstile widgets often do not create the
    // hidden response input until the widget runs, so a present widget with
    // no token input still counts as an unsolved challenge.
    if (widget && (!tokenNode || !((tokenNode.value || '').trim()))) return 'turnstile';
    return '';
  } catch (e) {
    return '';
  }
})()
""" % (
    list(TITLE_MARKERS),
    list(BODY_MARKERS),
    json.dumps(TURNSTILE_TOKEN_SELECTOR),
    list(TURNSTILE_WIDGET_SELECTORS),
)

_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None"}


def parse_proxy(raw: str | None) -> Dict[str, str] | None:
    """Convert ``BROWSER_PROXY`` into invisible_playwright's proxy dict.

    Accepts "host:port", "user:pass@host:port" or a full URL with a scheme
    (http://, https://, socks4://, socks5://). Returns ``None`` when empty or
    invalid.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    parts = urlsplit(candidate)
    if not parts.hostname or not parts.port:
        logger.warning("Ignoring invalid proxy value")
        return None
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = parts.username
    if parts.password:
        proxy["password"] = parts.password
    return proxy


def playwright_cookies_to_cdp(cookies: Iterable[dict] | None) -> list[dict]:
    """Convert Playwright cookie dicts into CDP ``Storage.setCookies`` format.

    Playwright returns ``expires=-1`` for session cookies and title-cased
    ``sameSite``; CDP wants a positive epoch or no field at all, and the
    sameSite values are identical in practice ("Strict"/"Lax"/"None").
    """
    converted: list[dict] = []
    for cookie in cookies or []:
        name = cookie.get("name")
        if not name or cookie.get("value") is None:
            continue
        entry: Dict[str, Any] = {"name": str(name), "value": str(cookie.get("value", ""))}
        if cookie.get("domain"):
            entry["domain"] = str(cookie["domain"])
        if cookie.get("path"):
            entry["path"] = str(cookie["path"])
        expires = cookie.get("expires")
        try:
            if expires is not None and float(expires) > 0:
                entry["expires"] = float(expires)
        except (TypeError, ValueError):
            pass
        if cookie.get("httpOnly"):
            entry["httpOnly"] = True
        if cookie.get("secure"):
            entry["secure"] = True
        same_site = cookie.get("sameSite")
        if same_site:
            entry["sameSite"] = _SAME_SITE.get(str(same_site).lower(), str(same_site))
        converted.append(entry)
    return converted


async def detect_challenge(session: Any) -> str | None:
    """Return the Cloudflare challenge kind on the agent's focused page.

    Returns ``"interstitial"``, ``"turnstile"`` or ``None``. ``session`` is a
    browser-use ``BrowserSession``; the probe runs through its CDP connection.
    """
    if session is None:
        return None
    try:
        cdp_session = await session.get_or_create_cdp_session()
    except Exception as exc:
        logger.warning("Cloudflare detection could not open a CDP session: %s", safe_error_message(exc))
        return None
    try:
        result = await asyncio.wait_for(
            cdp_session.cdp_client.send.Runtime.evaluate(
                params={"expression": CHALLENGE_JS, "returnByValue": True},
                session_id=cdp_session.session_id,
            ),
            timeout=6.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Cloudflare detection timed out - the page may be unresponsive")
        return None
    except Exception as exc:
        logger.warning("Cloudflare detection failed: %s", safe_error_message(exc))
        return None
    kind = result.get("result", {}).get("value")
    if kind in ("interstitial", "turnstile"):
        logger.info("Cloudflare challenge detected (%s)", kind)
        return kind
    return None


class CloudflareBypass:
    """Solve Cloudflare in a stealth Firefox (sync API in a worker thread).

    :meth:`solve` is async and never blocks the event loop.
    """

    def __init__(
        self,
        proxy: Dict[str, str] | None = None,
        headless: bool = False,
        timeout: float = 90.0,
        seed: int | None = None,
        locale: str = "auto",
        timezone: str = "",
        profile_dir: str | None = None,
        click_turnstile: bool = True,
        login: Dict[str, str] | None = None,
    ) -> None:
        self.proxy = proxy
        self.headless = headless
        self.timeout = max(15.0, float(timeout))
        self.seed = seed
        self.locale = locale or "auto"
        self.timezone = timezone or ""
        self.profile_dir = profile_dir
        self.click_turnstile = click_turnstile
        # Credentials already typed into the agent's page. When present the
        # stealth browser performs the real login (so the site's own Turnstile
        # flow runs with a genuine click) and we transplant the session cookies,
        # instead of trying to steal a single-use widget token.
        self.login = login or None
        self.last_seed: int | None = seed

    async def solve(self, url: str) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._solve_sync, url)

    # -- synchronous implementation (worker thread) -------------------------
    def _solve_sync(self, url: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "mode": None,
            "cookies": [],
            "cf_clearance": None,
            "turnstile_token": None,
            "user_agent": None,
            "error": None,
            "method": "invisible_playwright",
            "seed": self.seed,
        }
        try:
            from invisible_playwright import InvisiblePlaywright
        except Exception as exc:
            result["error"] = (
                "invisible-playwright is not installed or not usable: "
                f"{safe_error_message(exc)}"
            )
            return result

        deadline = time.monotonic() + self.timeout
        launch_kwargs: Dict[str, Any] = {"headless": self.headless, "locale": self.locale}
        if self.proxy:
            launch_kwargs["proxy"] = self.proxy
        if self.timezone:
            launch_kwargs["timezone"] = self.timezone
        if self.seed is not None:
            launch_kwargs["seed"] = self.seed
        if self.profile_dir:
            launch_kwargs["profile_dir"] = self.profile_dir

        try:
            driver = InvisiblePlaywright(**launch_kwargs)
            self.last_seed = driver.seed
            result["seed"] = driver.seed
            with driver as browser:
                context = browser.contexts[0] if getattr(browser, "contexts", None) else browser
                page = context.new_page()
                logger.info("Solving Cloudflare challenge for %s (stealth Firefox)", url)
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=min(120_000, int(self.timeout * 1000)),
                )

                clicked_at = 0.0
                armed = False
                login_submitted_at = 0.0
                last_probe = 0.0
                while time.monotonic() < deadline:
                    now_probe = time.monotonic()
                    if now_probe - last_probe >= 6.0:
                        last_probe = now_probe
                        self._log_page_state(page)
                    if self._is_interstitial(page):
                        now = time.monotonic()
                        if self.click_turnstile and (now - clicked_at) >= 4.0:
                            if self._click_turnstile(page):
                                clicked_at = now
                        time.sleep(1.0)
                        continue

                    if self._has_turnstile(page):
                        if self.login and not login_submitted_at:
                            if self._submit_login(page):
                                login_submitted_at = time.monotonic()
                                logger.info(
                                    "Submitted the real login form in the stealth browser"
                                )
                                time.sleep(2.0)
                                continue

                        if self._turnstile_solved(page):
                            if login_submitted_at:
                                # Give the login XHR time to set the session
                                # cookie and navigate before transplanting.
                                settle_deadline = time.monotonic() + 20
                                while time.monotonic() < settle_deadline:
                                    if self._login_moved_on(page, url):
                                        break
                                    try:
                                        page.wait_for_load_state("networkidle", timeout=3000)
                                    except Exception:
                                        pass
                                    time.sleep(1.0)
                                result["mode"] = "login"
                                result["note"] = (
                                    "Login submitted with a fresh Turnstile token; "
                                    "transplanting session cookies"
                                )
                                return self._success(context, page, result)
                            result["turnstile_token"] = self._read_turnstile_token(page)
                            result["mode"] = "turnstile_token"
                            return self._success(context, page, result)

                        if login_submitted_at:
                            # Challenge still running after the submit: click an
                            # interactive widget when one is rendered.
                            now = time.monotonic()
                            if self.click_turnstile and (now - clicked_at) >= 4.0:
                                if self._click_turnstile(page):
                                    clicked_at = now
                        elif not self.login:
                            if not armed:
                                self._arm_submit_capture(page)
                                self._trigger_turnstile(page)
                                armed = True
                            else:
                                self._trigger_turnstile(page, repeat=False)
                        time.sleep(1.0)
                        continue

                    result["mode"] = "interstitial"
                    return self._success(context, page, result)

                if login_submitted_at and self._login_moved_on(page, url):
                    result["mode"] = "login"
                    result["note"] = "Login submitted; cookies transplanted at timeout"
                    logger.info("Login completed in the stealth browser")
                    return self._success(context, page, result)
                return self._timeout_result(context, page, result)
        except Exception as exc:
            result["error"] = safe_error_message(exc)
            logger.warning("Cloudflare bypass failed: %s", result["error"])
        return result

    def _success(self, context: Any, page: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        self._collect(context, page, result)
        result["success"] = True
        if result["mode"] == "turnstile_token":
            logger.info("Captured a fresh Turnstile token for transplant")
        elif result["mode"] == "login":
            logger.info("Login completed in the stealth browser; transplanting session cookies")
        elif result["cf_clearance"]:
            logger.info("Cloudflare interstitial cleared; cf_clearance obtained")
        else:
            result["note"] = "Challenge cleared without a cf_clearance cookie"
            logger.info("Cloudflare challenge cleared (no cf_clearance cookie)")
        return result

    def _timeout_result(self, context: Any, page: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        self._collect(context, page, result)
        result["turnstile_token"] = self._read_turnstile_token(page) or result.get("turnstile_token")
        if result["turnstile_token"]:
            result["success"] = True
            result["mode"] = "turnstile_token"
            result["note"] = "Token captured at the end of the timeout window"
            logger.info("Turnstile token captured at timeout")
        elif result["cf_clearance"] or self._turnstile_solved(page):
            result["success"] = True
            result["note"] = "Challenge was still settling when the timeout expired"
            logger.info("Cloudflare bypass reached timeout but clearance was present")
        else:
            result["error"] = f"Timeout after {self.timeout:.0f}s (no clearance obtained)"
            logger.warning("Cloudflare bypass %s", result["error"])
        return result

    @staticmethod
    def _collect(context: Any, page: Any, result: Dict[str, Any]) -> None:
        try:
            cookies = context.cookies()
        except Exception as exc:
            logger.debug("Could not read bypass cookies: %s", safe_error_message(exc))
            cookies = []
        result["cookies"] = cookies
        result["cf_clearance"] = next(
            (cookie.get("value") for cookie in cookies if cookie.get("name") == "cf_clearance"),
            None,
        )
        if not result.get("user_agent"):
            try:
                result["user_agent"] = page.evaluate("() => navigator.userAgent")
            except Exception:
                pass

    # -- challenge helpers ---------------------------------------------------
    @staticmethod
    def _log_page_state(page: Any) -> None:
        """Throttled INFO probe of what the stealth browser currently sees."""
        try:
            info = page.evaluate(
                """
                () => {
                  const q = (s) => { try { return document.querySelectorAll(s).length; }
                                     catch (e) { return -1; } };
                  const token = document.querySelector('input[name*="turnstile-response"]');
                  return {
                    url: location.href,
                    title: document.title,
                    iframe: q('iframe[src*="challenges.cloudflare.com"]'),
                    widget: q('.cf-turnstile, [data-sitekey]'),
                    token_len: token ? (token.value || '').length : -1,
                    api: typeof window.turnstile,
                    body: (document.body ? document.body.innerText : '').slice(0, 120),
                  };
                }
                """
            )
            logger.info("Stealth page: %s", json.dumps(info, ensure_ascii=False)[:420])
        except Exception as exc:
            logger.info("Stealth page probe failed: %s", safe_error_message(exc))

    @staticmethod
    def _is_interstitial(page: Any) -> bool:
        try:
            title = (page.title() or "").lower()
        except Exception:
            title = ""
        if any(marker in title for marker in TITLE_MARKERS):
            return True
        try:
            body = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase().slice(0, 4000)"
            ) or ""
        except Exception:
            body = ""
        if any(marker in body for marker in BODY_MARKERS):
            return True
        try:
            html = (page.content() or "").lower()
        except Exception:
            html = ""
        # "challenge-platform" alone appears on any page running the Turnstile
        # api.js; only the orchestrate path marks a real interstitial.
        return (
            "cf_chl_opt" in html
            or "cf-browser-verification" in html
            or "challenge-platform/h/b/orchestrate" in html
        )

    @staticmethod
    def _has_turnstile(page: Any) -> bool:
        for selector in TURNSTILE_FRAME_SELECTORS + TURNSTILE_WIDGET_SELECTORS:
            try:
                if page.query_selector(selector):
                    return True
            except Exception:
                continue
        return False

    _LOGIN_BUTTON_RE = (
        r"log\s*in|sign\s*in|logowanie|zaloguj|einloggen|anmelden|connexion|iniciar|"
        r"submit|continue|verify"
    )
    _PASSWORD_OPTION_RE = (
        r"hasł|haslo|password|passwort|mot de passe|contrase|senha|wachtwoord|"
        r"heslo|jelsz|пароль"
    )
    _SOCIAL_BUTTON_RE = (
        r"with\s+(x|twitter|google|facebook|apple|steam|discord|paypal|microsoft)|"
        r"continue with|sign in with|sign up with|social|magic\s*link|zdo[b]?ąd[zź]"
    )

    @staticmethod
    def _fill_field(page: Any, selectors: list[str], value: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    ([selectors, value]) => {
                      const el = selectors
                        .map((sel) => { try { return document.querySelector(sel); } catch (e) { return null; } })
                        .find(Boolean);
                      if (!el) return false;
                      const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                      if (setter) setter.call(el, value); else el.value = value;
                      el.dispatchEvent(new Event('input', { bubbles: true }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                      el.focus();
                      return true;
                    }
                    """,
                    [selectors, value],
                )
            )
        except Exception as exc:
            logger.debug("Could not fill the stealth login field: %s", safe_error_message(exc))
            return False

    def _click_label(self, page: Any, pattern: str, *, exclude: str | None = None) -> bool:
        try:
            controls = page.query_selector_all("button, input[type=submit], a[role=button]")
        except Exception:
            controls = []
        for control in controls:
            try:
                label = " ".join(
                    filter(
                        None,
                        [
                            control.inner_text() or "",
                            control.get_attribute("value") or "",
                            control.get_attribute("aria-label") or "",
                        ],
                    )
                ).strip()
            except Exception:
                label = ""
            if not label:
                continue
            if exclude and re.search(exclude, label, re.IGNORECASE):
                continue
            if re.search(pattern, label, re.IGNORECASE):
                try:
                    control.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    control.click(timeout=5000)
                    logger.info("Stealth login: clicked %r", label[:60])
                    return True
                except Exception as exc:
                    logger.debug("Stealth login click failed: %s", safe_error_message(exc))
                    # Cookie banners and overlays can swallow a coordinate
                    # click; a JS click dispatches straight to the element.
                    try:
                        control.evaluate("el => el.click()")
                        logger.info("Stealth login: JS-clicked %r", label[:60])
                        return True
                    except Exception as exc2:
                        logger.debug("Stealth login JS click failed: %s", safe_error_message(exc2))
        return False

    def _submit_login(self, page: Any) -> bool:
        """Replay the real login in the stealth browser.

        eneba-style forms are two-step: the password field only exists after
        clicking "Log in with password" with the e-mail already typed. The
        stealth browser therefore fills the e-mail, reveals the password field
        when needed, fills it and clicks the final login control, so the site's
        own Turnstile flow runs with a genuine click. Social login buttons and
        the "magic link" option are skipped.
        """
        if not self.login:
            return False
        logger.info("Stealth login: attempting with %s***", str(self.login.get("email", ""))[:3])
        email_selectors = [
            'input[type="email"]',
            'input[name="username"]',
            'input[name*="email" i]',
            'input[name*="login" i]',
        ]
        try:
            page.wait_for_selector(
                ', '.join(email_selectors), timeout=15000, state="attached"
            )
        except Exception:
            logger.warning("Stealth login: no e-mail field on the page")
            return False
        if not self._fill_field(page, email_selectors, self.login.get("email", "")):
            logger.warning("Stealth login: could not fill the e-mail field")
            return False

        if not page.query_selector('input[type="password"]'):
            if not self._click_label(page, self._PASSWORD_OPTION_RE, exclude=self._SOCIAL_BUTTON_RE):
                try:
                    labels = [
                        (control.inner_text() or control.get_attribute("value") or "").strip()
                        for control in page.query_selector_all(
                            "button, input[type=submit], a[role=button]"
                        )
                    ]
                except Exception:
                    labels = []
                logger.warning(
                    "Stealth login: no 'log in with password' control found; buttons=%s",
                    labels[:12],
                )
                return False
            try:
                page.wait_for_selector('input[type="password"]', timeout=15000, state="visible")
            except Exception:
                logger.warning("Stealth login: password field never appeared")
                return False

        if not self._fill_field(page, ['input[type="password"]'], self.login.get("password", "")):
            logger.warning("Stealth login: could not fill the password field")
            return False

        # Prefer an exact "log in" label; only then the broader regex (without
        # the social and "with password" option labels).
        if self._click_label(page, r"^\s*(zaloguj się|zaloguj|log ?in|login|sign ?in|einloggen|anmelden|connexion|iniciar sesión)\s*$"):
            return True
        if self._click_label(
            page,
            self._LOGIN_BUTTON_RE,
            exclude=self._SOCIAL_BUTTON_RE + "|" + self._PASSWORD_OPTION_RE,
        ):
            return True
        # Last resort: submit the password field's form / press Enter in it.
        try:
            submitted = page.evaluate(
                """
                () => {
                  const password = document.querySelector('input[type=password]');
                  const scope = (password && password.form) ? password.form : document.querySelector('form');
                  if (!scope) return false;
                  if (typeof scope.requestSubmit === 'function') { scope.requestSubmit(); return true; }
                  scope.submit(); return true;
                }
                """
            )
            if submitted:
                logger.info("Stealth login: submitted the form directly")
                return True
        except Exception as exc:
            logger.debug("Fallback login submit failed: %s", safe_error_message(exc))
        try:
            page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    @staticmethod
    def _login_moved_on(page: Any, original_url: str) -> bool:
        try:
            current = (page.url or "").lower()
        except Exception:
            return False
        return not any(part in current for part in ("/login", "sign-in", "signin", "anmelden"))

    @staticmethod
    def _read_turnstile_token(page: Any) -> str | None:
        try:
            value = page.evaluate(
                """
                () => {
                  const node = document.querySelector(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"],'
                    + ' input[name$="turnstile-response"]'
                  );
                  return node ? (node.value || '').trim() : '';
                }
                """
            )
        except Exception:
            return None
        return value or None

    @staticmethod
    def _turnstile_solved(page: Any) -> bool:
        return bool(CloudflareBypass._read_turnstile_token(page))

    @staticmethod
    def _arm_submit_capture(page: Any) -> None:
        """Capture the Turnstile token and stop the stealth form from submitting.

        The token is only minted when the widget runs; sites usually run it on
        submit. We listen in the capture phase, store the token and cancel the
        submission so the token stays unused for the agent's own page.
        """
        try:
            page.evaluate(
                """
                () => {
                  if (window.__ipwCaptureArmed) return;
                  window.__ipwCaptureArmed = true;
                  window.__ipwToken = '';
                  const selector = 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"],'
                    + ' input[name$="turnstile-response"]';
                  document.addEventListener('submit', (event) => {
                    const node = document.querySelector(selector);
                    window.__ipwToken = node ? (node.value || '').trim() : '';
                    event.preventDefault();
                    event.stopImmediatePropagation();
                  }, true);
                }
                """
            )
        except Exception as exc:
            logger.debug("Could not arm submit capture: %s", safe_error_message(exc))

    def _trigger_turnstile(self, page: Any, repeat: bool = True) -> None:
        """Ask the widget to run (explicit API first, form submit fallback)."""
        try:
            triggered = page.evaluate(
                """
                () => {
                  const api = window.turnstile;
                  if (api && typeof api.execute === 'function') {
                    const widgets = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
                    let calls = 0;
                    for (const widget of widgets) {
                      try { api.execute(widget); calls += 1; } catch (e) {}
                    }
                    if (calls) return 'api';
                  }
                  return '';
                }
                """
            )
            if triggered == "api":
                return
        except Exception as exc:
            logger.debug("turnstile.execute failed: %s", safe_error_message(exc))

        if not repeat:
            return
        # Fallback: satisfy simple form validation with placeholder values and
        # click a submit control. The capture listener cancels the submit.
        try:
            page.evaluate(
                """
                () => {
                  const set = (el, value) => {
                    const proto = el.tagName === 'TEXTAREA'
                      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    if (setter) setter.call(el, value); else el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                  };
                  for (const el of document.querySelectorAll(
                    'input[type=email], input[name*=email i], input[name*=login i], input[name*=user i]'
                  )) { if (!el.value) set(el, 'bypass@example.com'); }
                  for (const el of document.querySelectorAll('input[type=password]')) {
                    if (!el.value) set(el, 'BypassPassword123!');
                  }
                  const controls = [...document.querySelectorAll('button, input[type=submit]')];
                  const submit = controls.find((el) =>
                    /log.?in|sign.?in|continue|submit|verify/i.test((el.innerText || el.value || '').trim())
                  ) || controls.find((el) => el.type === 'submit');
                  if (submit) submit.click();
                }
                """
            )
        except Exception as exc:
            logger.debug("Fallback submit trigger failed: %s", safe_error_message(exc))

    def _click_turnstile(self, page: Any) -> bool:
        """Click the Turnstile checkbox (bounding box first, frame fallback)."""
        try:
            element = page.query_selector(TURNSTILE_FRAME_SELECTORS[0])
            if element is None:
                element = page.query_selector(TURNSTILE_FRAME_SELECTORS[1])
            if element is not None:
                element.scroll_into_view_if_needed(timeout=2000)
                box = element.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    x = box["x"] + min(34.0, box["width"] / 2.0)
                    y = box["y"] + box["height"] / 2.0
                    page.mouse.move(x, y)
                    page.mouse.click(x, y)
                    logger.info("Clicked Turnstile checkbox at (%.0f, %.0f)", x, y)
                    return True
        except Exception as exc:
            logger.debug("Turnstile bounding-box click failed: %s", safe_error_message(exc))

        for selector in TURNSTILE_FRAME_SELECTORS:
            try:
                frame = page.frame_locator(selector)
                frame.locator("#challenge-stage, input[type=checkbox], label, body").first.click(
                    timeout=2000
                )
                logger.info("Clicked Turnstile widget through frame locator")
                return True
            except Exception:
                continue
        logger.info("No clickable Turnstile frame found")
        return False


async def bypass_url(
    url: str,
    proxy: Dict[str, str] | str | None = None,
    timeout: float = 90.0,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Convenience async wrapper to solve a single URL."""
    if isinstance(proxy, str):
        proxy = parse_proxy(proxy)
    bypass = CloudflareBypass(proxy=proxy, timeout=timeout, **kwargs)
    return await bypass.solve(url)
