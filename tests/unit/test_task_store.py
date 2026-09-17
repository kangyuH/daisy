from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.infra.db import init_db_sync
from app.services.tasks.models import (
    STATUS_DONE,
    STATUS_NOTED,
    STATUS_WAITING_EXTERNAL,
    TaskConflictError,
    TaskValidationError,
)
from app.services.tasks.store import TaskStore


@pytest.fixture()
def task_store(tmp_path, monkeypatch):
    db = tmp_path / "tasks.db"
    ws = tmp_path / "ws"
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db))
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(ws))
    init_db_sync(db)
    return TaskStore(str(db), workspace_root=str(ws))


def test_create_writes_db_and_workspace(task_store: TaskStore, tmp_path):
    task = task_store.create_task(
        title="查口径",
        kind="readonly",
        one_liner="上个月口径约定",
        chat_id="oc_1",
    )
    assert task["id"] >= 1
    assert task["status"] == STATUS_NOTED
    assert task["kind"] == "readonly"
    ws = Path(task["workspace_path"])
    assert ws.exists()
    assert (ws / "TASK.md").is_file()
    assert (ws / "timeline.jsonl").is_file()
    assert (ws / "scratch").is_dir()
    assert (ws / "artifacts").is_dir()
    lines = (ws / "timeline.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "created"


def test_chat_project_inherit_and_override(task_store: TaskStore):
    task_store.upsert_chat_project("oc_bound", "jd-patch")
    inherited = task_store.create_task(
        title="patch 跟进",
        kind="operational",
        chat_id="oc_bound",
    )
    assert inherited["project_id"] == "jd-patch"

    overridden = task_store.create_task(
        title="别的项目",
        kind="readonly",
        chat_id="oc_bound",
        project_id="nissin-delivery",
    )
    assert overridden["project_id"] == "nissin-delivery"

    unbound = task_store.create_task(
        title="无绑定",
        kind="readonly",
        chat_id="oc_other",
    )
    assert unbound["project_id"] is None


def test_followup_status_and_timeline(task_store: TaskStore):
    task = task_store.create_task(title="t1", kind="readonly")
    tid = int(task["id"])
    result = task_store.add_followup(
        tid,
        message="等对方给 CSV",
        event_type="requirement",
        status=STATUS_WAITING_EXTERNAL,
        actor="human",
    )
    assert result["task"]["status"] == STATUS_WAITING_EXTERNAL
    assert result["event"]["from_status"] == STATUS_NOTED
    assert result["event"]["to_status"] == STATUS_WAITING_EXTERNAL
    assert result["event"]["event_type"] == "requirement"

    ws = Path(result["task"]["workspace_path"])
    lines = (ws / "timeline.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    md = (ws / "TASK.md").read_text(encoding="utf-8")
    assert "等对方给 CSV" in md


def test_terminal_status_conflict_allows_note(task_store: TaskStore):
    task = task_store.create_task(title="t2", kind="operational")
    tid = int(task["id"])
    task_store.add_followup(
        tid, message="done", status=STATUS_DONE, actor="agent"
    )
    with pytest.raises(TaskConflictError):
        task_store.add_followup(
            tid, message="reopen?", status=STATUS_NOTED
        )
    note = task_store.add_followup(tid, message="补充说明一下")
    assert note["task"]["status"] == STATUS_DONE
    assert note["event"]["from_status"] is None


def test_invalid_kind(task_store: TaskStore):
    with pytest.raises(TaskValidationError):
        task_store.create_task(title="x", kind="general")


def test_bot_id_explicit_and_inherit(task_store: TaskStore):
    from app.infra.db import connect_sync
    from app.services.bots.store import STATUS_READY, BotStore

    bots = BotStore(task_store._db_path)
    bots.upsert_bot(
        bot_id="gemi",
        name="Gemi",
        app_id="cli_gemi",
        app_secret="s",
        status=STATUS_READY,
    )
    conn = connect_sync(task_store._db_path)
    try:
        conn.execute(
            "INSERT INTO bot_chats (chat_id, bot_id, bound_at) VALUES (?, ?, ?)",
            ("oc_bot_bound", "gemi", "2026-01-01T00:00:00+08:00"),
        )
        conn.commit()
    finally:
        conn.close()

    inherited = task_store.create_task(
        title="继承 bot",
        kind="readonly",
        chat_id="oc_bot_bound",
    )
    assert inherited["bot_id"] == "gemi"

    overridden = task_store.create_task(
        title="指定 bot",
        kind="readonly",
        chat_id="oc_bot_bound",
        bot_id="other",
    )
    assert overridden["bot_id"] == "other"

    bare = task_store.create_task(title="无会话", kind="readonly")
    assert bare["bot_id"] is None
