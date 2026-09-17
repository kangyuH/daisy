from __future__ import annotations

import pytest

from app.infra.db import init_db_sync
from app.services.im.outbound import extract_message_id, extract_thread_id
from app.services.tasks.board import TaskBoardSync
from app.services.tasks.store import TaskStore


def test_extract_message_and_thread_id():
    assert extract_message_id({"data": {"message_id": "om_1"}}) == "om_1"
    assert extract_message_id({"data": {"message": {"message_id": "om_2"}}}) == "om_2"
    assert extract_thread_id({"data": {"thread_id": "omt_1"}}) == "omt_1"
    assert extract_thread_id({"data": {"message": {"threadId": "omt_2"}}}) == "omt_2"
    assert extract_message_id({}) is None


@pytest.fixture()
def task_store(tmp_path, monkeypatch):
    db = tmp_path / "tasks.db"
    ws = tmp_path / "ws"
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db))
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(ws))
    monkeypatch.setenv("CALIBRATION_CHAT_ID", "oc_calib")
    init_db_sync(db)
    return TaskStore(str(db), workspace_root=str(ws))


@pytest.mark.asyncio
async def test_ensure_board_success(task_store: TaskStore, monkeypatch):
    task = task_store.create_task(title="t", kind="readonly")
    calls = []

    async def fake_send(**kwargs):
        calls.append(("send", kwargs))
        return {"data": {"message_id": "om_root"}}

    async def fake_reply(**kwargs):
        calls.append(("reply", kwargs))
        return {"data": {"message_id": "om_open", "thread_id": "omt_board"}}

    monkeypatch.setattr("app.services.tasks.board.send_message", fake_send)
    monkeypatch.setattr("app.services.tasks.board.reply_message", fake_reply)

    board = TaskBoardSync(
        task_store,
        chat_id="oc_calib",
        enabled=True,
        mention_user_open_id="ou_user",
    )
    result = await board.ensure_board(task)
    assert result["ok"] is True
    assert result["board_message_id"] == "om_root"
    assert result["board_thread_id"] == "omt_board"
    assert calls[0][0] == "send"
    assert calls[0][1].get("as_user") is False
    assert calls[0][1].get("markdown") is True
    assert calls[0][1].get("mention_open_ids") in (None, [])
    assert calls[1][0] == "reply"
    assert calls[1][1]["reply_in_thread"] is True
    assert calls[1][1].get("as_user") is False
    assert calls[1][1].get("markdown") is True
    assert calls[1][1].get("mention_open_ids") == ["ou_user"]

    fresh = task_store.get_task(int(task["id"]))
    assert fresh["board_message_id"] == "om_root"
    assert fresh["board_thread_id"] == "omt_board"
    assert fresh["board_sync_error"] is None


@pytest.mark.asyncio
async def test_ensure_board_failure_keeps_task(task_store: TaskStore, monkeypatch):
    task = task_store.create_task(title="t", kind="readonly")

    async def boom(**kwargs):
        raise RuntimeError("lark down")

    monkeypatch.setattr("app.services.tasks.board.send_message", boom)
    board = TaskBoardSync(task_store, chat_id="oc_calib", enabled=True)
    result = await board.ensure_board(task)
    assert result["ok"] is False
    assert "lark down" in result["error"]
    fresh = task_store.get_task(int(task["id"]))
    assert fresh is not None
    assert fresh["board_message_id"] is None
    assert "lark down" in (fresh["board_sync_error"] or "")


@pytest.mark.asyncio
async def test_followup_backfills_board(task_store: TaskStore, monkeypatch):
    task = task_store.create_task(title="t", kind="operational")
    fu = task_store.add_followup(int(task["id"]), message="催一下", status="waiting_human")
    calls = []

    async def fake_send(**kwargs):
        calls.append(("send", kwargs))
        return {"data": {"message_id": "om_root"}}

    async def fake_reply(**kwargs):
        calls.append(("reply", kwargs))
        if len(calls) == 2:
            return {"data": {"message_id": "om_open", "thread_id": "omt_x"}}
        return {"data": {"message_id": "om_fu", "thread_id": "omt_x"}}

    monkeypatch.setattr("app.services.tasks.board.send_message", fake_send)
    monkeypatch.setattr("app.services.tasks.board.reply_message", fake_reply)

    board = TaskBoardSync(
        task_store,
        chat_id="oc_calib",
        enabled=True,
        mention_user_open_id="ou_user",
    )
    result = await board.sync_followup(fu["task"], fu["event"])
    assert result["ok"] is True
    assert [c[0] for c in calls] == ["send", "reply", "reply"]
    assert calls[1][1].get("mention_open_ids") == ["ou_user"]
    assert calls[2][1].get("mention_open_ids") in (None, [])
    fresh = task_store.get_task(int(task["id"]))
    assert fresh["board_message_id"] == "om_root"
    assert fresh["status"] == "waiting_human"


@pytest.mark.asyncio
async def test_ensure_board_skips_mention_without_user(task_store: TaskStore, monkeypatch):
    task = task_store.create_task(title="t", kind="readonly")
    calls = []

    async def fake_send(**kwargs):
        calls.append(("send", kwargs))
        return {"data": {"message_id": "om_root"}}

    async def fake_reply(**kwargs):
        calls.append(("reply", kwargs))
        return {"data": {"message_id": "om_open", "thread_id": "omt_board"}}

    monkeypatch.setattr("app.services.tasks.board.send_message", fake_send)
    monkeypatch.setattr("app.services.tasks.board.reply_message", fake_reply)

    board = TaskBoardSync(task_store, chat_id="oc_calib", enabled=True)
    result = await board.ensure_board(task)
    assert result["ok"] is True
    assert calls[1][1].get("mention_open_ids") == []
