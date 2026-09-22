from __future__ import annotations

from typing import Any

from app.services.agent.cli_runner import (
    kill_process_group,
    process_identity_matches,
)
from app.services.tasks.models import TaskConflictError
from app.services.tasks.store import TaskStore


def reconcile_agent_locks(
    store: TaskStore,
    *,
    kill_orphans: bool = False,
) -> dict[str, Any]:
    """
    Startup / stop reconcile for agent run claims.

    - dead / missing identity → release (optionally kill pgid if kill_orphans)
    - alive with matching starttime → keep; note stale_live if lease expired
    - unknown (/proc unreadable) → keep lock
    Never kill a still-alive matching process on startup (kill_orphans=False).
    """
    released: list[int] = []
    kept_live: list[int] = []
    stale_live: list[int] = []
    unknown: list[int] = []
    killed: list[int] = []
    errors: list[dict[str, Any]] = []

    for task in store.list_agent_locks():
        tid = int(task["id"])
        pid = task.get("agent_pid")
        pgid = task.get("agent_pgid")
        proc_start = task.get("agent_proc_start")
        state = process_identity_matches(
            pid=int(pid) if pid is not None else None,
            pgid=int(pgid) if pgid is not None else None,
            proc_start=str(proc_start) if proc_start is not None else None,
        )
        if state == "alive":
            kept_live.append(tid)
            lease = str(task.get("agent_lease_until") or "").strip()
            if lease:
                from datetime import datetime, timedelta, timezone

                tz = timezone(timedelta(hours=8))
                try:
                    until = datetime.fromisoformat(lease)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=tz)
                    if until <= datetime.now(tz):
                        stale_live.append(tid)
                except ValueError:
                    stale_live.append(tid)
            if kill_orphans and pgid is not None:
                try:
                    kill_process_group(int(pgid))
                    killed.append(tid)
                except Exception as exc:
                    errors.append({"task_id": tid, "action": "kill", "error": str(exc)})
                    print(
                        f"[agent-reconcile] kill task={tid} pgid={pgid} failed: {exc}",
                        flush=True,
                    )
                    continue
                store.release_agent_run(tid, force=True)
                released.append(tid)
            continue
        if state == "unknown":
            unknown.append(tid)
            print(
                f"[agent-reconcile] task={tid} process identity unknown; keep lock",
                flush=True,
            )
            continue
        # dead
        try:
            if kill_orphans:
                store.release_agent_run(tid, force=True)
            else:
                store.release_agent_run(
                    tid, run_id=str(task.get("agent_run_id") or "")
                )
        except TaskConflictError:
            # The observed run was replaced while reconciling; never clear it.
            continue
        released.append(tid)
        print(
            f"[agent-reconcile] released stale agent lock task={tid} "
            f"run={task.get('agent_run_id')}",
            flush=True,
        )

    return {
        "released": released,
        "kept_live": kept_live,
        "stale_live": stale_live,
        "unknown": unknown,
        "killed": killed,
        "errors": errors,
    }


def agent_lock_health(store: TaskStore) -> dict[str, Any]:
    running = 0
    stale_live = 0
    unknown = 0
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    for task in store.list_agent_locks():
        state = process_identity_matches(
            pid=int(task["agent_pid"]) if task.get("agent_pid") is not None else None,
            pgid=int(task["agent_pgid"]) if task.get("agent_pgid") is not None else None,
            proc_start=str(task.get("agent_proc_start") or "") or None,
        )
        if state == "alive":
            running += 1
            lease = str(task.get("agent_lease_until") or "").strip()
            if lease:
                try:
                    until = datetime.fromisoformat(lease)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=tz)
                    if until <= now:
                        stale_live += 1
                except ValueError:
                    stale_live += 1
        elif state == "unknown":
            unknown += 1
            running += 1
        # dead locks should be rare mid-flight; count as unknown until reconcile
        else:
            unknown += 1
    return {
        "running": running,
        "stale_live": stale_live,
        "unknown": unknown,
    }


def clear_dead_agent_lock(store: TaskStore, task: dict[str, Any]) -> bool:
    """If lock present but process dead, release and return True."""
    if not (task.get("agent_run_id") or task.get("agent_pgid") is not None):
        return False
    state = process_identity_matches(
        pid=int(task["agent_pid"]) if task.get("agent_pid") is not None else None,
        pgid=int(task["agent_pgid"]) if task.get("agent_pgid") is not None else None,
        proc_start=str(task.get("agent_proc_start") or "") or None,
    )
    if state == "dead":
        run_id = str(task.get("agent_run_id") or "").strip()
        if not run_id:
            return False
        try:
            store.release_agent_run(int(task["id"]), run_id=run_id)
            return True
        except TaskConflictError:
            return False
    return False
