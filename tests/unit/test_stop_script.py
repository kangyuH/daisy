from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.infra.db import init_db_sync
from app.services.tasks.store import TaskStore

ROOT = Path(__file__).resolve().parents[2]


def _run_stop(db_path: Path, pid_dir: Path, workspace: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GATEWAY_DB_PATH"] = str(db_path)
    env["GATEWAY_TASK_WORKSPACE"] = str(workspace)
    env["PID_DIR"] = str(pid_dir)
    return subprocess.run(
        ["bash", str(ROOT / "stop.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_stop_script_clean_database_exits_zero(tmp_path):
    db_path = tmp_path / "gateway.db"
    pid_dir = tmp_path / "pids"
    workspace = tmp_path / "ws"
    pid_dir.mkdir()
    init_db_sync(db_path)

    result = _run_stop(db_path, pid_dir, workspace)

    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert output.index("[stop] agent reap") < output.index("[stop] gateway:")
    assert output.rstrip().endswith("[stop] done")


def test_stop_script_unknown_lock_still_stops_gateway(tmp_path):
    db_path = tmp_path / "gateway.db"
    pid_dir = tmp_path / "pids"
    workspace = tmp_path / "ws"
    pid_dir.mkdir()
    init_db_sync(db_path)
    store = TaskStore(str(db_path), workspace_root=str(workspace))
    task_id = int(store.create_task(title="unknown", kind="readonly")["id"])
    store.try_claim_agent_run(task_id, mode="research", run_id="unknown-run")
    store.attach_agent_pgid(
        task_id, run_id="unknown-run", pgid=12345, pid=12345, proc_start=None
    )

    result = _run_stop(db_path, pid_dir, workspace)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert output.index("[stop] agent reap") < output.index("[stop] gateway:")
    assert "[stop] completed with errors" in output
    fresh = store.get_task(task_id, events_limit=1)
    assert fresh is not None
    assert fresh.get("agent_run_id") == "unknown-run"
