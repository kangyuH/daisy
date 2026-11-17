from __future__ import annotations

from collections.abc import Awaitable
from typing import Optional, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.api.im_payload import (
    OutboundRequest,
    ReplyBody,
    RespondBody,
    SendBody,
    outbound_openapi,
    read_outbound,
)
from app.core.deps import bot_profile, check_token, get_queue, get_task_store
from app.infra.lark.cli import LarkCliError
from app.services.im.gateway import (
    ImGatewayError,
    get_inbound_message,
    get_message_context,
    link_outbound_task,
    respond_to_inbound,
    validate_task_send,
)
from app.services.im.outbound import reply_message, send_message

router = APIRouter(prefix="/im", tags=["im"])
Result = TypeVar("Result")


class ContextBody(BaseModel):
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    before: int = 10
    after: int = 5
    before_seconds: Optional[int] = None
    after_seconds: Optional[int] = None


async def _call_im(operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except ImGatewayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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
    await _call_im(validate_task_send(get_task_store(request), body.task_id, body.idempotency_key))
    result = await _call_im(
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
        **await link_outbound_task(request.app.state.message_task_store, body.task_id, result),
    }


@router.post("/respond", openapi_extra=outbound_openapi("respond", RespondBody))
async def im_respond(
    request: Request,
    outbound: OutboundRequest[RespondBody] = Depends(read_outbound(RespondBody)),
):
    """Look up inbound queue item; auto-fill message_id/thread/sender for reply."""
    body = outbound.body
    inbound = await _call_im(get_inbound_message(get_queue(request), body.inbound_id))
    return await _call_im(
        respond_to_inbound(
            inbound,
            profile=bot_profile(request, inbound.bot_id),
            task_store=get_task_store(request),
            message_task_store=request.app.state.message_task_store,
            content=outbound.content_kwargs(),
            task_id=body.task_id,
            idempotency_key=body.idempotency_key,
        )
    )


@router.post("/send", openapi_extra=outbound_openapi("send", SendBody))
async def im_send(
    request: Request,
    outbound: OutboundRequest[SendBody] = Depends(read_outbound(SendBody)),
):
    body = outbound.body
    await _call_im(validate_task_send(get_task_store(request), body.task_id, body.idempotency_key))
    result = await _call_im(
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
        **await link_outbound_task(request.app.state.message_task_store, body.task_id, result),
    }


@router.post("/messages/context")
async def im_messages_context(
    body: ContextBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    check_token(authorization)
    return await _call_im(
        get_message_context(request.app.state.message_task_store, **body.model_dump())
    )
