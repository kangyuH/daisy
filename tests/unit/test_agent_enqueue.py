from __future__ import annotations

from pathlib import Path

from app.services.agent.project_knowledge import (
    existing_repo_paths,
    format_project_brief,
)
from app.services.agent.prompts import RESEARCH_SYSTEM, system_prompt_for_mode
from app.services.queue.service import QUEUE_AGENT, ItemQueue


def test_research_prompt_bans_disco_traversal():
    text = system_prompt_for_mode("research")
    assert text == RESEARCH_SYSTEM
    assert "Disco tag" in text
    assert "ddfs ls" in text
    assert "禁止" in text


def test_enqueue_auto_research_idempotent(db_file):
    q = ItemQueue(str(db_file))
    import asyncio

    async def _run():
        from app.services.agent.enqueue import enqueue_auto_research

        a = await enqueue_auto_research(q, task_id=42, source="api_create")
        b = await enqueue_auto_research(q, task_id=42, source="dispatch_create")
        return a, b

    a, b = asyncio.run(_run())
    assert a["queue"] == QUEUE_AGENT
    assert a.get("deduped") is not True
    assert b.get("deduped") is True
    assert a["id"] == b["id"]
    assert a["payload"]["mode"] == "auto_research"
    assert a["payload"]["phase"] == "research"


def test_format_brief_unbound():
    assert "未绑定" in format_project_brief(None)


def test_existing_repo_paths_skips_missing(tmp_path):
    real = tmp_path / "repo"
    real.mkdir()
    project = {
        "repos": [
            {"path": str(real), "remote": "git@x"},
            {"path": str(tmp_path / "missing"), "remote": "git@y"},
            {"path": "", "remote": None},
        ]
    }
    got = existing_repo_paths(project)
    assert len(got) == 1
    assert got[0]["path"] == str(real)
