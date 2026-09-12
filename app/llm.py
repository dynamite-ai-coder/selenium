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


class AgentChatDeepSeek(ChatDeepSeek):
    """DeepSeek chat model that always runs with thinking mode disabled.

    Browser Use needs forced tool calls for its action schemas. DeepSeek
    thinking models reject a forced ``tool_choice`` with "Thinking mode does
    not support this tool_choice" unless thinking is explicitly disabled.
    Upstream only sends the disable flag for names containing "deepseek-v4",
    so a model such as ``deepseek-flash`` would fail on every step. Overriding
    this makes any configured model work with the agent.
    """

    def _supports_thinking(self) -> bool:  # noqa: D102 - upstream hook
        return True


def get_llm() -> BaseChatModel:
    """Build the chat model used by the browser agent."""
    api_key = settings.deepseek_api_key
    if not api_key:
        raise LLMConfigError(
            "DEEPSEEK_API_KEY is not configured. Add it to the environment "
            "(Render: Environment Variables, local: .env) and redeploy/restart."
        )

    register_secret(api_key)
    model = settings.deepseek_model or "deepseek-flash"
    logger.info("Configuring DeepSeek model '%s'", model)

    return AgentChatDeepSeek(
        model=model,
        api_key=api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.deepseek_temperature,
        timeout=settings.deepseek_timeout,
    )


def llm_status() -> str:
    return "configured" if settings.llm_configured else "missing"
