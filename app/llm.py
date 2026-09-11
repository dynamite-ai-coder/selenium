"""LLM provider abstraction.

The browser agent only depends on :func:`get_llm`, which returns a
``browser_use`` chat model. DeepSeek is the default (and currently only)
provider, but swapping providers later only requires changing this file.

Environment variables:
    DEEPSEEK_API_KEY  - required at task time
    DEEPSEEK_MODEL    - model id (default: deepseek-chat)
    DEEPSEEK_BASE_URL - OpenAI-compatible endpoint
"""

from __future__ import annotations

import logging

from browser_use import ChatDeepSeek
from browser_use.llm.base import BaseChatModel

from app.config import settings
from app.utils import register_secret

logger = logging.getLogger("browser_agent.llm")


class LLMConfigError(RuntimeError):
    """Raised when the LLM cannot be configured (e.g. missing API key)."""


def get_llm() -> BaseChatModel:
    """Build the chat model used by the browser agent."""
    api_key = settings.deepseek_api_key
    if not api_key:
        raise LLMConfigError(
            "DEEPSEEK_API_KEY is not configured. Add it to the environment "
            "(Render: Environment Variables, local: .env) and redeploy/restart."
        )

    register_secret(api_key)
    model = settings.deepseek_model or "deepseek-v4-flash"
    logger.info("Configuring DeepSeek model '%s'", model)

    return ChatDeepSeek(
        model=model,
        api_key=api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.deepseek_temperature,
        timeout=settings.deepseek_timeout,
    )


def llm_status() -> str:
    return "configured" if settings.llm_configured else "missing"
