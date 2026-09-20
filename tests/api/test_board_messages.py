from __future__ import annotations

from unittest.mock import patch


def test_board_message_no_task_event(client):
    created = client.post(
        "/tasks",
        json={"title": "bm", "kind": "readonly"},
    ).json()["task"]
    tid = int(created["id"])
    from app.services.tasks.store import TaskStore
    import os

    store = TaskStore(os.environ["GATEWAY_DB_PATH"])
    store.update_board_fields(
        tid,
        board_chat_id="oc_calibration_test",
        board_message_id="om_root_bm",
        board_thread_id="omt_board_bm",
    )
    before = store.get_task(tid, events_limit=50)
    n_before = len(before.get("events") or [])

    async def _fake_post(self, task, text, *, idempotency_key, markdown=True):
        assert "hello board" in text
        assert idempotency_key == "run-abc-say-1"
        return {
            "ok": True,
            "message_id": "om_bm_1",
            "board_message_id": "om_root_bm",
            "board_thread_id": "omt_board_bm",
        }

    with patch(
        "app.services.tasks.board.TaskBoardSync.post_board_message",
        new=_fake_post,
    ):
        r = client.post(
            f"/tasks/{tid}/board-messages",
            json={
                "text": "hello board",
                "actor": "agent",
                "idempotency_key": "run-abc-say-1",
            },
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    after = store.get_task(tid, events_limit=50)
    assert len(after.get("events") or []) == n_before


def test_board_message_requires_idempotency_key(client):
    created = client.post(
        "/tasks",
        json={"title": "bm2", "kind": "readonly"},
    ).json()["task"]
    tid = int(created["id"])
    r = client.post(
        f"/tasks/{tid}/board-messages",
        json={"text": "hello"},
    )
    assert r.status_code == 422
