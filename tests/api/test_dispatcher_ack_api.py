from __future__ import annotations

from unittest.mock import AsyncMock, patch


def _enable_ack(client) -> None:
    client.app.state.dispatch_ack_enabled = True


def _enqueue_inbound(client, **payload_extra):
    payload = {
        "bot_id": "gemi",
        "message_id": "om_ack_1",
        "thread_id": "omt_ack_1",
        "sender_open_id": "ou_sender",
        "bot_open_id": "ou_bot",
        "self_open_id": "ou_self",
    }
    payload.update(payload_extra)
    enq = client.post("/queue/inbound/enqueue", json={"payload": payload})
    assert enq.status_code == 200
    return enq.json()["item"]["id"]


def _create_task(client, title: str = "查上个月口径") -> int:
    r = client.post(
        "/tasks",
        json={"title": title, "kind": "readonly", "actor": "test"},
    )
    assert r.status_code == 200
    return int(r.json()["task"]["id"])


def test_dispatch_create_sends_ack(client):
    _enable_ack(client)
    inbound_id = _enqueue_inbound(client)
    task_id = _create_task(client, "查上个月口径")

    with patch(
        "app.services.dispatcher.ack.respond_to_message",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_respond:
        r = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "create",
                "reason": "新事项",
                "task_id": task_id,
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deduped"] is False
    assert body["reply_sync"]["ok"] is True
    assert body["reply_sync"]["text"] == "收到，已创建任务「查上个月口径」。"
    assert "#" not in body["reply_sync"]["text"]
    assert str(task_id) not in body["reply_sync"]["text"]

    mock_respond.assert_awaited_once()
    kwargs = mock_respond.await_args.kwargs
    assert kwargs["message_id"] == "om_ack_1"
    assert kwargs["text"] == "收到，已创建任务「查上个月口径」。"
    assert kwargs["profile"] == "gemi"
    assert kwargs["thread_id"] == "omt_ack_1"
    assert kwargs["sender_open_id"] == "ou_sender"
    assert kwargs["idempotency_key"] == f"ack:{inbound_id}"


def test_dispatch_followup_sends_ack(client):
    _enable_ack(client)
    inbound_id = _enqueue_inbound(client)
    task_id = _create_task(client, "补数口径")

    with patch(
        "app.services.dispatcher.ack.respond_to_message",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_respond:
        r = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "followup",
                "reason": "同事项跟进",
                "task_id": task_id,
            },
        )

    assert r.status_code == 200
    assert r.json()["reply_sync"]["text"] == "收到，已跟进任务「补数口径」。"
    mock_respond.assert_awaited_once()
    assert mock_respond.await_args.kwargs["text"] == "收到，已跟进任务「补数口径」。"


def test_dispatch_noop_does_not_reply(client):
    _enable_ack(client)
    inbound_id = _enqueue_inbound(client)

    with patch(
        "app.services.dispatcher.ack.respond_to_message",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_respond:
        r = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "noop",
                "reason": "寒暄",
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["reply_sync"]["skipped"] is True
    assert body["reply_sync"]["reason"] == "decision_not_ackable"
    mock_respond.assert_not_awaited()


def test_dispatch_deduped_does_not_reply(client):
    _enable_ack(client)
    inbound_id = _enqueue_inbound(client)
    task_id = _create_task(client)

    with patch(
        "app.services.dispatcher.ack.respond_to_message",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_respond:
        r1 = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "create",
                "reason": "新建",
                "task_id": task_id,
            },
        )
        assert r1.status_code == 200
        assert r1.json()["deduped"] is False
        assert mock_respond.await_count == 1

        r2 = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "followup",
                "reason": "应被忽略",
                "task_id": task_id,
            },
        )

    assert r2.status_code == 200
    assert r2.json()["deduped"] is True
    assert r2.json()["reply_sync"] is None
    assert mock_respond.await_count == 1


def test_dispatch_ack_disabled_in_testing_by_default(client):
    inbound_id = _enqueue_inbound(client)
    task_id = _create_task(client)

    with patch(
        "app.services.dispatcher.ack.respond_to_message",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_respond:
        r = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "create",
                "reason": "新建",
                "task_id": task_id,
            },
        )

    assert r.status_code == 200
    assert r.json()["reply_sync"]["reason"] == "disabled"
    mock_respond.assert_not_awaited()


def test_dispatch_ack_feishu_failure_still_200(client):
    _enable_ack(client)
    inbound_id = _enqueue_inbound(client)
    task_id = _create_task(client)

    with patch(
        "app.services.dispatcher.ack.respond_to_message",
        new_callable=AsyncMock,
        side_effect=RuntimeError("lark down"),
    ):
        r = client.post(
            "/dispatcher/runs",
            json={
                "inbound_id": inbound_id,
                "decision": "create",
                "reason": "新建",
                "task_id": task_id,
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run"]["decision"] == "create"
    assert body["reply_sync"]["ok"] is False
    assert "lark down" in body["reply_sync"]["error"]
