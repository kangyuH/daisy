from unittest.mock import AsyncMock, patch


def test_im_respond_loads_inbound_item(client):
    enq = client.post(
        "/queue/inbound/enqueue",
        json={
            "payload": {
                "bot_id": "gemi",
                "message_id": "om_1",
                "thread_id": "omt_1",
                "sender_open_id": "ou_sender",
                "bot_open_id": "ou_bot",
                "self_open_id": "ou_self",
            }
        },
    )
    assert enq.status_code == 200
    inbound_id = enq.json()["item"]["id"]

    with patch(
        "app.api.im.respond_to_message",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_respond:
        r = client.post(
            "/im/respond",
            json={
                "inbound_id": inbound_id,
                "text": "出来干活",
                "mention_open_ids": ["ou_self"],
            },
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["inbound_id"] == inbound_id
    mock_respond.assert_awaited_once()
    kwargs = mock_respond.await_args.kwargs
    assert kwargs["message_id"] == "om_1"
    assert kwargs["text"] == "出来干活"
    assert kwargs["thread_id"] == "omt_1"
    assert kwargs["sender_open_id"] == "ou_sender"
    assert kwargs["mention_open_ids"] == ["ou_self"]
    assert kwargs["profile"] == "gemi"
    assert kwargs["idempotency_key"] == f"inbound:{inbound_id}"
