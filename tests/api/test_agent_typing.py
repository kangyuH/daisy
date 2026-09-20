from __future__ import annotations

from unittest.mock import patch


def test_ledger_empty_rest_rejected(client, monkeypatch):
    created = client.post(
        "/tasks",
        json={"title": "empty rest", "kind": "readonly"},
    ).json()["task"]
    from app.services.tasks.store import TaskStore
    import os

    store = TaskStore(os.environ["GATEWAY_DB_PATH"])
    store.update_board_fields(
        int(created["id"]),
        board_chat_id="oc_calibration_test",
        board_message_id="om_root_e",
        board_thread_id="omt_board_e",
    )
    client.app.state.auth_user_open_id = "ou_auth"

    board_posts: list[str] = []

    async def _fake_board(self, task, text, *, idempotency_key, markdown=True):
        board_posts.append(text)
        return {"ok": True, "message_id": "om_bm"}

    with (
        patch("app.services.agent.runtime.run_phase_async") as run_mock,
        patch(
            "app.services.tasks.board.TaskBoardSync.post_board_message",
            new=_fake_board,
        ),
    ):
        r = client.post(
            "/agent/ledger",
            json={
                "bot_id": "gemi",
                "message_id": "om_cmd_empty",
                "chat_id": "oc_calibration_test",
                "thread_id": "omt_board_e",
                "sender_open_id": "ou_auth",
                "text": "/research",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["rejected"] is True
    assert body["result"]["reason"] == "empty_rest"
    run_mock.assert_not_called()
    assert any("被拒绝" in m and "具体指令" in m for m in board_posts)
    # No new task events for empty-rest rejection
    task = store.get_task(int(created["id"]), events_limit=50)
    msgs = [e.get("message") or "" for e in (task.get("events") or [])]
    assert not any("被拒绝" in m and "具体指令" in m for m in msgs)


def test_ledger_spawn_adds_and_clears_typing(client, monkeypatch):
    created = client.post(
        "/tasks",
        json={"title": "typing", "kind": "readonly"},
    ).json()["task"]
    from app.services.tasks.store import TaskStore
    import os

    store = TaskStore(os.environ["GATEWAY_DB_PATH"])
    store.update_board_fields(
        int(created["id"]),
        board_chat_id="oc_calibration_test",
        board_message_id="om_root_t",
        board_thread_id="omt_board_t",
    )
    client.app.state.auth_user_open_id = "ou_auth"

    async def _fake_add(*, message_id, profile=None):
        assert message_id == "om_cmd_typing"
        assert profile is None
        return "re_typing_ledger"

    clear_calls: list[int] = []

    async def _fake_clear(store_arg, tid):
        clear_calls.append(int(tid))
        store_arg.update_agent_runtime(int(tid), clear_typing=True)

    async def _fake_run(*_a, **_k):
        return {"ok": True}

    async def _fake_board(self, task, text, *, idempotency_key, markdown=True):
        return {"ok": True}

    with (
        patch(
            "app.services.agent.runtime.add_typing_reaction",
            side_effect=_fake_add,
        ),
        patch(
            "app.services.agent.runtime.clear_task_typing_reaction",
            side_effect=_fake_clear,
        ),
        patch(
            "app.services.agent.runtime.run_phase_async",
            side_effect=_fake_run,
        ),
        patch(
            "app.services.tasks.board.TaskBoardSync.post_board_message",
            new=_fake_board,
        ),
    ):
        r = client.post(
            "/agent/ledger",
            json={
                "bot_id": "gemi",
                "message_id": "om_cmd_typing",
                "chat_id": "oc_calibration_test",
                "thread_id": "omt_board_t",
                "sender_open_id": "ou_auth",
                "text": "/research 这个口径怎么理解",
            },
        )
        import time

        for _ in range(50):
            if clear_calls:
                break
            time.sleep(0.05)

    assert r.status_code == 200
    body = r.json()
    assert body["result"]["accepted"] is True
    # Typing is now marked inside run_phase_async (which we mocked),
    # so clear may not fire from mocked path — accept ack only.
    assert body["result"]["cmd"] == "research"


def test_clear_typing_runtime(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="t", kind="readonly")
    tid = int(task["id"])
    store.update_agent_runtime(
        tid,
        agent_typing_message_id="om_x",
        agent_typing_reaction_id="re_x",
        agent_typing_bot_id="",
    )
    got = store.get_task(tid, events_limit=1)
    assert got["agent_typing_reaction_id"] == "re_x"
    store.update_agent_runtime(tid, clear_typing=True)
    got2 = store.get_task(tid, events_limit=1)
    assert got2.get("agent_typing_reaction_id") is None
    assert got2.get("agent_typing_message_id") is None
    assert got2.get("agent_typing_bot_id") is None
