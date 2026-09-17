from __future__ import annotations

import json
from unittest.mock import MagicMock

from workers.dispatcher.state import RunState
from workers.dispatcher.tools import build_dispatcher_tools


def _tools(client, state):
    tools = {t.name: t for t in build_dispatcher_tools(client, state)}
    return tools


def test_tool_whitelist_has_no_im_write():
    tools = build_dispatcher_tools(MagicMock(), RunState(inbound_id=1))
    names = {t.name for t in tools}
    assert names == {
        "fetch_message_context",
        "get_chat_project",
        "list_open_tasks",
        "get_task",
        "create_task",
        "followup_task",
        "finalize_dispatch",
    }
    assert "respond" not in names
    assert "send" not in names
    assert "reply" not in names


def test_followup_rejects_unseen_task_id():
    client = MagicMock()
    state = RunState(inbound_id=1, chat_id="oc_a")
    tools = _tools(client, state)
    out = json.loads(tools["followup_task"].invoke({"task_id": 9, "message": "催一下"}))
    assert out["ok"] is False
    assert "seen" in out["error"]
    client.add_followup.assert_not_called()


def test_create_forces_noted_and_finalize():
    client = MagicMock()
    client.create_task.return_value = {
        "id": 11,
        "title": "查口径",
        "kind": "readonly",
        "status": "noted",
    }
    client.record_dispatch_run.return_value = {
        "inbound_id": 1,
        "decision": "create",
        "task_id": 11,
    }
    state = RunState(
        inbound_id=1,
        chat_id="oc_a",
        thread_id="omt_1",
        bot_id="gemi",
        message_id="om_1",
    )
    tools = _tools(client, state)

    created = json.loads(
        tools["create_task"].invoke(
            {
                "title": "查口径",
                "kind": "readonly",
                "one_liner": "对方问口径",
                "reason": "新需求",
            }
        )
    )
    assert created["ok"] is True
    client.create_task.assert_called_once()
    kwargs = client.create_task.call_args.kwargs
    assert kwargs["status"] == "noted"
    assert kwargs["created_from_inbound_id"] == 1
    assert kwargs["actor"] == "dispatcher"

    fin = json.loads(
        tools["finalize_dispatch"].invoke(
            {
                "decision": "create",
                "reason": "根据新@判断需建任务，已创建",
                "task_id": 11,
            }
        )
    )
    assert fin["ok"] is True
    assert state.finalized is True
    client.record_dispatch_run.assert_called_once()


def test_second_write_rejected():
    client = MagicMock()
    client.create_task.return_value = {
        "id": 1,
        "title": "a",
        "kind": "readonly",
        "status": "noted",
    }
    state = RunState(inbound_id=5, chat_id="oc_a")
    tools = _tools(client, state)
    assert json.loads(
        tools["create_task"].invoke(
            {"title": "a", "kind": "readonly", "reason": "r"}
        )
    )["ok"]
    state.seen_task_ids.add(2)
    out = json.loads(
        tools["followup_task"].invoke({"task_id": 2, "message": "x"})
    )
    assert out["ok"] is False
    assert "already performed a write" in out["error"]
