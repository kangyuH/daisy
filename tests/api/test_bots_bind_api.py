import os

from app.services.bots.store import STATUS_READY, BotStore


def test_bind_success(client):
    r = client.post("/bots/gemi/chats", json={"chat_id": "oc_biz_1"})
    assert r.status_code == 200
    assert r.json()["chats"] == ["oc_biz_1"]


def test_bind_calibration_rejected(client):
    r = client.post(
        "/bots/gemi/chats",
        json={"chat_id": "oc_calibration_test"},
    )
    assert r.status_code == 400


def test_bind_conflict_and_force(client):
    st = BotStore(os.environ["GATEWAY_DB_PATH"])
    st.upsert_bot(
        bot_id="other",
        name="Other",
        app_id="cli_other",
        app_secret="s",
        enabled=True,
        status=STATUS_READY,
    )
    r = client.post("/bots/gemi/chats", json={"chat_id": "oc_shared"})
    assert r.status_code == 200
    r = client.post("/bots/other/chats", json={"chat_id": "oc_shared", "force": False})
    assert r.status_code == 409
    r = client.post("/bots/other/chats", json={"chat_id": "oc_shared", "force": True})
    assert r.status_code == 200
    assert r.json()["binding"]["bot_id"] == "other"


def test_list_bots(client):
    r = client.get("/bots")
    assert r.status_code == 200
    ids = [b["id"] for b in r.json()["bots"]]
    assert "gemi" in ids
