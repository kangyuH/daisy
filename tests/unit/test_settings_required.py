from __future__ import annotations

import pytest

from app.core.settings import (
    SettingsError,
    get_settings,
    require_calibration_chat_id,
)
from workers.daemon import _require_dispatcher_llm_key, build_daemon_from_env


def test_calibration_chat_id_empty_without_env(monkeypatch):
    monkeypatch.delenv("CALIBRATION_CHAT_ID", raising=False)
    # Avoid pick-up from a real .env via load_dotenv setdefault: force empty.
    monkeypatch.setenv("CALIBRATION_CHAT_ID", "")
    assert get_settings().calibration_chat_id == ""
    with pytest.raises(SettingsError, match="CALIBRATION_CHAT_ID"):
        require_calibration_chat_id()


def test_require_calibration_chat_id_ok(monkeypatch):
    monkeypatch.setenv("CALIBRATION_CHAT_ID", "oc_test_calib")
    assert require_calibration_chat_id() == "oc_test_calib"


def test_default_bot_id_empty_by_default(monkeypatch):
    monkeypatch.delenv("DEFAULT_BOT_ID", raising=False)
    monkeypatch.setenv("DEFAULT_BOT_ID", "")
    assert get_settings().default_bot_id == ""


def test_dispatcher_requires_api_key_at_daemon_build(monkeypatch):
    monkeypatch.setenv("WORKER_IMPL", "dispatcher")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DISPATCHER_LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DISPATCHER_LLM_API_KEY", "")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        build_daemon_from_env()


def test_dispatcher_llm_key_helper_accepts_generic(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DISPATCHER_LLM_API_KEY", "sk-generic")
    _require_dispatcher_llm_key()


def test_simple_worker_skips_llm_key(monkeypatch):
    monkeypatch.setenv("WORKER_IMPL", "simple")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DISPATCHER_LLM_API_KEY", "")
    daemon = build_daemon_from_env()
    assert daemon.worker.__class__.__name__ == "SimpleWorker"
