from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from app.core.deps import check_token, get_queue
from app.services.queue.service import JobQueue

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
    request: Request,
    authorization: Optional[str] = Header(default=None),
    queue: JobQueue = Depends(get_queue),
):
    check_token(authorization)
    job = await queue.enqueue(name, body.payload, idempotency_key=body.idempotency_key)
    return {"ok": True, "job": job}


@router.post("/{name}/claim")
async def queue_claim(
    name: str,
    body: ClaimBody,
    authorization: Optional[str] = Header(default=None),
    queue: JobQueue = Depends(get_queue),
):
    check_token(authorization)
    jobs = await queue.claim(name, limit=body.limit, claimed_by=body.claimed_by)
    return {"ok": True, "jobs": jobs}


@router.post("/{name}/ack")
async def queue_ack(
    name: str,
    body: AckBody,
    authorization: Optional[str] = Header(default=None),
    queue: JobQueue = Depends(get_queue),
):
    from fastapi import HTTPException

    check_token(authorization)
    try:
        job = await queue.ack(body.id, error=body.error)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.post("/{name}/nack")
async def queue_nack(
    name: str,
    body: NackBody,
    authorization: Optional[str] = Header(default=None),
    queue: JobQueue = Depends(get_queue),
):
    from fastapi import HTTPException

    check_token(authorization)
    try:
        job = await queue.nack(body.id, requeue=body.requeue, error=body.error)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.get("/{name}/stats")
async def queue_stats(
    name: str,
    authorization: Optional[str] = Header(default=None),
    queue: JobQueue = Depends(get_queue),
):
    check_token(authorization)
    return {"ok": True, **(await queue.stats(name))}
