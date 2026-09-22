from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.agent.constants import MODE_AUTO_RESEARCH, MODE_PLAN, MODE_RESEARCH
from app.services.agent.runtime import _finish_phase


def _begun(tmp_path: Path, *, mode: str, tid: int = 1) -> dict:
    rid = "run_test_1"
    log = tmp_path / "scratch" / "runs" / f"{rid}_{mode}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"system","subtype":"init","session_id":"sess_x"}\n')
    proc = MagicMock()
    proc.poll.return_value = 0
    return {
        "tid": tid,
        "mode": mode,
        "source": "test",
        "run_id": rid,
        "proc": proc,
        "pgid": 12345,
        "d": tmp_path,
        "log_path": log,
        "session_id": None,
        "add_dirs": [],
        "argv": ["agent"],
        "start_event": None,
        "clues_event": None,
        "plan_fp": {"exists": False, "mtime_ns": 0, "sha256": ""},
        "research_fp": {"exists": False, "mtime_ns": 0, "sha256": ""},
        "started_at": 0,
        "interactive": mode != MODE_AUTO_RESEARCH,
    }


def test_finish_auto_research_syncs_layers(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="auto", kind="readonly")
    tid = int(task["id"])
    d = Path(task["workspace_path"])
    (d / "scratch" / "runs").mkdir(parents=True, exist_ok=True)
    research = d / "research.md"
    research.write_text(
        "## 1. 诉求澄清\nA\n\n## 2. 事实层\nB\n\n## 3. 方案建议\nC\n",
        encoding="utf-8",
    )
    begun = _begun(d, mode=MODE_AUTO_RESEARCH, tid=tid)
    # fingerprint before write would miss; simulate empty before
    begun["research_fp"] = {"exists": False, "mtime_ns": 0, "sha256": ""}
    store.try_claim_agent_run(tid, mode=MODE_AUTO_RESEARCH, run_id=begun["run_id"])
    store.attach_agent_pgid(tid, run_id=begun["run_id"], pgid=12345)
    with patch("app.services.agent.runtime.wait_agent", return_value=0):
        with patch(
            "app.services.agent.runtime.parse_session_id_from_log",
            return_value="sess_auto",
        ):
            out = _finish_phase(store, begun)
    assert out["ok"] is True
    assert out.get("research_summary_event") is not None
    msg = out["research_summary_event"]["event"]["message"]
    assert "诉求澄清" in msg
    fresh = store.get_task(tid, events_limit=1)
    assert fresh.get("agent_run_id") is None
    assert fresh.get("agent_pgid") is None


def test_finish_ledger_research_skips_layers(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="ledger", kind="readonly")
    tid = int(task["id"])
    d = Path(task["workspace_path"])
    (d / "scratch" / "runs").mkdir(parents=True, exist_ok=True)
    (d / "research.md").write_text(
        "## 1. 诉求澄清\nA\n\n## 2. 事实层\nB\n\n## 3. 方案建议\nC\n",
        encoding="utf-8",
    )
    begun = _begun(d, mode=MODE_RESEARCH, tid=tid)
    store.try_claim_agent_run(tid, mode=MODE_RESEARCH, run_id=begun["run_id"])
    store.attach_agent_pgid(tid, run_id=begun["run_id"], pgid=12345)
    with patch("app.services.agent.runtime.wait_agent", return_value=0):
        with patch(
            "app.services.agent.runtime.parse_session_id_from_log",
            return_value="sess_led",
        ):
            out = _finish_phase(store, begun)
    assert out["ok"] is True
    assert out.get("research_summary_event") is None
    assert out.get("end_event") is None
    assert "三层摘要" not in (out.get("board_end_text") or "")


def test_finish_plan_prefers_log_over_stale_plan_md(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="plan", kind="readonly")
    tid = int(task["id"])
    d = Path(task["workspace_path"])
    (d / "scratch" / "runs").mkdir(parents=True, exist_ok=True)
    old = d / "plan.md"
    old.write_text("# OLD PLAN\n\nstale\n", encoding="utf-8")
    fp = {
        "exists": True,
        "mtime_ns": old.stat().st_mtime_ns,
        "sha256": __import__("hashlib").sha256(old.read_bytes()).hexdigest(),
    }
    new_plan = "# NEW PLAN\n\nfresh from CreatePlan\n"
    begun = _begun(d, mode=MODE_PLAN, tid=tid)
    begun["plan_fp"] = fp
    begun["log_path"].write_text(
        json.dumps(
            {
                "type": "tool_call",
                "tool_call": {
                    "createPlanToolCall": {"args": {"plan": new_plan}}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store.try_claim_agent_run(tid, mode=MODE_PLAN, run_id=begun["run_id"])
    store.attach_agent_pgid(tid, run_id=begun["run_id"], pgid=99)
    with patch("app.services.agent.runtime.wait_agent", return_value=0):
        with patch(
            "app.services.agent.runtime.parse_session_id_from_log",
            return_value="sess_p",
        ):
            out = _finish_phase(store, begun)
    assert out["ok"] is True
    assert out.get("plan_copy_event") is not None
    assert "NEW PLAN" in out["plan_copy_event"]["event"]["message"]
    assert "OLD PLAN" not in out["plan_copy_event"]["event"]["message"]
    assert "NEW PLAN" in (d / "plan.md").read_text(encoding="utf-8")


def test_finish_releases_run_when_kill_fails(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="kill-fail", kind="readonly")
    tid = int(task["id"])
    begun = _begun(Path(task["workspace_path"]), mode=MODE_AUTO_RESEARCH, tid=tid)
    begun["proc"].poll.side_effect = RuntimeError("wait failed")
    store.try_claim_agent_run(
        tid, mode=MODE_AUTO_RESEARCH, run_id=begun["run_id"]
    )
    store.attach_agent_pgid(
        tid, run_id=begun["run_id"], pgid=12345, pid=12345, proc_start="1"
    )
    with patch(
        "app.services.agent.runtime.kill_process_group",
        side_effect=PermissionError("denied"),
    ):
        out = _finish_phase(store, begun)

    assert out["ok"] is False
    fresh = store.get_task(tid, events_limit=1)
    assert fresh is not None
    assert fresh.get("agent_run_id") is None


def test_claim_agent_run_mutex(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="mutex", kind="readonly")
    tid = int(task["id"])
    a = store.try_claim_agent_run(tid, mode=MODE_RESEARCH, run_id="run_a")
    b = store.try_claim_agent_run(tid, mode=MODE_RESEARCH, run_id="run_b")
    assert a["ok"] is True
    assert b["ok"] is False
    assert b["reason"] == "already_running"
    store.release_agent_run(tid, run_id="run_a")
    c = store.try_claim_agent_run(tid, mode=MODE_PLAN, run_id="run_c")
    assert c["ok"] is True


def test_claim_agent_run_concurrent(db_file, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore

    store = TaskStore(str(db_file))
    task = store.create_task(title="conc", kind="readonly")
    tid = int(task["id"])
    results: list[bool] = []

    def _try(i: int) -> None:
        r = store.try_claim_agent_run(
            tid, mode=MODE_RESEARCH, run_id=f"run_{i}"
        )
        results.append(bool(r.get("ok")))

    threads = [threading.Thread(target=_try, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for x in results if x) == 1
