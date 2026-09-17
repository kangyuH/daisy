from __future__ import annotations


def test_dispatch_run_create_and_get(client):
    r = client.post(
        "/dispatcher/runs",
        json={
            "inbound_id": 42,
            "decision": "noop",
            "reason": "寒暄无需建任务",
            "evidence": {"seen_task_ids": []},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deduped"] is False
    run = body["run"]
    assert run["inbound_id"] == 42
    assert run["decision"] == "noop"
    assert run["reason"] == "寒暄无需建任务"
    assert run["evidence"]["seen_task_ids"] == []

    g = client.get("/dispatcher/runs/42")
    assert g.status_code == 200
    assert g.json()["run"]["id"] == run["id"]


def test_dispatch_run_idempotent(client):
    payload = {
        "inbound_id": 7,
        "decision": "create",
        "reason": "新建",
        "task_id": 3,
    }
    r1 = client.post("/dispatcher/runs", json=payload)
    assert r1.status_code == 200
    assert r1.json()["deduped"] is False

    r2 = client.post(
        "/dispatcher/runs",
        json={
            "inbound_id": 7,
            "decision": "noop",
            "reason": "应被忽略",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["deduped"] is True
    assert r2.json()["run"]["decision"] == "create"
    assert r2.json()["run"]["reason"] == "新建"


def test_dispatch_run_validation(client):
    bad = client.post(
        "/dispatcher/runs",
        json={"inbound_id": 1, "decision": "explode", "reason": "x"},
    )
    assert bad.status_code == 400

    missing = client.post(
        "/dispatcher/runs",
        json={"inbound_id": 1, "decision": "noop", "reason": "  "},
    )
    assert missing.status_code == 400


def test_dispatch_run_404(client):
    r = client.get("/dispatcher/runs/999999")
    assert r.status_code == 404
