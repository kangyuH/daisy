from __future__ import annotations

from app.services.agent.constants import (
    MODE_AUTO_RESEARCH,
    MODE_RESEARCH,
    normalize_mode,
)
from app.services.agent.prompts import (
    AUTO_RESEARCH_SYSTEM,
    RESEARCH_SYSTEM,
    build_user_prompt,
    system_prompt_for_mode,
)
from app.services.im.reactions import _bot_identity_args


def test_research_prompt_bans_disco_traversal():
    text = system_prompt_for_mode(MODE_RESEARCH)
    assert text == RESEARCH_SYSTEM
    assert "Disco tag" in text
    assert "ddfs ls" in text
    assert "禁止" in text


def test_auto_research_prompt_requires_three_layers():
    text = system_prompt_for_mode(MODE_AUTO_RESEARCH)
    assert text == AUTO_RESEARCH_SYSTEM
    assert "research.md" in text
    assert "诉求澄清" in text


def test_normalize_mode():
    assert normalize_mode("auto_research") == MODE_AUTO_RESEARCH
    assert normalize_mode("RESEARCH") == MODE_RESEARCH


def test_ledger_research_user_prompt_uses_rest_not_auto_goal():
    task = {
        "id": 7,
        "title": "t",
        "one_liner": "o",
        "kind": "readonly",
        "status": "waiting_human",
        "project_id": "",
        "chat_id": "",
        "workspace_path": "/tmp/x",
    }
    user = build_user_prompt(
        mode=MODE_RESEARCH,
        task=task,
        project_brief="none",
        rest="这个 tag 是什么意思？",
        source="ledger",
        run_id="abc123",
    )
    assert "这个 tag 是什么意思？" in user
    assert "完成本轮自动调研" not in user
    assert "board-messages" in user
    assert "GATEWAY_BASE_URL" in user
    assert "idempotency_key" in user


def test_auto_research_user_prompt_requires_md():
    task = {
        "id": 8,
        "title": "t",
        "one_liner": "o",
        "kind": "readonly",
        "status": "noted",
        "project_id": "",
        "chat_id": "",
        "workspace_path": "/tmp/x",
    }
    user = build_user_prompt(
        mode=MODE_AUTO_RESEARCH,
        task=task,
        project_brief="none",
        rest="",
        source="api_create",
        run_id="r1",
    )
    assert "完成本轮自动调研" in user
    assert "诉求澄清" in user


def test_reaction_default_app_omits_profile():
    assert _bot_identity_args(None) == ["--as", "bot"]
    assert _bot_identity_args("") == ["--as", "bot"]
    assert _bot_identity_args("gemi") == ["--as", "bot", "--profile", "gemi"]
