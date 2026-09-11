"""Shared helpers: logging, redaction and safe file handling."""

from __future__ import annotations

import logging
import re
import unicodedata
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")
_UNSAFE_PATTERNS = (
    re.compile(
        r"\b(credential\s*stuffing|brute\s*-?\s*force|crack(?:ing)?\s+(?:the\s+)?password|"
        r"bypass\s+(?:the\s+)?captcha|ddos|denial\s+of\s+service|"
        r"steal\s+(?:cookies|credentials|tokens|session)|keylog(?:ger)?|ransomware)\b",
        re.IGNORECASE,
    ),
)
_SECRET_PATTERNS = (
    re.compile(r"rnd_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
)

# Values that must never appear in logs. Populated lazily from settings.
_SECRET_VALUES: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a runtime secret so that log filters can redact it."""
    if value and len(value) >= 6:
        _SECRET_VALUES.add(value)


def redact(text: str) -> str:
    """Replace secrets and typical API-key patterns in ``text``."""
    if not text:
        return text
    result = str(text)
    for secret in _SECRET_VALUES:
        if secret and secret in result:
            result = result.replace(secret, "***REDACTED***")
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("***REDACTED***", result)
    return result


class RedactingFilter(logging.Filter):
    """Logging filter that removes secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
            redacted = redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


def setup_logging() -> logging.Logger:
    """Configure console + rotating file logging with secret redaction."""
    logs_dir = settings.logs_path
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if getattr(root, "_agent_configured", False):
        return logging.getLogger("browser_agent")
    root._agent_configured = True  # type: ignore[attr-defined]

    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    redactor = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            logs_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    except OSError:
        pass

    # Browser Use is chatty; keep its console output at WARNING but keep our
    # own application logs at INFO.
    logging.getLogger("browser_use").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    register_secret(settings.deepseek_api_key)
    register_secret(settings.app_password)
    register_secret(settings.session_secret)

    return logging.getLogger("browser_agent")


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Return a safe basename derived from a user supplied filename."""
    name = unicodedata.normalize("NFKC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = _FILENAME_SAFE.sub("_", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > 120:
        stem, _, suffix = name.rpartition(".")
        if stem and len(suffix) <= 10:
            name = stem[: 120 - len(suffix) - 1] + "." + suffix
        else:
            name = name[:120]
    return name


def safe_join(base: Path, name: str) -> Path:
    """Join ``base`` with a user supplied name, preventing traversal."""
    candidate = (base / sanitize_filename(name)).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ValueError("Path traversal detected")
    return candidate


def truncate(text: str, limit: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def safe_error_message(exc: BaseException) -> str:
    """Return a short, secret-free error message suitable for the UI."""
    text = redact(str(exc) or exc.__class__.__name__)
    text = re.sub(r"\s+", " ", text).strip()
    return truncate(text, 300)


def is_unsafe_task(text: str) -> bool:
    """Return True for clearly abusive requests the agent must refuse."""
    return any(pattern.search(text or "") for pattern in _UNSAFE_PATTERNS)
