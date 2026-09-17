from __future__ import annotations

from pathlib import Path


def test_create_and_get_task(client, tmp_path):
    r = client.post(
        "/tasks",
        json={
            "title": "查根因",
            "kind": "readonly",
            "one_liner": "xxx 异常",
            "bot_id": "gemi",
            "chat_id": "oc_a",
            "thread_id": "omt_1",
        },
    )
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["status"] == "noted"
    assert task["kind"] == "readonly"
    assert task["bot_id"] == "gemi"
    assert Path(task["workspace_path"]).exists()

    gid = client.get(f"/tasks/{task['id']}")
    assert gid.status_code == 200
    got = gid.json()["task"]
    assert got["title"] == "查根因"
    assert len(got["events"]) >= 1
    assert got["events"][0]["event_type"] == "created"


def test_chat_project_bind_inherit(client):
    put = client.put(
        "/chat-projects/oc_patch",
        json={"project_id": "jd-patch", "note": "patch 群"},
    )
    assert put.status_code == 200
    assert put.json()["item"]["project_id"] == "jd-patch"

    r = client.post(
        "/tasks",
        json={"title": "修数", "kind": "operational", "chat_id": "oc_patch"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["project_id"] == "jd-patch"

    r2 = client.post(
        "/tasks",
        json={
            "title": "覆盖",
            "kind": "readonly",
            "chat_id": "oc_patch",
            "project_id": "daliver",
        },
    )
    assert r2.json()["task"]["project_id"] == "daliver"


def test_followup_and_terminal_409(client):
    created = client.post(
        "/tasks",
        json={"title": "催一下", "kind": "operational"},
    ).json()["task"]
    tid = created["id"]

    fu = client.post(
        f"/tasks/{tid}/followups",
        json={
            "message": "需求方催了一下",
            "event_type": "note",
            "status": "waiting_human",
            "actor": "human",
        },
    )
    assert fu.status_code == 200
    body = fu.json()
    assert body["task"]["status"] == "waiting_human"
    assert body["event"]["from_status"] == "noted"
    assert body["event"]["to_status"] == "waiting_human"

    done = client.post(
        f"/tasks/{tid}/followups",
        json={"message": "已反馈", "status": "done"},
    )
    assert done.status_code == 200
    assert done.json()["task"]["status"] == "done"
    assert done.json()["task"]["closed_at"]

    bad = client.post(
        f"/tasks/{tid}/followups",
        json={"message": "想重开", "status": "noted"},
    )
    assert bad.status_code == 409

    note = client.post(
        f"/tasks/{tid}/followups",
        json={"message": "事后备注"},
    )
    assert note.status_code == 200
    assert note.json()["task"]["status"] == "done"


def test_invalid_kind_400(client):
    r = client.post("/tasks", json={"title": "x", "kind": "general"})
    assert r.status_code == 400


def test_invalid_status_400(client):
    created = client.post(
        "/tasks", json={"title": "y", "kind": "readonly"}
    ).json()["task"]
    r = client.post(
        f"/tasks/{created['id']}/followups",
        json={"message": "bad", "status": "open"},
    )
    assert r.status_code == 400


def test_list_tasks_filter(client):
    client.post(
        "/tasks",
        json={
            "title": "a",
            "kind": "readonly",
            "chat_id": "oc_f",
            "project_id": "jd-patch",
        },
    )
    client.post(
        "/tasks",
        json={"title": "b", "kind": "operational", "chat_id": "oc_f"},
    )
    r = client.get("/tasks", params={"chat_id": "oc_f", "kind": "readonly"})
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["title"] == "a"


def test_chat_projects_list_delete(client):
    client.put("/chat-projects/oc_x", json={"project_id": "pm-redmine"})
    listed = client.get("/chat-projects")
    assert listed.status_code == 200
    assert any(i["chat_id"] == "oc_x" for i in listed.json()["items"])

    deleted = client.delete("/chat-projects/oc_x")
    assert deleted.status_code == 200
    missing = client.get("/chat-projects/oc_x")
    assert missing.status_code == 404
