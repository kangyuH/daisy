from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import check_token, get_queue, get_task_store
from app.core.settings import auth_user_open_id, ledger_command_bot_id
from app.services.agent.constants import AGENT_MODES, normalize_mode
from app.services.agent.enqueue import enqueue_auto_research
from app.services.agent.parse import extract_plain_text, parse_ledger_command
from app.services.agent.runtime import (
    clear_task_typing_reaction,
    handle_ledger_command,
    kill_task_agent,
    run_phase_async,
)
from app.services.queue.service import ItemQueue
from app.services.tasks.board import TaskBoardSync
from app.services.tasks.store import TaskStore

router = APIRouter(prefix="/agent", tags=["agent"])


def _board_sync(request: Request, store: TaskStore) -> TaskBoardSync:
    enabled = bool(getattr(request.app.state, "task_board_enabled", True))
    chat_id = getattr(request.app.state, "calibration_chat_id", None)
    oid = getattr(request.app.state, "auth_user_open_id", None)
    if oid is None:
        oid = auth_user_open_id()
        request.app.state.auth_user_open_id = oid
    return TaskBoardSync(
        store,
        chat_id=chat_id,
        enabled=enabled,
        mention_user_open_id=str(oid or "").strip() or None,
    )


class LedgerCommandBody(BaseModel):
    bot_id: str
    message_id: str
    chat_id: str
    thread_id: Optional[str] = None
    root_id: Optional[str] = None
    sender_open_id: Optional[str] = None
    bot_open_id: Optional[str] = None
    self_open_id: Optional[str] = None
    content: Optional[Any] = None
    text: Optional[str] = None


class RunPhaseBody(BaseModel):
    task_id: int
    mode: str
    rest: str = ""
    source: str = "api"
    # Deprecated: ignored for behavior; kept so old clients don't 422.
    phase: Optional[str] = None
    trigger: Optional[str] = None


class StopBody(BaseModel):
    task_id: int


async def _resolve_ledger_sender(
    body: LedgerCommandBody, auth_oid: str
) -> tuple[Optional[str], str]:
    from app.services.im.fetch import resolve_user_sender_open_id

    sender = (body.sender_open_id or "").strip() or None
    bot_oids = {
        x
        for x in (
            (body.bot_open_id or "").strip(),
            (body.self_open_id or "").strip(),
        )
        if x
    }
    auth = (auth_oid or "").strip()
    if sender and sender == auth:
        return sender, "event"
    if sender and sender not in bot_oids and auth and sender != auth:
        pass
    mid = (body.message_id or "").strip()
    if mid:
        real = await resolve_user_sender_open_id(mid)
        if real:
            return real, "user_mget"
    return sender, "event_untrusted"


