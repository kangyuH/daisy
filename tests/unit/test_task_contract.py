from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from app.infra.db import connect_sync
from app.services.tasks.contract import parse_contract, seed_contract
from app.services.tasks.models import TaskValidationError
from app.services.tasks.store import TaskStore

import pytest


def test_seed_keeps_empty_lists_open_and_omits_revision():
    doc = seed_contract(task_id=3, title="查口径", one_liner="上个月怎么定的")
    assert "revision" not in doc
    assert doc["schema_version"] == "0.1"
    assert doc["task_id"] == "task_3"
    assert doc["objective"]["status"] == "provisional"
    assert doc["objective"]["statement"] == "上个月怎么定的"
    assert doc["scope"]["completeness"] == "open"
    assert doc["scope"]["included"] == []
    assert doc["scope"]["excluded"] == []


def test_reject_bad_status_and_unknown_party():
    with pytest.raises(TaskValidationError):
        parse_contract(
            {"objective": {"status": "explicit", "statement": "x"}},
            task_id=1,
        )
    with pytest.raises(TaskValidationError):
        parse_contract(
            {"subject": {"authority_party_id": "p-missing"}},
            task_id=1,
        )
    with pytest.raises(TaskValidationError):
        parse_contract({"revision": 2, "title": "x"}, task_id=1)
    with pytest.raises(TaskValidationError):
        parse_contract({"schema_version": "9.9"}, task_id=1)


def test_create_persists_revision_without_writing_it_into_yaml(task_store: TaskStore):
    task = task_store.create_task(
        title="查口径",
        kind="readonly",
        one_liner="上个月口径",
        thread_id="omt_keep",
        created_from_inbound_id=7,
    )
    assert task["contract_revision"] == 1
    assert task["contract_persisted"] is True
    assert task["kind"] == "readonly"
    assert task["thread_id"] == "omt_keep"
    raw = yaml.safe_load(
        Path(task["workspace_path"], "contract.yaml").read_text(encoding="utf-8")
    )
    assert "revision" not in raw
    assert raw["task_id"] == f"task_{task['id']}"
    assert raw["origin"]["sources"][0]["ref"] == "inbound:7"
    assert raw["feedback"]["destination"]["status"] == "provisional"
    assert raw["feedback"]["destination"]["ref"] == "omt_keep"


def test_replace_bumps_revision_and_projects_title(task_store: TaskStore):
    task = task_store.create_task(title="旧标题", kind="operational", one_liner="旧陈述")
    tid = int(task["id"])
    saved = task_store.save_contract(
        tid,
        {
            "title": "新标题",
            "objective": {"status": "provisional", "statement": "新的结果承诺"},
        },
    )
    assert saved["contract_revision"] == 2
    assert saved["title"] == "新标题"
    assert saved["one_liner"] == "新的结果承诺"
    assert saved["kind"] == "operational"
    assert not (Path(saved["workspace_path"]) / "contract.yaml.tmp").exists()


class _FailingUpdateConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and "contract_revision = contract_revision + 1" in sql:
            raise sqlite3.OperationalError("update failed")
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


def test_update_failure_keeps_previous_contract(task_store: TaskStore, monkeypatch):
    task = task_store.create_task(title="旧标题", kind="readonly", one_liner="旧陈述")
    tid = int(task["id"])
    path = Path(task["workspace_path"]) / "contract.yaml"
    before = path.read_text(encoding="utf-8")
    real_connect = task_store._connect
    seen = {"n": 0}

    def connect_then_fail():
        conn = real_connect()
        seen["n"] += 1
        if seen["n"] >= 2:
            return _FailingUpdateConn(conn)
        return conn

    monkeypatch.setattr(task_store, "_connect", connect_then_fail)
    with pytest.raises(sqlite3.OperationalError, match="update failed"):
        task_store.save_contract(
            tid,
            {"title": "新标题", "objective": {"status": "confirmed", "statement": "不该落地"}},
        )
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name("contract.yaml.tmp").exists()
    got = task_store.get_task(tid)
    assert got is not None
    assert got["contract_revision"] == 1
    assert got["title"] == "旧标题"
    assert got["one_liner"] == "旧陈述"


def test_provisional_thread_does_not_overwrite_route(task_store: TaskStore):
    task = task_store.create_task(
        title="路由",
        kind="readonly",
        thread_id="omt_old",
    )
    tid = int(task["id"])
    kept = task_store.save_contract(
        tid,
        {
            "title": "路由",
            "feedback": {
                "destination": {
                    "status": "provisional",
                    "kind": "feishu_thread",
                    "ref": "omt_new",
                }
            },
        },
    )
    assert kept["thread_id"] == "omt_old"
    confirmed = task_store.save_contract(
        tid,
        {
            "title": "路由",
            "objective": {"status": "provisional", "statement": "仍在"},
            "feedback": {
                "destination": {
                    "status": "confirmed",
                    "kind": "feishu_thread",
                    "ref": "omt_new",
                }
            },
        },
    )
    assert confirmed["thread_id"] == "omt_new"
    assert confirmed["one_liner"] == "仍在"


def test_blank_statement_keeps_one_liner(task_store: TaskStore):
    task = task_store.create_task(title="标题", kind="readonly", one_liner="保留我")
    saved = task_store.save_contract(
        int(task["id"]),
        {"title": "标题", "objective": {"status": "open", "statement": None}},
    )
    assert saved["one_liner"] == "保留我"


def test_unpersisted_read_synthesizes_without_writing(task_store: TaskStore, tmp_path):
    task = task_store.create_task(title="旧任务", kind="readonly", one_liner="只有旧列")
    tid = int(task["id"])
    path = Path(task["workspace_path"]) / "contract.yaml"
    path.unlink()
    conn = connect_sync(str(tmp_path / "tasks.db"))
    try:
        conn.execute(
            "UPDATE tasks SET contract_revision = 0 WHERE id = ?",
            (tid,),
        )
        conn.commit()
    finally:
        conn.close()
    got = task_store.get_task(tid)
    assert got is not None
    assert got["contract_revision"] == 0
    assert got["contract_persisted"] is False
    assert got["contract"]["objective"]["statement"] == "只有旧列"
    assert got["contract"]["scope"]["included"] == []
    assert not path.exists()


def test_missing_file_keeps_revision(task_store: TaskStore):
    task = task_store.create_task(title="丢文件", kind="readonly")
    tid = int(task["id"])
    path = Path(task["workspace_path"]) / "contract.yaml"
    path.unlink()
    got = task_store.get_task(tid)
    assert got is not None
    assert got["contract_revision"] == 1
    assert got["contract"] is None
    assert got["contract_persisted"] is False


@pytest.fixture()
def task_store(tmp_path, monkeypatch):
    from app.infra.db import init_db_sync

    db = tmp_path / "tasks.db"
    ws = tmp_path / "ws"
    monkeypatch.setenv("GATEWAY_DB_PATH", str(db))
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(ws))
    init_db_sync(db)
    return TaskStore(str(db), workspace_root=str(ws))
