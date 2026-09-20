from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_create_task_enqueues_agent_research(client):
    r = client.post(
        "/tasks",
        json={"title": "自动调研", "kind": "readonly", "one_liner": "x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    tid = body["task"]["id"]
    enq = body.get("research_enqueue") or {}
    assert enq.get("queue") == "agent"
    assert enq.get("payload", {}).get("task_id") == tid
    assert enq.get("payload", {}).get("mode") == "auto_research"
    r2 = client.post(
        "/agent/enqueue-research",
        json={"task_id": tid, "source": "api_create"},
    )
    assert r2.status_code == 200
    assert r2.json()["item"].get("deduped") is True


def test_ledger_rejects_wrong_sender(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.agent.auth_user_open_id", lambda: "ou_auth"
    )
    created = client.post(
        "/tasks",
        json={"title": "口令", "kind": "readonly"},
    ).json()["task"]
    from app.services.tasks.store import TaskStore
    import os

    store = TaskStore(os.environ["GATEWAY_DB_PATH"])
    store.update_board_fields(
        int(created["id"]),
        board_chat_id="oc_calibration_test",
        board_message_id="om_root",
        board_thread_id="omt_board_1",
    )
    client.app.state.auth_user_open_id = "ou_auth"

    async def _no_mget(_mid):
        return None

    with patch(
        "app.services.im.fetch.resolve_user_sender_open_id",
        side_effect=_no_mget,
    ):
        r = client.post(
            "/agent/ledger",
            json={
                "bot_id": "gemi",
                "message_id": "om_cmd_1",
                "chat_id": "oc_calibration_test",
                "thread_id": "omt_board_1",
                "sender_open_id": "ou_other",
                "text": "/research",
            },
        )
    assert r.status_code == 200
    assert r.json().get("skipped") is True
    assert r.json().get("reason") == "sender_not_auth_user"


def test_ledger_accepts_and_spawns(client, monkeypatch):
    created = client.post(
        "/tasks",
        json={"title": "spawn", "kind": "readonly"},
    ).json()["task"]
    from app.services.tasks.store import TaskStore
    import os

    store = TaskStore(os.environ["GATEWAY_DB_PATH"])
    store.update_board_fields(
        int(created["id"]),
        board_chat_id="oc_calibration_test",
        board_message_id="om_root3",
        board_thread_id="omt_board_3",
    )
    client.app.state.auth_user_open_id = "ou_auth"

    async def _fake_handle(*_a, **_k):
        return {"ok": True, "accepted": True, "cmd": "research"}

    with patch("app.api.agent.handle_ledger_command", side_effect=_fake_handle):
        r = client.post(
            "/agent/ledger",
            json={
                "bot_id": "gemi",
                "message_id": "om_cmd_r",
                "chat_id": "oc_calibration_test",
                "thread_id": "omt_board_3",
                "sender_open_id": "ou_auth",
                "text": "/research 补充一下",
            },
        )
    assert r.status_code == 200
    assert r.json()["result"]["accepted"] is True


def test_auto_run_marks_and_clears_typing(client):
    created = client.post(
        "/tasks",
        json={"title": "auto typing", "kind": "readonly"},
    ).json()["task"]
    tid = int(created["id"])
    from app.services.tasks.store import TaskStore
    import os

    store = TaskStore(os.environ["GATEWAY_DB_PATH"])
    store.update_board_fields(
        tid,
        board_chat_id="oc_calibration_test",
        board_message_id="om_root_auto",
        board_thread_id="omt_board_auto",
    )

    marked: list[str] = []
    cleared: list[int] = []

    async def _fake_mark(store_arg, task_id, *, message_id):
        marked.append(message_id)
        store_arg.update_agent_runtime(
            int(task_id),
            agent_typing_message_id=message_id,
            agent_typing_reaction_id="re_auto",
            agent_typing_bot_id="",
        )
        return "re_auto"

    async def _fake_clear(store_arg, tid_):
        cleared.append(int(tid_))
        store_arg.update_agent_runtime(int(tid_), clear_typing=True)

    def _fake_begin(*_a, **_k):
        return {
            "ok": True,
            "spawned": True,
            "tid": tid,
            "mode": "auto_research",
            "run_id": "run_x",
            "proc": MagicMock(),
            "pgid": 1,
            "d": MagicMock(),
            "log_path": MagicMock(),
            "add_dirs": [],
            "argv": [],
            "interactive": False,
            "plan_fp": {},
            "research_fp": {},
            "started_at": 0,
            "source": "api_create",
            "session_id": None,
            "start_event": None,
            "clues_event": None,
        }

    def _fake_finish(_store, begun):
        return {"ok": True, "mode": "auto_research", "interactive": False}

    with (
        patch(
            "app.services.agent.runtime.mark_task_typing_reaction",
            side_effect=_fake_mark,
        ),
        patch(
            "app.services.agent.runtime.clear_task_typing_reaction",
            side_effect=_fake_clear,
        ),
        patch(
            "app.services.agent.runtime._begin_phase",
            side_effect=_fake_begin,
        ),
        patch(
            "app.services.agent.runtime._finish_phase",
            side_effect=_fake_finish,
        ),
    ):
        r = client.post(
            "/agent/run",
            json={
                "task_id": tid,
                "mode": "auto_research",
                "source": "api_create",
            },
        )
    assert r.status_code == 200
    assert marked == ["om_root_auto"]
    assert cleared == [tid]


def test_run_phase_plan_gate(db_file, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_REQUIRE_PLAN_BEFORE_EXEC", "true")
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore
    from app.services.agent.runtime import run_phase

    store = TaskStore(str(db_file))
    task = store.create_task(title="gate", kind="operational")
    out = run_phase(store, int(task["id"]), mode="exec", source="test")
    assert out.get("rejected") is True
    assert out.get("reason") == "plan_required"


def test_agent_worker_skips_when_running():
    from workers.agent.worker import AgentWorker

    client = MagicMock()
    client.get_task.return_value = {"id": 1, "agent_pgid": 999, "agent_run_id": "r1"}
    w = AgentWorker(client)
    result = w.handle(
        {
            "id": 10,
            "payload": {
                "task_id": 1,
                "mode": "auto_research",
                "source": "create",
            },
        }
    )
    assert result.status == "skip"
    client.run_agent_phase.assert_not_called()