@router.post("/ledger")
async def ledger_command(
    body: LedgerCommandBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    """Process a calibration-chat slash command (spawn in-process, not queued)."""
    check_token(authorization)

    listener = ledger_command_bot_id()
    if listener and body.bot_id.strip() != listener:
        return {
            "ok": True,
            "skipped": True,
            "reason": "not_ledger_listener",
            "listener": listener,
            "bot_id": body.bot_id,
        }
    if not listener:
        bots = getattr(request.app.state, "bots", None) or []
        if bots and body.bot_id.strip() != str(bots[0].id):
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_default_listener",
                "bot_id": body.bot_id,
                "expected": str(bots[0].id),
            }

    auth_oid = getattr(request.app.state, "auth_user_open_id", None)
    if not auth_oid:
        auth_oid = auth_user_open_id()
        request.app.state.auth_user_open_id = auth_oid
    if not auth_oid:
        return {
            "ok": True,
            "skipped": True,
            "reason": "auth_user_unavailable",
        }

    sender, sender_via = await _resolve_ledger_sender(body, str(auth_oid))
    if not sender or sender != str(auth_oid).strip():
        return {
            "ok": True,
            "skipped": True,
            "reason": "sender_not_auth_user",
            "sender_open_id": sender,
            "sender_via": sender_via,
            "auth_user_open_id": str(auth_oid),
        }

    thread_id = (body.thread_id or "").strip() or None
    root_id = (body.root_id or "").strip() or None
    if not thread_id and not root_id:
        return {"ok": True, "skipped": True, "reason": "missing_thread_id"}

    task = await asyncio.to_thread(
        store.find_task_for_ledger, thread_id=thread_id, root_id=root_id
    )
    if not task:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_task_for_thread",
            "thread_id": thread_id,
            "root_id": root_id,
        }

    if thread_id and not task.get("board_thread_id"):
        try:
            await asyncio.to_thread(
                store.update_board_fields,
                int(task["id"]),
                board_thread_id=thread_id,
            )
            task = (
                await asyncio.to_thread(
                    store.get_task, int(task["id"]), events_limit=1
                )
                or task
            )
        except Exception:
            pass

    text = (body.text or "").strip() or extract_plain_text(body.content)
    parsed = parse_ledger_command(text)
    if not parsed:
        if text.startswith("/"):
            board = _board_sync(request, store)
            try:
                await board.post_board_message(
                    task,
                    (
                        f"不支持的口令（仅 /research /plan /exec /stop /status）："
                        f"{text[:80]}"
                    ),
                    idempotency_key=f"badcmd-{task['id']}-{body.message_id}"[:50],
                )
            except Exception:
                pass
            return {"ok": True, "rejected": True, "reason": "unsupported_command"}
        return {"ok": True, "skipped": True, "reason": "not_a_command"}

    board = _board_sync(request, store)
    result = await handle_ledger_command(
        store,
        task=task,
        cmd=parsed["cmd"],
        rest=parsed["rest"],
        message_id=str(body.message_id or ""),
        board_sync=board,
        reply_root_message_id=str(task.get("board_message_id") or "") or None,
    )
    return {
        "ok": True,
        "task_id": task["id"],
        "result": result,
        "sender_via": sender_via,
    }


@router.post("/run")
async def run_agent_phase(
    body: RunPhaseBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    """Blocking-ish: schedules phase on thread pool; used by agent worker."""
    check_token(authorization)
    raw_mode = (body.mode or body.phase or "").strip().lower()
    if raw_mode not in AGENT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid mode {raw_mode!r}; expected one of {sorted(AGENT_MODES)}",
        )
    mode = normalize_mode(raw_mode)
    source = (body.source or body.trigger or "api").strip() or "api"
    board = _board_sync(request, store)
    result = await run_phase_async(
        store,
        int(body.task_id),
        mode=mode,
        rest=body.rest,
        source=source,
        board_sync=board,
    )
    return {"ok": True, "result": result}


@router.post("/stop")
async def stop_agent(
    body: StopBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)

    out = await asyncio.to_thread(kill_task_agent, store, int(body.task_id))
    await clear_task_typing_reaction(store, int(body.task_id))
    board = _board_sync(request, store)
    fresh = await asyncio.to_thread(
        store.get_task, int(body.task_id), events_limit=1
    )
    if fresh:
        try:
            await board.post_board_message(
                fresh,
                f"/stop 已处理：{out}",
                idempotency_key=f"api-stop-{body.task_id}"[:50],
            )
        except Exception:
            pass
    return {"ok": True, "result": out}


class EnqueueResearchBody(BaseModel):
    task_id: int
    source: str = "create"
    inbound_id: Optional[int] = None
    trigger: Optional[str] = None  # deprecated alias for source


@router.post("/enqueue-research")
async def enqueue_research(
    body: EnqueueResearchBody,
    authorization: Optional[str] = Header(default=None),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    item = await enqueue_auto_research(
        queue,
        task_id=body.task_id,
        source=(body.source or body.trigger or "create"),
        inbound_id=body.inbound_id,
    )
    return {"ok": True, "item": item}
