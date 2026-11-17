from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.im_payload import (
    OutboundRequest,
    ReplyBody,
    RespondBody,
    SendBody,
    outbound_openapi,
    read_outbound,
)
from app.core.deps import (
    bot_profile,
    check_token,
    get_queue,
    get_task_store,
)
from app.infra.db import sqlite3
from app.infra.lark.cli import LarkCliError
from app.services.im.fetch import add_task_refs, fetch_context
from app.services.im.outbound import (
    extract_message_id,
    reply_message,
    respond_to_message,
    send_message,
)
from app.services.queue.service import QUEUE_INBOUND

router = APIRouter(prefix="/im", tags=["im"])
logger = logging.getLogger(__name__)


class ContextBody(BaseModel):
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
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


async def _validate_task_send(
    request: Request, task_id: Optional[int], idempotency_key: Optional[str]
) -> None:
    if task_id is None:
        return
    task = await asyncio.to_thread(
        get_task_store(request).get_task, task_id, events_limit=1
    )
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 50:
        raise HTTPException(
            status_code=400, detail="task sends require an idempotency_key of 1–50 characters"
        )


async def _outbound_task_link(
    request: Request, task_id: Optional[int], result: Any
) -> dict[str, Any]:
    if task_id is None:
        return {}
    message_id = extract_message_id(result)
    link = {"task_id": task_id, "message_id": message_id, "status": "unrecorded"}
    if not message_id:
        return {"task_link": {**link, "reason": "missing_message_id"}}

    try:
        linked = await asyncio.to_thread(
            request.app.state.message_task_store.record_outbound, message_id, task_id
        )
    except sqlite3.Error:
        logger.exception("Failed to link outbound message %s to task %s", message_id, task_id)
        return {"task_link": {**link, "reason": "storage_error"}}
    if not linked:
        return {"task_link": {**link, "reason": "message_link_conflict"}}
    return {"task_link": {**link, "status": "recorded"}}


async def _send_outbound(operation: Awaitable[dict]) -> dict:
    try:
        return await operation
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


@router.post("/reply", openapi_extra=outbound_openapi("reply", ReplyBody))
async def im_reply(
    request: Request,
    outbound: OutboundRequest[ReplyBody] = Depends(read_outbound(ReplyBody)),
):
    body = outbound.body
    await _validate_task_send(request, body.task_id, body.idempotency_key)
    result = await _send_outbound(
        reply_message(
            message_id=body.message_id,
            reply_in_thread=body.reply_in_thread,
            profile=bot_profile(request, body.bot_id),
            idempotency_key=body.idempotency_key,
            **outbound.content_kwargs(),
        )
    )
    return {
        "ok": True,
        "result": result,
        **await _outbound_task_link(request, body.task_id, result),
    }


@router.post("/respond", openapi_extra=outbound_openapi("respond", RespondBody))
async def im_respond(
    request: Request,
    outbound: OutboundRequest[RespondBody] = Depends(read_outbound(RespondBody)),
):
    """Look up inbound queue item; auto-fill message_id/thread/sender for reply."""
    body = outbound.body
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
    await _validate_task_send(request, body.task_id, idem)
    task_id = body.task_id
    if task_id is None and idem.strip():
        task_id = await asyncio.to_thread(
            request.app.state.message_task_store.find_inbound_task, body.inbound_id
        )


    result = await _send_outbound(
        respond_to_message(
            message_id=message_id,
            profile=profile,
            thread_id=thread_id,
            sender_open_id=sender_open_id,
            idempotency_key=idem,
            **outbound.content_kwargs(),
        )
    )
    return {
        "ok": True,
        "inbound_id": body.inbound_id,
        "result": result,
        **await _outbound_task_link(request, task_id, result),
    }


@router.post("/send", openapi_extra=outbound_openapi("send", SendBody))
async def im_send(
    request: Request,
    outbound: OutboundRequest[SendBody] = Depends(read_outbound(SendBody)),
):
    body = outbound.body
    await _validate_task_send(request, body.task_id, body.idempotency_key)
    result = await _send_outbound(
        send_message(
            chat_id=body.chat_id,
            profile=bot_profile(request, body.bot_id),
            idempotency_key=body.idempotency_key,
            **outbound.content_kwargs(),
        )
    )
    return {
        "ok": True,
        "result": result,
        **await _outbound_task_link(request, body.task_id, result),
    }


@router.post("/messages/context")
async def im_messages_context(
    body: ContextBody,
    request: Request,
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
    context = await add_task_refs(
        result,
        request.app.state.message_task_store,
        chat_id=body.chat_id,
        reply_to_message_id=body.reply_to_message_id,
    )
    return {"ok": True, **context}
