from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.bots.store import STATUS_READY, BotStore
from app.infra.db import init_db_sync


@pytest.fixture()
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("GATEWAY_DB_PATH", str(path))
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "task_workspace"))
    monkeypatch.setenv("CALIBRATION_CHAT_ID", "oc_calibration_test")
    init_db_sync(path)
    return path


@pytest.fixture()
def store(db_file):
    return BotStore(str(db_file))


@pytest.fixture()
def seeded_store(store):
    store.upsert_bot(
        bot_id="gemi",
        name="Gemi",
        app_id="cli_test_gemi",
        app_secret="secret",
        open_id="ou_bot",
        self_open_id="ou_self",
        enabled=True,
        status=STATUS_READY,
    )
    return store


@pytest.fixture()
def client(db_file, monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db_file))
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "task_workspace"))
    monkeypatch.setenv("CALIBRATION_CHAT_ID", "oc_calibration_test")
    application = create_app(testing=True)
    with TestClient(application) as c:
        # seed bot after lifespan init
        st = BotStore(str(db_file))
        st.upsert_bot(
            bot_id="gemi",
            name="Gemi",
            app_id="cli_test_gemi",
            app_secret="secret",
            open_id="ou_bot",
            self_open_id="ou_self",
            enabled=True,
            status=STATUS_READY,
        )
        yield c
