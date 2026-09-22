def test_live(client):
    r = client.get("/live")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body.get("live") is True
    assert "bots" in body
    assert "queue" not in body
    assert "db_path" not in body


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "calibration_chat_id" in body
    assert "queue" in body
    assert "db_path" in body
