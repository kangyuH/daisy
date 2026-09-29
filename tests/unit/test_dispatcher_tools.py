from __future__ import annotations

import json
from unittest.mock import MagicMock

from workers.client import GatewayError
from workers.dispatcher.prompts import SYSTEM_PROMPT
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
    assert "replace_contract" not in names
    assert "respond" not in names
    assert "send" not in names
    assert "reply" not in names
    assert "create / followup / noop" in SYSTEM_PROMPT
    assert "contract_brief" in SYSTEM_PROMPT
    assert "contract_brief_error" in SYSTEM_PROMPT
    assert "业务对象" in SYSTEM_PROMPT
    assert "amend" not in SYSTEM_PROMPT


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


def test_get_task_includes_contract_brief_only():
    client = MagicMock()
    client.get_task.return_value = {
        "id": 4,
        "title": "查口径",
        "one_liner": "上个月怎么定的",
        "status": "waiting_human",
        "kind": "readonly",
        "contract_revision": 2,
        "contract": {
            "subject": {
                "status": "provisional",
                "object_type": "渠道收入报表",
                "object_ref": "report:channel",
                "occurrence": "2026-08",
            },
            "objective": {"status": "provisional", "statement": "上个月怎么定的"},
            "scope": {
                "completeness": "open",
                "included": [{"value": "指定报表"}],
                "excluded": [{"value": "不负责修复"}],
            },
            "acceptance": {"completeness": "open"},
            "origin": {
                "sources": [{"role": "parent_task", "kind": "task", "ref": "task_1"}]
            },
            "gaps": [{"question": "是否包含历史数据"}],
        },
    }
    state = RunState(inbound_id=1)
    tools = _tools(client, state)
    out = json.loads(tools["get_task"].invoke({"task_id": 4}))
    assert out["ok"] is True
    task = out["data"]
    assert "contract" not in task
    brief = task["contract_brief"]
    assert brief["subject_status"] == "provisional"
    assert brief["object_type"] == "渠道收入报表"
    assert brief["object_ref"] == "report:channel"
    assert brief["occurrence"] == "2026-08"
    assert brief["objective_statement"] == "上个月怎么定的"
    assert brief["objective_status"] == "provisional"
    assert brief["scope_completeness"] == "open"
    assert brief["included"] == ["指定报表"]
    assert brief["excluded"] == ["不负责修复"]
    assert brief["acceptance_completeness"] == "open"
    assert brief["gap_questions"] == ["是否包含历史数据"]
    assert brief["parent_task_refs"] == ["task_1"]


def test_list_open_tasks_hydrates_contract_brief():
    client = MagicMock()
    client.list_tasks.return_value = [
        {"id": 8, "title": "旧标题", "status": "noted", "kind": "readonly"}
    ]
    client.get_task.return_value = {
        "id": 8,
        "title": "旧标题",
        "status": "noted",
        "kind": "readonly",
        "contract": {
            "objective": {"status": "open", "statement": "待澄清的结果"},
            "scope": {"completeness": "open", "included": [], "excluded": []},
            "acceptance": {"completeness": "open"},
            "origin": {"sources": []},
            "gaps": [],
        },
    }
    state = RunState(inbound_id=3, chat_id="oc_a", thread_id="omt_1")
    tools = _tools(client, state)
    out = json.loads(
        tools["list_open_tasks"].invoke(
            {"use_thread": True, "use_chat": False, "limit": 5}
        )
    )
    assert out["ok"] is True
    assert out["data"]["tasks"][0]["contract_brief"]["objective_statement"] == "待澄清的结果"
    assert "contract_brief_error" not in out["data"]["tasks"][0]
    client.get_task.assert_called()


def test_list_open_tasks_marks_contract_brief_error():
    client = MagicMock()
    client.list_tasks.return_value = [
        {"id": 8, "title": "旧标题", "status": "noted", "kind": "readonly"}
    ]
    client.get_task.side_effect = GatewayError("timeout")
    state = RunState(inbound_id=3, chat_id="oc_a", thread_id="omt_1")
    tools = _tools(client, state)
    out = json.loads(
        tools["list_open_tasks"].invoke(
            {"use_thread": True, "use_chat": False, "limit": 5}
        )
    )
    assert out["ok"] is True
    row = out["data"]["tasks"][0]
    assert row["contract_brief"] is None
    assert row["contract_brief_error"] == "timeout"
    assert row["title"] == "旧标题"
