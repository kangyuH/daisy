from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from app.core.settings import calibration_chat_id
from app.infra.db import db_path
from app.services.agent.reconcile import agent_lock_health
from app.services.queue.service import QUEUE_AGENT, QUEUE_INBOUND, ItemQueue
from app.services.tasks.store import TaskStore

router = APIRouter(tags=["health"])


@router.get("/live")
async def live(request: Request):
    """Process liveness for probes — no SQLite access."""
    manager = getattr(request.app.state, "manager", None)
    body = {
        "ok": False,
        "live": True,
        "bots": [],
    }
    if not manager:
        body["error"] = "manager not started"
        return body
    h = manager.health()
    body.update(h)
    return body


@router.get("/health")
async def health(request: Request):
    queue_stats = None
    queues: dict = {}
    q: Optional[ItemQueue] = getattr(request.app.state, "queue", None)
    if q:
        try:
            inbound = await q.stats(QUEUE_INBOUND)
            agent = await q.stats(QUEUE_AGENT)
            queues = {"inbound": inbound, "agent": agent}
            queue_stats = inbound  # back-compat top-level
        except Exception as exc:
            queue_stats = {"error": str(exc)}
            queues = {"error": str(exc)}

    agent_locks = None
    task_store: Optional[TaskStore] = getattr(request.app.state, "task_store", None)
    if task_store is not None:
        try:
            agent_locks = agent_lock_health(task_store)
        except Exception as exc:
            agent_locks = {"error": str(exc)}

    manager = getattr(request.app.state, "manager", None)
    body = {
        "ok": False,
        "db_path": getattr(request.app.state, "db_path", str(db_path())),
        "calibration_chat_id": getattr(
            request.app.state, "calibration_chat_id", calibration_chat_id()
        ),
        "queue": queue_stats,
        "queues": queues,
        "agent_locks": agent_locks,
        "bots": [],
    }
    if not manager:
        body["error"] = "manager not started"
        return body
    h = manager.health()
    body.update(h)
    if queue_stats and "pending" in queue_stats:
        body["pending"] = queue_stats["pending"]
    return body
