from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.deps import check_token, get_queue, get_task_store
from app.core.settings import auth_user_open_id
from app.services.agent.enqueue import enqueue_auto_research
from app.services.queue.service import ItemQueue
from app.services.tasks.board import TaskBoardSync
from app.services.tasks.models import (
    EVENT_NOTE,
    TaskConflictError,
    TaskValidationError,
)
from app.services.tasks.store import TaskStore

router = APIRouter(tags=["tasks"])


def _resolve_board_mention_open_id(request: Request) -> Optional[str]:
    if hasattr(request.app.state, "auth_user_open_id"):
        cached = getattr(request.app.state, "auth_user_open_id")
        return str(cached or "").strip() or None
    oid = auth_user_open_id()
    request.app.state.auth_user_open_id = oid
    return oid


def _board_sync(request: Request, store: TaskStore) -> TaskBoardSync:
    enabled = bool(getattr(request.app.state, "task_board_enabled", True))
    chat_id = getattr(request.app.state, "calibration_chat_id", None)
    return TaskBoardSync(
        store,
        chat_id=chat_id,
        enabled=enabled,
        mention_user_open_id=_resolve_board_mention_open_id(request),
    )


class CreateTaskBody(BaseModel):
    title: str
    kind: str
    one_liner: str = ""
    bot_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    project_id: Optional[str] = None
    status: Optional[str] = None
    created_from_inbound_id: Optional[int] = None
    actor: str = "api"
    message: Optional[str] = None


class FollowupBody(BaseModel):
    message: str
    event_type: str = EVENT_NOTE
    status: Optional[str] = None
    actor: str = "api"
    inbound_id: Optional[int] = None
    payload: Optional[Any] = None


class BoardMessageBody(BaseModel):
    text: str
    idempotency_key: str
    markdown: bool = True
    actor: str = "agent"


class UpsertClueBody(BaseModel):
    kind: str
    ref_key: str
    one_liner: Optional[str] = None
    relevance: Optional[str] = None
    demote: bool = False
    actor: str = "api"
    extra: Optional[Any] = None


class ChatProjectBody(BaseModel):
    project_id: str
    note: str = ""


def _http_from_store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TaskValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, TaskConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    raise exc


@router.post("/tasks")
async def create_task(
    body: CreateTaskBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
    queue: ItemQueue = Depends(get_queue),
):
    check_token(authorization)
    try:
        task = await asyncio.to_thread(
            store.create_task,
            title=body.title,
            kind=body.kind,
            one_liner=body.one_liner,
            bot_id=body.bot_id,
            chat_id=body.chat_id,
            thread_id=body.thread_id,
            project_id=body.project_id,
            status=body.status,
            created_from_inbound_id=body.created_from_inbound_id,
            actor=body.actor,
            message=body.message,
        )
    except (TaskValidationError, TaskConflictError, KeyError) as exc:
        raise _http_from_store_error(exc) from exc
    board = await _board_sync(request, store).ensure_board(task)
    research_enqueue = None
    try:
        research_enqueue = await enqueue_auto_research(
            queue,
            task_id=int(task["id"]),
            source="api_create",
            inbound_id=body.created_from_inbound_id,
        )
    except Exception as exc:
        research_enqueue = {"ok": False, "error": str(exc)[:500]}
    refreshed = await asyncio.to_thread(store.get_task, int(task["id"]))
    return {
        "ok": True,
        "task": refreshed or task,
        "board_sync": board,
        "research_enqueue": research_enqueue,
    }


@router.get("/tasks")
async def list_tasks(
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
    status: Optional[str] = Query(default=None),
    bot_id: Optional[str] = Query(default=None),
    chat_id: Optional[str] = Query(default=None),
    thread_id: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    check_token(authorization)
    try:
        tasks = await asyncio.to_thread(
            store.list_tasks,
            status=status,
            bot_id=bot_id,
            chat_id=chat_id,
            thread_id=thread_id,
            project_id=project_id,
            kind=kind,
            limit=limit,
        )
    except TaskValidationError as exc:
        raise _http_from_store_error(exc) from exc
    return {"ok": True, "tasks": tasks}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
    events_limit: int = Query(default=50, ge=1, le=500),
):
    check_token(authorization)
    task = await asyncio.to_thread(
        store.get_task, task_id, events_limit=events_limit
    )
    if not task:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return {"ok": True, "task": task}


