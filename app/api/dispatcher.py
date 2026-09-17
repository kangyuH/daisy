from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.core.deps import check_token, get_dispatch_run_store
from app.services.dispatcher.store import DispatchRunStore

router = APIRouter(prefix="/dispatcher", tags=["dispatcher"])


class CreateDispatchRunBody(BaseModel):
    inbound_id: int
    decision: str
    reason: str
    task_id: Optional[int] = None
    evidence: Optional[Any] = None
    actor: str = "dispatcher"


@router.post("/runs")
async def create_dispatch_run(
    body: CreateDispatchRunBody,
    authorization: Optional[str] = Header(default=None),
    store: DispatchRunStore = Depends(get_dispatch_run_store),
):
    check_token(authorization)
    try:
        run = store.create_run(
            inbound_id=body.inbound_id,
            decision=body.decision,
            reason=body.reason,
            task_id=body.task_id,
            evidence=body.evidence,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "run": run, "deduped": bool(run.get("deduped"))}


@router.get("/runs/{inbound_id}")
async def get_dispatch_run(
    inbound_id: int,
    authorization: Optional[str] = Header(default=None),
    store: DispatchRunStore = Depends(get_dispatch_run_store),
):
    check_token(authorization)
    run = store.get_by_inbound(inbound_id)
    if not run:
        raise HTTPException(
            status_code=404, detail=f"dispatch_run for inbound {inbound_id} not found"
        )
    return {"ok": True, "run": run}
