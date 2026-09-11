"""Authentication: password login, signed session cookies and rate limiting.

Sessions are stored server side (in memory) and referenced by an
HMAC-signed, HTTP-only cookie. This keeps the password and tokens out of
frontend JavaScript and lets us revoke sessions on logout.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket, status

from app.config import settings

logger = logging.getLogger("browser_agent.auth")

COOKIE_NAME = "agent_session"


@dataclass
class _Session:
    expires_at: float


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def _sign(self, payload: str) -> str:
        key = settings.effective_session_secret.encode("utf-8")
        return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def create(self) -> tuple[str, int]:
        """Create a session and return ``(cookie_value, max_age_seconds)``."""
        sid = secrets.token_urlsafe(24)
        max_age = max(60, settings.session_hours * 3600)
        expires_at = time.time() + max_age
        with self._lock:
            self._sessions[sid] = _Session(expires_at=expires_at)
            self._purge_locked()
        payload = f"{sid}.{int(expires_at)}"
        return f"{payload}.{self._sign(payload)}", max_age

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            sid, expires_raw, signature = token.rsplit(".", 2)
            payload = f"{sid}.{expires_raw}"
            expected = self._sign(payload)
            if not hmac.compare_digest(signature, expected):
                return False
            expires_at = float(expires_raw)
        except (ValueError, TypeError):
            return False
        if expires_at < time.time():
            return False
        with self._lock:
            session = self._sessions.get(sid)
            return session is not None and session.expires_at >= time.time()

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        try:
            sid = token.rsplit(".", 2)[0]
        except (ValueError, IndexError):
            return
        with self._lock:
            self._sessions.pop(sid, None)

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [sid for sid, sess in self._sessions.items() if sess.expires_at < now]
        for sid in expired:
            self._sessions.pop(sid, None)


class LoginRateLimiter:
    """Very small in-memory brute-force protection for the login endpoint."""

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> int:
        with self._lock:
            entry = self._failures.get(key)
            if not entry:
                return 0
            count, locked_until = entry
            remaining = int(locked_until - time.time())
            if remaining <= 0:
                if count <= 0:
                    self._failures.pop(key, None)
                return 0
            return remaining

    def register_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            count += 1
            locked_until = 0.0
            if count >= max(1, settings.login_max_attempts):
                locked_until = time.time() + settings.login_lockout_seconds
                count = 0
            self._failures[key] = (count, locked_until)

    def register_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


sessions = SessionStore()
login_limiter = LoginRateLimiter()


def check_password(password: str) -> bool:
    """Constant-time password comparison. Fails closed when unconfigured."""
    configured = settings.app_password
    if not configured:
        return False
    return hmac.compare_digest(password or "", configured)


def client_key(request: Request | WebSocket) -> str:
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def _extract_token(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME)


def is_authenticated(request: Request) -> bool:
    return sessions.validate(_extract_token(request))


def require_auth(request: Request) -> None:
    """Dependency for JSON API routes."""
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def require_page_auth(request: Request) -> None:
    """Dependency for HTML pages; raises 401 so the UI can redirect."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"X-Redirect-To": "/login"},
        )


def is_authenticated_ws(websocket: WebSocket) -> bool:
    return sessions.validate(websocket.cookies.get(COOKIE_NAME))
