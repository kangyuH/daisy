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
