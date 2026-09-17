from __future__ import annotations

import pytest

from workers.dispatcher.llm.factory import (
    UnknownLLMProviderError,
    build_chat_model_from_env,
)


def test_factory_unknown_provider(monkeypatch):
    monkeypatch.setenv("DISPATCHER_LLM_PROVIDER", "not-a-vendor")
    with pytest.raises(UnknownLLMProviderError):
        build_chat_model_from_env()


def test_factory_deepseek_builds_chat_openai(monkeypatch):
    monkeypatch.setenv("DISPATCHER_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DISPATCHER_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DISPATCHER_LLM_BASE_URL", "https://api.deepseek.com")
    model = build_chat_model_from_env()
    assert model.model_name == "deepseek-v4-pro" or getattr(
        model, "model", None
    ) == "deepseek-v4-pro"


def test_factory_deepseek_requires_key(monkeypatch):
    monkeypatch.setenv("DISPATCHER_LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DISPATCHER_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError):
        build_chat_model_from_env()
