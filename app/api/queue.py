from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.core.deps import check_token, get_queue
from app.services.im.reactions import add_typing_reaction, delete_reaction
from app.services.queue.service import QUEUE_INBOUND, ItemQueue

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


def _payload_str(payload: dict[str, Any], key: str) -> Optional[str]:
    val = payload.get(key)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


async def _maybe_add_typing_reaction(
    queue: ItemQueue, name: str, item: dict[str, Any]
) -> dict[str, Any]:
    if name != QUEUE_INBOUND or item.get("deduped"):
        return item
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    message_id = _payload_str(payload, "message_id")
    bot_id = _payload_str(payload, "bot_id")
    if not message_id or not bot_id:
        return item
    try:
        rid = await add_typing_reaction(message_id=message_id, profile=bot_id)
        return await queue.merge_payload(int(item["id"]), {"typing_reaction_id": rid})
    except Exception as exc:
        print(
            f"[queue] typing reaction create failed item={item.get('id')}: {exc}",
            flush=True,
        )
        return item


async def _maybe_remove_typing_reaction(item: dict[str, Any]) -> None:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    message_id = _payload_str(payload, "message_id")
    bot_id = _payload_str(payload, "bot_id")
    reaction_id = _payload_str(payload, "typing_reaction_id")
    if not (message_id and bot_id and reaction_id):
        return
    try:
        await delete_reaction(
            message_id=message_id,
            reaction_id=reaction_id,
            profile=bot_id,
        )
    except Exception as exc:
        print(
            f"[queue] typing reaction delete failed item={item.get('id')}: {exc}",
            flush=True,
        )


@router.post("/{name}/enqueue")
async def queue_enqueue(
    name: str,
    body: EnqueueBody,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    item = await queue.enqueue(name, body.payload, idempotency_key=body.idempotency_key)
    item = await _maybe_add_typing_reaction(queue, name, item)
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
        before = await queue.get(body.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if name == QUEUE_INBOUND:
        await _maybe_remove_typing_reaction(before)
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
