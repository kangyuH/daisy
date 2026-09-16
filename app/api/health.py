from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from app.core.settings import calibration_chat_id
from app.infra.db import db_path
from app.services.queue.service import ItemQueue

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    queue_stats = None
    q: Optional[ItemQueue] = getattr(request.app.state, "queue", None)
    if q:
        try:
            queue_stats = await q.stats("inbound")
        except Exception as exc:
            queue_stats = {"error": str(exc)}

    manager = getattr(request.app.state, "manager", None)
    body = {
        "ok": False,
        "db_path": getattr(request.app.state, "db_path", str(db_path())),
        "calibration_chat_id": getattr(
            request.app.state, "calibration_chat_id", calibration_chat_id()
        ),
        "queue": queue_stats,
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
