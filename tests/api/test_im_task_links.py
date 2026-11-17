from unittest.mock import AsyncMock, patch

import pytest

from app.infra.db import sqlite3
from app.infra.lark.cli import LarkCliError


def _task(client, title, status="noted"):
    response = client.post(
        "/tasks",
        json={"title": title, "kind": "readonly", "chat_id": "oc_business", "status": status},
    )
    assert response.status_code == 200
    return response.json()["task"]["id"]


def _dispatch_inbound(client, message_id, task_id):
    entry = client.post(
        "/queue/inbound/enqueue",
        json={"payload": {"message_id": message_id, "chat_id": "oc_business"}},
    ).json()["item"]
    response = client.post(
        "/dispatcher/runs",
        json={
            "inbound_id": entry["id"],
            "decision": "followup",
            "reason": "补充原需求",
            "task_id": task_id,
        },
    )
    assert response.status_code == 200
    return entry["id"]


def _message(message_id, chat_id="oc_business"):
    return {"message_id": message_id, "chat_id": chat_id, "thread_id": "omt_shared"}


@pytest.mark.parametrize("quoted_message_id", ["om_requirement", "om_delivery"])
def test_context_labels_history_and_resolves_direct_reply(client, quoted_message_id):
    requirement_task = _task(client, "需求任务")
    delivery_task = _task(client, "交付任务", status="done")
    _dispatch_inbound(client, "om_requirement", requirement_task)

    inbound_id = _dispatch_inbound(client, "om_original_delivery_request", delivery_task)
    with patch(
        "app.api.im.respond_to_message",
        new=AsyncMock(return_value={"data": {"message_id": "om_delivery"}}),
    ) as respond:
        response = client.post(
            "/im/respond", json={"inbound_id": inbound_id, "text": "这是交付文件"}
        )
    assert response.status_code == 200
    assert response.json()["task_link"] == {
        "task_id": delivery_task,
        "message_id": "om_delivery",
        "status": "recorded",
    }
    assert respond.await_args.kwargs["idempotency_key"] == f"inbound:{inbound_id}"

    # The other task shares the thread. The direct target can also sit outside the window.
    context = {"anchor": _message("om_question"), "messages": [_message("om_requirement")]}
    with (
        patch("app.api.im.fetch_context", new=AsyncMock(return_value=context)),
        patch(
            "app.services.im.fetch.mget_messages",
            new=AsyncMock(return_value=[_message("om_delivery")]),
        ) as mget,
    ):
        response = client.post(
            "/im/messages/context",
            json={"message_id": "om_question", "reply_to_message_id": quoted_message_id},
        )
    assert response.status_code == 200
    data = response.json()
    requirement_ref = {"task_id": requirement_task, "status": "noted", "source": "inbound"}
    delivery_ref = {"task_id": delivery_task, "status": "done", "source": "outbound"}
    assert data["anchor"]["task_refs"] == []
    assert data["messages"][0]["task_refs"] == [requirement_ref]
    assert data["reply_target"]["message_id"] == quoted_message_id
    if quoted_message_id == "om_delivery":
        mget.assert_awaited_once_with(["om_delivery"])
        assert data["reply_target"]["task_refs"] == [delivery_ref]
    else:
        mget.assert_not_awaited()
        assert data["reply_target"]["task_refs"] == [requirement_ref]


def test_context_excludes_a_direct_reply_from_another_chat(client):
    context = {"anchor": _message("om_question"), "messages": []}
    with (
        patch("app.api.im.fetch_context", new=AsyncMock(return_value=context)),
        patch(
            "app.services.im.fetch.mget_messages",
            new=AsyncMock(return_value=[_message("om_other_chat", chat_id="oc_other")]),
        ),
    ):
        response = client.post(
            "/im/messages/context",
            json={"message_id": "om_question", "reply_to_message_id": "om_other_chat"},
        )
    assert response.status_code == 200
    assert response.json()["reply_target"] is None


@pytest.mark.parametrize(
    ("endpoint", "sender", "target"),
    [
        ("send", "send_message", {"chat_id": "oc_business"}),
        ("reply", "reply_message", {"message_id": "om_original"}),
    ],
)
def test_explicit_task_link_is_idempotent_and_preserves_conflicts(client, endpoint, sender, target):
    first = _task(client, "原任务")
    second = _task(client, "另一个任务")
    body = {**target, "text": "交付", "task_id": first, "idempotency_key": "delivery:1"}
    result = {"message_id": "om_delivery"}
    with patch(f"app.api.im.{sender}", new=AsyncMock(return_value=result)) as send:
        for _ in range(2):
            response = client.post(f"/im/{endpoint}", json=body)
            assert response.json()["task_link"]["status"] == "recorded"
        response = client.post(f"/im/{endpoint}", json={**body, "task_id": second})
    assert response.json()["task_link"]["reason"] == "message_link_conflict"
    refs = client.app.state.message_task_store.find_refs({"om_delivery"})
    assert refs["om_delivery"] == [{"task_id": first, "status": "noted", "source": "outbound"}]
    assert send.await_args_list[0].kwargs["idempotency_key"] == "delivery:1"
    assert send.await_args_list[1].kwargs["idempotency_key"] == "delivery:1"


def test_task_send_validates_before_contacting_feishu(client):
    task_id = _task(client, "已有任务")
    target = {"chat_id": "oc_business", "text": "交付"}
    with patch("app.api.im.send_message", new_callable=AsyncMock) as send:
        missing = client.post(
            "/im/send", json={**target, "task_id": task_id + 100, "idempotency_key": "missing"}
        )
        missing_key = client.post("/im/send", json={**target, "task_id": task_id})
    assert missing.status_code == 404
    assert missing_key.status_code == 400
    send.assert_not_awaited()


def test_failed_send_creates_no_link(client):
    body = {
        "chat_id": "oc_business",
        "task_id": _task(client, "交付任务"),
        "idempotency_key": "delivery:failed",
    }
    error = LarkCliError("failed", returncode=1, stdout="", stderr="")
    with (
        patch("app.api.im.send_message", new=AsyncMock(side_effect=error)),
        patch.object(client.app.state.message_task_store, "record_outbound") as record,
    ):
        response = client.post("/im/send", json=body)
    assert response.status_code == 502
    record.assert_not_called()


def test_storage_failure_allows_retry_with_same_key(client):
    body = {
        "chat_id": "oc_business",
        "task_id": _task(client, "交付任务"),
        "idempotency_key": "delivery:retry",
    }
    with patch(
        "app.api.im.send_message", new=AsyncMock(return_value={"message_id": "om_delivery"})
    ) as send:
        with patch.object(
            client.app.state.message_task_store, "record_outbound",
            side_effect=sqlite3.OperationalError("simulated storage failure"),
        ):
            response = client.post("/im/send", json=body)
        assert response.status_code == 200
        assert response.json()["task_link"] == {
            "status": "unrecorded",
            "reason": "storage_error",
            "task_id": body["task_id"],
            "message_id": "om_delivery",
        }
        assert client.app.state.message_task_store.find_refs({"om_delivery"}) == {}
        retry = client.post("/im/send", json=body)
    assert retry.json()["task_link"]["status"] == "recorded"
    assert {call.kwargs["idempotency_key"] for call in send.await_args_list} == {"delivery:retry"}
