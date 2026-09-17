from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request

from app.core.settings import get_settings
from app.infra.events.manager import BotManager
from app.services.bots.store import BotStore
from app.services.dispatcher.store import DispatchRunStore
from app.services.queue.service import ItemQueue
from app.services.tasks.store import TaskStore


def check_token(authorization: Optional[str] = Header(default=None)) -> None:
    token = get_settings().gateway_token
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="unauthorized")


def get_queue(request: Request) -> ItemQueue:
    q = getattr(request.app.state, "queue", None)
    if q is None:
        raise HTTPException(status_code=503, detail="queue not ready")
    return q


def get_bot_store(request: Request) -> BotStore:
    st = getattr(request.app.state, "bot_store", None)
    if st is None:
        raise HTTPException(status_code=503, detail="bot_store not ready")
    return st


def get_task_store(request: Request) -> TaskStore:
    st = getattr(request.app.state, "task_store", None)
    if st is None:
        raise HTTPException(status_code=503, detail="task_store not ready")
    return st


def get_dispatch_run_store(request: Request) -> DispatchRunStore:
    st = getattr(request.app.state, "dispatch_run_store", None)
    if st is None:
        raise HTTPException(status_code=503, detail="dispatch_run_store not ready")
    return st


def get_manager(request: Request) -> BotManager:
    m = getattr(request.app.state, "manager", None)
    if m is None:
        raise HTTPException(status_code=503, detail="manager not ready")
    return m


def default_bot_profile(request: Request) -> str:
    bots = getattr(request.app.state, "bots", None) or []
    if bots:
        return bots[0].id
    st = getattr(request.app.state, "bot_store", None)
    if st:
        listed = st.list_bots()
        if listed:
            return listed[0]["id"]
    return get_settings().default_bot_id


def bot_profile(request: Request, bot_id: Optional[str]) -> str:
    if bot_id:
        return bot_id
    return default_bot_profile(request)
