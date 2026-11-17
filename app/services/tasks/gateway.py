from __future__ import annotations

import asyncio
from typing import Any

from app.services.tasks.board import TaskBoardSync
from app.services.tasks.models import EVENT_NOTE
from app.services.tasks.store import TaskStore


async def list_tasks(
    store: TaskStore,
    *,
    status: str | None = None,
    bot_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
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
    return {"ok": True, "tasks": tasks}


async def get_task(
    store: TaskStore, task_id: int, *, events_limit: int = 50
) -> dict[str, Any]:
    task = await asyncio.to_thread(
        store.get_task, task_id, events_limit=events_limit
    )
    if not task:
        raise KeyError(f"task {task_id} not found")
    return {"ok": True, "task": task}


async def append_followup(
    store: TaskStore,
    board: TaskBoardSync,
    task_id: int,
    *,
    message: str,
    event_type: str = EVENT_NOTE,
    status: str | None = None,
    actor: str = "api",
    inbound_id: int | None = None,
    payload: Any = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        store.add_followup,
        task_id,
        message=message,
        event_type=event_type,
        status=status,
        actor=actor,
        inbound_id=inbound_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    board_result = result.get("board_sync")
    if board_result is None:
        board_result = await board.sync_followup(result["task"], result["event"])
        if idempotency_key is not None and board_result.get("ok"):
            await asyncio.to_thread(
                store.record_followup_board_sync, int(result["event"]["id"]), board_result
            )
    refreshed = await asyncio.to_thread(store.get_task, task_id)
    return {
        "ok": True,
        "task": refreshed or result["task"],
        "event": result["event"],
        "board_sync": board_result,
    }
