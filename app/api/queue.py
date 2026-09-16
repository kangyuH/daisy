from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.core.deps import check_token, get_queue
from app.services.queue.service import ItemQueue

router = APIRouter(prefix="/queue", tags=["queue"])


class EnqueueBody(BaseModel):
    payload: Any
    idempotency_key: Optional[str] = None


class ClaimBody(BaseModel):
    limit: int = 1
    claimed_by: str = "worker"


class AckBody(BaseModel):
    id: int
    error: Optional[str] = None


class NackBody(BaseModel):
    id: int
    requeue: bool = True
    error: Optional[str] = None


@router.post("/{name}/enqueue")
async def queue_enqueue(
    name: str,
    body: EnqueueBody,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    item = await queue.enqueue(name, body.payload, idempotency_key=body.idempotency_key)
    return {"ok": True, "item": item}


@router.post("/{name}/claim")
async def queue_claim(
    name: str,
    body: ClaimBody,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    items = await queue.claim(name, limit=body.limit, claimed_by=body.claimed_by)
    return {"ok": True, "items": items}


@router.post("/{name}/ack")
async def queue_ack(
    name: str,
    body: AckBody,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    try:
        item = await queue.ack(body.id, error=body.error)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.post("/{name}/nack")
async def queue_nack(
    name: str,
    body: NackBody,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    try:
        item = await queue.nack(body.id, requeue=body.requeue, error=body.error)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.get("/{name}/stats")
async def queue_stats(
    name: str,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    return {"ok": True, **(await queue.stats(name))}
