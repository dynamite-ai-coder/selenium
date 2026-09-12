"""Application configuration.

All secrets and deployment specific values are read from environment
variables (optionally via a local ``.env`` file). Nothing sensitive is
hardcoded in the application logic.
"""

from __future__ import annotations

import secrets
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Locations that are commonly used for Chrome/Chromium binaries. The first
# match is used when CHROME_BIN is not explicitly configured.
CHROME_CANDIDATES = (
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/local/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/local/bin/chromium",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Authentication -----------------------------------------------------
    app_password: str = ""
    session_secret: str = ""
    cookie_secure: bool = False
    session_hours: int = 24
    login_max_attempts: int = 10
    login_lockout_seconds: int = 300

    # --- DeepSeek / LLM -----------------------------------------------------
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_temperature: float = 0.0
    deepseek_timeout: float = 120.0

    # --- HTTP server --------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 10000

    # --- Browser / desktop --------------------------------------------------
    display: str = ":99"
    screen_width: int = 1920
    screen_height: int = 1080
    browser_headless: bool = False
    cdp_url: str = "http://127.0.0.1:9222"
    cdp_url_file: str = "/tmp/cdp_url"
    chrome_bin: str = ""
    browser_profile_dir: str = "browser_profile"
    # Stealth / localization. Empty values keep the browser defaults.
    # BROWSER_PROXY accepts "host:port", "user:pass@host:port" or a full
    # URL with scheme (http://, https://, socks5://).
    browser_proxy: str = ""
    browser_tz: str = ""
    browser_lang: str = ""
    browser_geolocation: str = ""  # "lat,lon"
    downloads_dir: str = "downloads"
    uploads_dir: str = "uploads"
    novnc_dir: str = "/usr/share/novnc"
    vnc_port: int = 5900
    novnc_port: int = 6080

    # --- Cloudflare Bypass (invisible_playwright stealth Firefox) ------------
    cf_bypass_enabled: bool = True
    cf_bypass_timeout: float = 90.0
    # The stealth Firefox is patched for both modes, but Cloudflare scores a
    # real GUI session (Xvfb) better than a hidden display, so the default is
    # headed on the container display and visible in the noVNC preview.
    cf_bypass_headless: bool = False
    # 0 = random fingerprint per solve; any other value is reproducible.
    cf_bypass_seed: int = 0
    # "auto" derives the language from the egress IP (matches BROWSER_PROXY).
    cf_bypass_locale: str = "auto"
    # Empty = one-off profile; set to reuse the stealth profile between solves.
    cf_bypass_profile_dir: str = ""
    cf_bypass_click_turnstile: bool = True
    # Minimum seconds between bypass attempts for the same host.
    cf_bypass_cooldown: float = 90.0

    # --- Agent --------------------------------------------------------------
    agent_max_steps: int = 100
    agent_task_timeout: int = 900
    agent_use_vision: bool = True
    agent_use_judge: bool = False
    agent_enable_planning: bool = True

    # --- Files --------------------------------------------------------------
    max_upload_mb: int = 10
    max_task_chars: int = 4000

    # --- Derived paths ------------------------------------------------------
    @property
    def browser_profile_path(self) -> Path:
        return self._resolve(self.browser_profile_dir)

    @property
    def cf_bypass_profile_path(self) -> Path | None:
        if not self.cf_bypass_profile_dir.strip():
            return None
        return self._resolve(self.cf_bypass_profile_dir)

    @property
    def downloads_path(self) -> Path:
        return self._resolve(self.downloads_dir)

    @property
    def uploads_path(self) -> Path:
        return self._resolve(self.uploads_dir)

    @property
    def logs_path(self) -> Path:
        return BASE_DIR / "logs"

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    # --- Capability helpers -------------------------------------------------
    @property
    def effective_session_secret(self) -> str:
        """Return a stable secret; generate an ephemeral one if unset."""
        return self.session_secret or self._ephemeral_secret()

    @property
    def auth_configured(self) -> bool:
        return bool(self.app_password)

    @property
    def llm_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def resolved_chrome_bin(self) -> str | None:
        if self.chrome_bin:
            return self.chrome_bin
        for candidate in CHROME_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        return found

    @property
    def novnc_path(self) -> Path:
        return Path(self.novnc_dir)

    _ephemeral: str | None = None

    def _ephemeral_secret(self) -> str:
        if self._ephemeral is None:
            self._ephemeral = secrets.token_urlsafe(48)
        return self._ephemeral

    def ensure_directories(self) -> None:
        for path in (
            self.browser_profile_path,
            self.downloads_path,
            self.uploads_path,
            self.logs_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
