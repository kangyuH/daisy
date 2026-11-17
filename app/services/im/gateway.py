from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.infra.db import sqlite3
from app.services.im.fetch import add_task_refs, fetch_context
from app.services.im.outbound import extract_message_id, respond_to_message
from app.services.im.task_links import MessageTaskStore
from app.services.queue.service import QUEUE_INBOUND, ItemQueue
from app.services.tasks.store import TaskStore

logger = logging.getLogger(__name__)


class ImGatewayError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class InboundMessage:
    inbound_id: int
    message_id: str
    bot_id: str | None
    chat_id: str | None
    thread_id: str | None
    reply_to_message_id: str | None
    sender_open_id: str | None


def _payload_str(payload: dict[str, Any], key: str) -> str | None:
    val = payload.get(key)
    if val is None:
        return None
    return str(val).strip() or None


async def get_inbound_message(queue: ItemQueue, inbound_id: int) -> InboundMessage:
    try:
        item = await queue.get(inbound_id)
    except KeyError as exc:
        raise ImGatewayError(404, str(exc)) from exc

    if str(item.get("queue") or "") != QUEUE_INBOUND:
        raise ImGatewayError(400, f"item {inbound_id} is not an inbound message queue entry")

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    message_id = _payload_str(payload, "message_id")
    if not message_id:
        raise ImGatewayError(400, f"inbound item {inbound_id} missing message_id in payload")

    return InboundMessage(
        inbound_id=inbound_id,
        message_id=message_id,
        bot_id=_payload_str(payload, "bot_id"),
        chat_id=_payload_str(payload, "chat_id"),
        thread_id=_payload_str(payload, "thread_id"),
        reply_to_message_id=_payload_str(payload, "reply_to_message_id"),
        sender_open_id=_payload_str(payload, "sender_open_id"),
    )


async def validate_task_send(
    task_store: TaskStore, task_id: int | None, idempotency_key: str | None
) -> None:
    if task_id is None:
        return
    task = await asyncio.to_thread(task_store.get_task, task_id, events_limit=1)
    if task is None:
        raise ImGatewayError(404, f"task {task_id} not found")
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 50:
        raise ImGatewayError(400, "task sends require an idempotency_key of 1–50 characters")


async def link_outbound_task(
    message_task_store: MessageTaskStore, task_id: int | None, result: Any
) -> dict[str, Any]:
    if task_id is None:
        return {}
    message_id = extract_message_id(result)
    link = {"task_id": task_id, "message_id": message_id, "status": "unrecorded"}
    if not message_id:
        return {"task_link": {**link, "reason": "missing_message_id"}}

    try:
        linked = await asyncio.to_thread(
            message_task_store.record_outbound, message_id, task_id
        )
    except sqlite3.Error:
        logger.exception("Failed to link outbound message %s to task %s", message_id, task_id)
        return {"task_link": {**link, "reason": "storage_error"}}
    if not linked:
        return {"task_link": {**link, "reason": "message_link_conflict"}}
    return {"task_link": {**link, "status": "recorded"}}


async def respond_to_inbound(
    inbound: InboundMessage,
    *,
    profile: str | None,
    task_store: TaskStore,
    message_task_store: MessageTaskStore,
    content: dict[str, Any],
    task_id: int | None = None,
    idempotency_key: str | None = None,
    inferred_task_chat_id: str | None = None,
) -> dict[str, Any]:
    idem = idempotency_key or f"inbound:{inbound.inbound_id}"
    await validate_task_send(task_store, task_id, idem)
    if task_id is None and idem.strip():
        task_id = await asyncio.to_thread(
            message_task_store.find_inbound_task,
            inbound.inbound_id,
            task_chat_id=inferred_task_chat_id,
        )

    result = await respond_to_message(
        message_id=inbound.message_id,
        profile=profile,
        thread_id=inbound.thread_id,
        sender_open_id=inbound.sender_open_id,
        idempotency_key=idem,
        **content,
    )
    return {
        "ok": True,
        "inbound_id": inbound.inbound_id,
        "result": result,
        **await link_outbound_task(message_task_store, task_id, result),
    }


async def get_message_context(
    message_task_store: MessageTaskStore,
    *,
    message_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    reply_to_message_id: str | None = None,
    before: int = 10,
    after: int = 5,
    before_seconds: int | None = None,
    after_seconds: int | None = None,
    task_chat_id: str | None = None,
) -> dict[str, Any]:
    if not (message_id or chat_id or thread_id):
        raise ImGatewayError(400, "at least one of message_id, chat_id, thread_id is required")
    try:
        result = await fetch_context(
            message_id=message_id,
            chat_id=chat_id,
            thread_id=thread_id,
            before=before,
            after=after,
            before_seconds=before_seconds,
            after_seconds=after_seconds,
        )
    except ValueError as exc:
        raise ImGatewayError(400, str(exc)) from exc

    context = await add_task_refs(
        result,
        message_task_store,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        task_chat_id=task_chat_id,
    )
    return {"ok": True, **context}
