from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel


def build_deepseek_chat_model(
    *,
    api_key: str,
    model: str = "deepseek-v4-pro",
    base_url: str = "https://api.deepseek.com",
    temperature: float = 0.2,
) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    key = (api_key or "").strip()
    if not key:
        raise ValueError("DeepSeek API key is required (DEEPSEEK_API_KEY)")
    return ChatOpenAI(
        model=(model or "deepseek-v4-pro").strip() or "deepseek-v4-pro",
        api_key=key,
        base_url=(base_url or "https://api.deepseek.com").rstrip("/"),
        temperature=temperature,
    )
