from unittest.mock import AsyncMock, patch


def test_enqueue_adds_typing_reaction(client):
    with patch(
        "app.api.queue.add_typing_reaction",
        new_callable=AsyncMock,
        return_value="re_typing_1",
    ) as mock_add:
        r = client.post(
            "/queue/inbound/enqueue",
            json={
                "payload": {
                    "bot_id": "1",
                    "message_id": "om_1",
                    "bot_open_id": "ou_bot",
                },
                "idempotency_key": "react-1",
            },
        )
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["payload"]["typing_reaction_id"] == "re_typing_1"
    mock_add.assert_awaited_once()
    assert mock_add.await_args.kwargs["message_id"] == "om_1"
    assert mock_add.await_args.kwargs["profile"] == "1"


def test_enqueue_dedupe_skips_reaction(client):
    payload = {"bot_id": "1", "message_id": "om_2"}
    with patch(
        "app.api.queue.add_typing_reaction",
        new_callable=AsyncMock,
        return_value="re_x",
    ) as mock_add:
        r1 = client.post(
            "/queue/inbound/enqueue",
            json={"payload": payload, "idempotency_key": "react-dup"},
        )
        r2 = client.post(
            "/queue/inbound/enqueue",
            json={"payload": payload, "idempotency_key": "react-dup"},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["item"]["deduped"] is True
    assert mock_add.await_count == 1


def test_ack_deletes_typing_reaction(client):
    with patch(
        "app.api.queue.add_typing_reaction",
        new_callable=AsyncMock,
        return_value="re_del_1",
    ):
        enq = client.post(
            "/queue/inbound/enqueue",
            json={
                "payload": {
                    "bot_id": "1",
                    "message_id": "om_ack",
                }
            },
        )
    iid = enq.json()["item"]["id"]
    client.post("/queue/inbound/claim", json={"limit": 1, "claimed_by": "t"})

    with patch(
        "app.api.queue.delete_reaction",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as mock_del:
        r = client.post("/queue/inbound/ack", json={"id": iid})
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "done"
    mock_del.assert_awaited_once()
    assert mock_del.await_args.kwargs["message_id"] == "om_ack"
    assert mock_del.await_args.kwargs["reaction_id"] == "re_del_1"
    assert mock_del.await_args.kwargs["profile"] == "1"


def test_queue_api_flow(client):
    r = client.post(
        "/queue/inbound/enqueue",
        json={"payload": {"t": 1}, "idempotency_key": "api-1"},
    )
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "pending"

    r = client.post("/queue/inbound/claim", json={"limit": 1, "claimed_by": "test"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    iid = items[0]["id"]

    r = client.post("/queue/inbound/ack", json={"id": iid})
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "done"

    r = client.get("/queue/inbound/stats")
    assert r.status_code == 200
    assert r.json()["counts"]["done"] >= 1
