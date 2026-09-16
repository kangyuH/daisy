from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.deps import bot_profile, check_token, get_queue
from app.infra.lark.cli import LarkCliError
from app.services.im.fetch import fetch_context
from app.services.im.outbound import reply_message, respond_to_message, send_message
from app.services.queue.service import QUEUE_INBOUND

router = APIRouter(prefix="/im", tags=["im"])


class ReplyBody(BaseModel):
    message_id: str
    text: str = ""
    mention_open_ids: list[str] = Field(default_factory=list)
    reply_in_thread: bool = False
    bot_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class RespondBody(BaseModel):
    """Worker only sends inbound queue item id + text (+ optional extra @)."""

    inbound_id: int
    text: str = ""
    mention_open_ids: list[str] = Field(default_factory=list)
    idempotency_key: Optional[str] = None


class SendBody(BaseModel):
    chat_id: str
    text: str = ""
    mention_open_ids: list[str] = Field(default_factory=list)
    bot_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class ContextBody(BaseModel):
    message_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    before: int = 10
    after: int = 5
    before_seconds: Optional[int] = None
    after_seconds: Optional[int] = None


def _payload_str(payload: dict[str, Any], key: str) -> Optional[str]:
    val = payload.get(key)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


@router.post("/reply")
async def im_reply(
    body: ReplyBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    check_token(authorization)
    profile = bot_profile(request, body.bot_id)
    try:
        result = await reply_message(
            message_id=body.message_id,
            text=body.text,
            mention_open_ids=body.mention_open_ids,
            reply_in_thread=body.reply_in_thread,
            profile=profile,
            idempotency_key=body.idempotency_key,
        )
    except LarkCliError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": str(exc),
                "returncode": exc.returncode,
                "stdout": exc.stdout,
                "stderr": exc.stderr,
            },
        ) from exc
    return {"ok": True, "result": result}


@router.post("/respond")
async def im_respond(
    body: RespondBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """Look up inbound queue item; auto-fill message_id/thread/sender for reply."""
    check_token(authorization)
    q = get_queue(request)
    try:
        item = await q.get(body.inbound_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if str(item.get("queue") or "") != QUEUE_INBOUND:
        raise HTTPException(
            status_code=400,
            detail=f"item {body.inbound_id} is not an inbound message queue entry",
        )

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    message_id = _payload_str(payload, "message_id")
    if not message_id:
        raise HTTPException(
            status_code=400,
            detail=f"inbound item {body.inbound_id} missing message_id in payload",
        )

    bot_id = _payload_str(payload, "bot_id")
    profile = bot_profile(request, bot_id)
    thread_id = _payload_str(payload, "thread_id")
    sender_open_id = _payload_str(payload, "sender_open_id")
    idem = body.idempotency_key or f"inbound:{body.inbound_id}"

    try:
        result = await respond_to_message(
            message_id=message_id,
            text=body.text,
            profile=profile,
            thread_id=thread_id,
            sender_open_id=sender_open_id,
            mention_open_ids=body.mention_open_ids,
            idempotency_key=idem,
        )
    except LarkCliError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": str(exc),
                "returncode": exc.returncode,
                "stdout": exc.stdout,
                "stderr": exc.stderr,
            },
        ) from exc
    return {
        "ok": True,
        "inbound_id": body.inbound_id,
        "result": result,
    }


@router.post("/send")
async def im_send(
    body: SendBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    check_token(authorization)
    profile = bot_profile(request, body.bot_id)
    try:
        result = await send_message(
            chat_id=body.chat_id,
            text=body.text,
            mention_open_ids=body.mention_open_ids,
            profile=profile,
            idempotency_key=body.idempotency_key,
        )
    except LarkCliError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": str(exc),
                "returncode": exc.returncode,
                "stdout": exc.stdout,
                "stderr": exc.stderr,
            },
        ) from exc
    return {"ok": True, "result": result}


@router.post("/messages/context")
async def im_messages_context(
    body: ContextBody,
    authorization: Optional[str] = Header(default=None),
):
    check_token(authorization)
    if not (body.message_id or body.chat_id or body.thread_id):
        raise HTTPException(
            status_code=400,
            detail="at least one of message_id, chat_id, thread_id is required",
        )
    try:
        result = await fetch_context(
            message_id=body.message_id,
            chat_id=body.chat_id,
            thread_id=body.thread_id,
            before=body.before,
            after=body.after,
            before_seconds=body.before_seconds,
            after_seconds=body.after_seconds,
        )
    except LarkCliError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": str(exc),
                "returncode": exc.returncode,
                "stdout": exc.stdout,
                "stderr": exc.stderr,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}
