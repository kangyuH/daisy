from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.deps import bot_profile, check_token
from app.core.settings import calibration_chat_id
from app.infra.events.manager import BotManager
from app.infra.lark.cli import LarkCliError
from app.services.bots.models import load_bots_from_db
from app.services.bots.register import RegisterError, register_bot
from app.services.bots.store import BotStore
from app.core.deps import get_bot_store, get_manager

router = APIRouter(prefix="/bots", tags=["bots"])


class RegisterBody(BaseModel):
    app_id: str
    app_secret: str
    id: Optional[str] = Field(
        default=None, description="optional; omit to auto-allocate 1,2,3… (reuse if app_id exists)"
    )
    name: Optional[str] = Field(
        default=None, description="optional; omit to fetch from calibration chat members"
    )


class BindChatBody(BaseModel):
    chat_id: str
    force: bool = False


@router.post("/register")
async def bots_register(
    body: RegisterBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    store: BotStore = Depends(get_bot_store),
    manager: BotManager = Depends(get_manager),
):
    check_token(authorization)
    try:
        bot = await register_bot(
            app_id=body.app_id,
            app_secret=body.app_secret,
            bot_id=body.id,
            name=body.name,
            store=store,
            manager=manager,
        )
    except RegisterError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": str(exc), "details": exc.details},
        ) from exc
    request.app.state.bots = load_bots_from_db(store)
    return {"ok": True, "bot": bot, "calibration_chat_id": calibration_chat_id()}


@router.get("")
async def bots_list(
    authorization: Optional[str] = Header(default=None),
    store: BotStore = Depends(get_bot_store),
):
    check_token(authorization)
    return {"ok": True, "bots": store.list_bots()}


@router.get("/{bot_id}")
async def bots_get(
    bot_id: str,
    authorization: Optional[str] = Header(default=None),
    store: BotStore = Depends(get_bot_store),
):
    check_token(authorization)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail=f"bot {bot_id} not found")
    return {"ok": True, "bot": bot}


@router.post("/{bot_id}/chats")
async def bots_bind_chat(
    bot_id: str,
    body: BindChatBody,
    authorization: Optional[str] = Header(default=None),
    store: BotStore = Depends(get_bot_store),
    manager: BotManager = Depends(get_manager),
):
    check_token(authorization)
    chat_id = body.chat_id.strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")
    calib = calibration_chat_id()
    if chat_id == calib:
        raise HTTPException(
            status_code=400,
            detail=f"calibration chat {calib} cannot be used as business binding",
        )
    if not store.get_bot(bot_id):
        raise HTTPException(status_code=404, detail=f"bot {bot_id} not found")
    try:
        binding = store.bind_chat(bot_id, chat_id, force=body.force)
    except PermissionError as exc:
        existing = store.get_chat_binding(chat_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(exc),
                "chat_id": chat_id,
                "bound_bot_id": existing["bot_id"] if existing else None,
            },
        ) from exc
    chats = store.chats_for_bot(bot_id)
    manager.update_chats(bot_id, chats)
    return {"ok": True, "binding": binding, "chats": chats}
