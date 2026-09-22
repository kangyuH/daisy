from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import (
    check_token,
    get_dispatch_run_store,
    get_queue,
    get_task_store,
)
from app.services.dispatcher.ack import maybe_ack_dispatch_run
from app.services.dispatcher.store import DispatchRunStore
from app.services.agent.enqueue import enqueue_auto_research
from app.services.queue.service import ItemQueue
from app.services.tasks.store import TaskStore

router = APIRouter(prefix="/dispatcher", tags=["dispatcher"])


class CreateDispatchRunBody(BaseModel):
    inbound_id: int
    decision: str
    reason: str
    task_id: Optional[int] = None
    evidence: Optional[Any] = None
    actor: str = "dispatcher"


async def _send_ack_if_needed(
    *,
    request: Request,
    queue: ItemQueue,
    task_store: TaskStore,
    store: DispatchRunStore,
    run: dict[str, Any],
    force: bool = False,
) -> Optional[dict[str, Any]]:
    enabled = bool(getattr(request.app.state, "dispatch_ack_enabled", True))
    if not enabled:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    ack_status = str(run.get("ack_status") or "").strip()
    if ack_status == "sent" and not force:
        return {"ok": True, "skipped": True, "reason": "already_sent"}
    reply_sync = await maybe_ack_dispatch_run(
        request=request,
        queue=queue,
        task_store=task_store,
        inbound_id=int(run["inbound_id"]),
        decision=str(run.get("decision") or ""),
        task_id=run.get("task_id"),
        enabled=enabled,
    )
    if reply_sync.get("skipped"):
        return reply_sync
    if reply_sync.get("ok"):
        await asyncio.to_thread(
            store.update_ack,
            int(run["inbound_id"]),
            ack_status="sent",
            ack_error=None,
        )
    else:
        await asyncio.to_thread(
            store.update_ack,
            int(run["inbound_id"]),
            ack_status="failed",
            ack_error=str(reply_sync.get("error") or "ack failed"),
        )
    return reply_sync


@router.post("/runs")
async def create_dispatch_run(
    body: CreateDispatchRunBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: DispatchRunStore = Depends(get_dispatch_run_store),
    queue: ItemQueue = Depends(get_queue),
    task_store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    try:
        run = await asyncio.to_thread(
            store.create_run,
            inbound_id=body.inbound_id,
            decision=body.decision,
            reason=body.reason,
            task_id=body.task_id,
            evidence=body.evidence,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    deduped = bool(run.get("deduped"))
    reply_sync: Optional[dict[str, Any]] = None
    research_enqueue: Optional[dict[str, Any]] = None
    # Fresh run: always try ack. Deduped run: retry ack when not yet sent.
    reply_sync = await _send_ack_if_needed(
        request=request,
        queue=queue,
        task_store=task_store,
        store=store,
        run=run,
        force=False,
    )
    if (
        str(run.get("decision") or "") == "create"
        and run.get("task_id") is not None
    ):
        try:
            research_enqueue = await enqueue_auto_research(
                queue,
                task_id=int(run["task_id"]),
                source="dispatch_create",
                inbound_id=int(run["inbound_id"]),
            )
        except Exception as exc:
            research_enqueue = {"ok": False, "error": str(exc)[:500]}

    return {
        "ok": True,
        "run": run,
        "deduped": deduped,
        "reply_sync": reply_sync,
        "research_enqueue": research_enqueue,
    }


@router.get("/runs/{inbound_id}")
async def get_dispatch_run(
    inbound_id: int,
    authorization: Optional[str] = Header(default=None),
    store: DispatchRunStore = Depends(get_dispatch_run_store),
):
    check_token(authorization)
    run = await asyncio.to_thread(store.get_by_inbound, inbound_id)
    if not run:
        raise HTTPException(
            status_code=404, detail=f"dispatch_run for inbound {inbound_id} not found"
        )
    return {"ok": True, "run": run}
