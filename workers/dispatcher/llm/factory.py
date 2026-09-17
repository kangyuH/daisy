from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel

from workers.dispatcher.llm.deepseek import build_deepseek_chat_model


class UnknownLLMProviderError(ValueError):
    pass


def build_chat_model_from_env() -> BaseChatModel:
    """Build ChatModel from env. Provider-agnostic entry for the agent."""
    provider = (
        os.environ.get("DISPATCHER_LLM_PROVIDER", "deepseek").strip().lower()
        or "deepseek"
    )
    model = os.environ.get("DISPATCHER_LLM_MODEL", "").strip()
    base_url = os.environ.get("DISPATCHER_LLM_BASE_URL", "").strip()
    generic_key = os.environ.get("DISPATCHER_LLM_API_KEY", "").strip()

    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or generic_key
        return build_deepseek_chat_model(
            api_key=api_key,
            model=model or "deepseek-v4-pro",
            base_url=base_url or "https://api.deepseek.com",
        )

    raise UnknownLLMProviderError(
        f"unknown DISPATCHER_LLM_PROVIDER={provider!r}; "
        "supported: deepseek"
    )