@router.post("/tasks/{task_id}/followups")
async def add_followup(
    task_id: int,
    body: FollowupBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    try:
        result = await asyncio.to_thread(
            store.add_followup,
            task_id,
            message=body.message,
            event_type=body.event_type,
            status=body.status,
            actor=body.actor,
            inbound_id=body.inbound_id,
            payload=body.payload,
        )
    except (TaskValidationError, TaskConflictError, KeyError) as exc:
        raise _http_from_store_error(exc) from exc
    board = await _board_sync(request, store).sync_followup(
        result["task"], result["event"]
    )
    refreshed = await asyncio.to_thread(store.get_task, task_id)
    return {
        "ok": True,
        "task": refreshed or result["task"],
        "event": result["event"],
        "board_sync": board,
    }


@router.post("/tasks/{task_id}/board-messages")
async def post_board_message(
    task_id: int,
    body: BoardMessageBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    """
    Post to the ledger thread only — does not create a task followup/event.
    For agent Q&A visible in 台账 without recording task progress.
    """
    check_token(authorization)
    task = await asyncio.to_thread(store.get_task, task_id, events_limit=1)
    if not task:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    key = (body.idempotency_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="idempotency_key is required")
    board = await _board_sync(request, store).post_board_message(
        task, text, idempotency_key=key, markdown=body.markdown
    )
    if not board.get("ok") and not board.get("skipped"):
        raise HTTPException(
            status_code=502,
            detail=board.get("error") or "board message failed",
        )
    print(
        f"[board-message] task={task_id} actor={body.actor} "
        f"ok={board.get('ok')} mid={board.get('message_id')} key={key}",
        flush=True,
    )
    return {"ok": True, "board": board, "task_id": task_id}


@router.post("/tasks/{task_id}/clues")
async def upsert_clue(
    task_id: int,
    body: UpsertClueBody,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    try:
        result = await asyncio.to_thread(
            store.upsert_clue,
            task_id,
            kind=body.kind,
            ref_key=body.ref_key,
            one_liner=body.one_liner,
            relevance=body.relevance,
            demote=body.demote,
            actor=body.actor,
            extra=body.extra,
        )
    except (TaskValidationError, KeyError) as exc:
        raise _http_from_store_error(exc) from exc
    return {
        "ok": True,
        "clue": result["clue"],
        "created": result["created"],
    }


@router.get("/tasks/{task_id}/clues")
async def list_task_clues(
    task_id: int,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
    min_relevance: Optional[str] = Query(default=None),
):
    check_token(authorization)
    try:
        clues = await asyncio.to_thread(
            store.list_clues, task_id, min_relevance=min_relevance
        )
    except (TaskValidationError, KeyError) as exc:
        raise _http_from_store_error(exc) from exc
    return {"ok": True, "clues": clues}


@router.get("/tasks/{task_id}/clues/{clue_id}")
async def get_task_clue(
    task_id: int,
    clue_id: int,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    clue = await asyncio.to_thread(store.get_clue, clue_id, task_id=task_id)
    if not clue:
        raise HTTPException(
            status_code=404,
            detail=f"clue {clue_id} not found on task {task_id}",
        )
    return {"ok": True, "clue": clue}


@router.get("/clues")
async def find_clues(
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
    kind: str = Query(...),
    ref_key: str = Query(...),
    min_relevance: Optional[str] = Query(default=None),
):
    check_token(authorization)
    try:
        clues = await asyncio.to_thread(
            store.find_clues,
            kind=kind,
            ref_key=ref_key,
            min_relevance=min_relevance,
        )
    except TaskValidationError as exc:
        raise _http_from_store_error(exc) from exc
    return {"ok": True, "clues": clues}


@router.put("/chat-projects/{chat_id}")
async def upsert_chat_project(
    chat_id: str,
    body: ChatProjectBody,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    try:
        item = await asyncio.to_thread(
            store.upsert_chat_project,
            chat_id,
            body.project_id,
            note=body.note,
        )
    except TaskValidationError as exc:
        raise _http_from_store_error(exc) from exc
    return {"ok": True, "item": item}


@router.get("/chat-projects/{chat_id}")
async def get_chat_project(
    chat_id: str,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    item = await asyncio.to_thread(store.get_chat_project, chat_id)
    if not item:
        raise HTTPException(
            status_code=404, detail=f"chat_project {chat_id} not found"
        )
    return {"ok": True, "item": item}


@router.delete("/chat-projects/{chat_id}")
async def delete_chat_project(
    chat_id: str,
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    ok = await asyncio.to_thread(store.delete_chat_project, chat_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail=f"chat_project {chat_id} not found"
        )
    return {"ok": True}


@router.get("/chat-projects")
async def list_chat_projects(
    authorization: Optional[str] = Header(default=None),
    store: TaskStore = Depends(get_task_store),
):
    check_token(authorization)
    items = await asyncio.to_thread(store.list_chat_projects)
    return {"ok": True, "items": items}
