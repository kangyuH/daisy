from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.agent.reconcile import (
    clear_dead_agent_lock,
    reconcile_agent_locks,
)
from app.services.tasks.models import TaskConflictError
from app.services.tasks.store import TaskStore


def test_create_task_dedupes_same_inbound(db_file):
    store = TaskStore(str(db_file))
    a = store.create_task(
        title="t1", kind="readonly", created_from_inbound_id=99
    )
    b = store.create_task(
        title="t2", kind="readonly", created_from_inbound_id=99
    )
    assert a["deduped"] is False
    assert b["deduped"] is True
    assert a["id"] == b["id"]


def test_reconcile_releases_dead_lock(db_file):
    store = TaskStore(str(db_file))
    task = store.create_task(title="lock", kind="readonly")
    tid = int(task["id"])
    store.try_claim_agent_run(tid, mode="auto_research", run_id="run1")
    store.attach_agent_pgid(
        tid, run_id="run1", pgid=12345, pid=12345, proc_start="1"
    )
    with patch(
        "app.services.agent.reconcile.process_identity_matches",
        return_value="dead",
    ):
        summary = reconcile_agent_locks(store, kill_orphans=False)
    assert tid in summary["released"]
    fresh = store.get_task(tid, events_limit=1)
    assert fresh.get("agent_run_id") is None


def test_reconcile_keeps_alive_lock(db_file):
    store = TaskStore(str(db_file))
    task = store.create_task(title="live", kind="readonly")
    tid = int(task["id"])
    store.try_claim_agent_run(tid, mode="auto_research", run_id="run2")
    store.attach_agent_pgid(
        tid, run_id="run2", pgid=99, pid=99, proc_start="42"
    )
    with patch(
        "app.services.agent.reconcile.process_identity_matches",
        return_value="alive",
    ):
        summary = reconcile_agent_locks(store, kill_orphans=False)
    assert tid in summary["kept_live"]
    fresh = store.get_task(tid, events_limit=1)
    assert fresh.get("agent_run_id") == "run2"


def test_clear_dead_agent_lock(db_file):
    store = TaskStore(str(db_file))
    task = store.create_task(title="dead", kind="readonly")
    tid = int(task["id"])
    store.try_claim_agent_run(tid, mode="research", run_id="rx")
    store.attach_agent_pgid(tid, run_id="rx", pgid=1, pid=1, proc_start="9")
    task = store.get_task(tid, events_limit=1)
    with patch(
        "app.services.agent.reconcile.process_identity_matches",
        return_value="dead",
    ):
        assert clear_dead_agent_lock(store, task) is True
    assert store.get_task(tid, events_limit=1).get("agent_run_id") is None


def test_old_run_release_does_not_clear_new_run(db_file):
    store = TaskStore(str(db_file))
    tid = int(store.create_task(title="run fencing", kind="readonly")["id"])
    store.try_claim_agent_run(tid, mode="research", run_id="old")
    store.release_agent_run(tid, run_id="old")
    store.try_claim_agent_run(tid, mode="research", run_id="new")

    with pytest.raises(TaskConflictError):
        store.release_agent_run(tid, run_id="old")

    current = store.get_task(tid, events_limit=1)
    assert current is not None
    assert current["agent_run_id"] == "new"


def test_reconcile_keeps_lock_when_proc_start_missing(db_file):
    store = TaskStore(str(db_file))
    tid = int(store.create_task(title="unknown identity", kind="readonly")["id"])
    store.try_claim_agent_run(tid, mode="research", run_id="unknown-run")
    store.attach_agent_pgid(
        tid, run_id="unknown-run", pgid=12345, pid=12345, proc_start=None
    )

    with patch(
        "app.services.agent.reconcile.kill_process_group"
    ) as mock_kill:
        summary = reconcile_agent_locks(store, kill_orphans=True)

    assert tid in summary["unknown"]
    assert tid not in summary["released"]
    mock_kill.assert_not_called()
    current = store.get_task(tid, events_limit=1)
    assert current is not None
    assert current["agent_run_id"] == "unknown-run"
