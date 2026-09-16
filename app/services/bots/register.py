from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from app.services.bots.store import STATUS_ERROR, STATUS_READY, STATUS_REGISTERING, BotStore
from app.core.settings import (
    auth_user_open_id,
    calibration_chat_id,
    user_cli_profile_args,
)
from app.services.bots.models import BotConfig
from app.infra.lark.cli import LarkCliError, run_cli_async
from app.infra.events.manager import BotManager


class RegisterError(Exception):
    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


def _user_args() -> list[str]:
    return ["--as", "user", *user_cli_profile_args()]


async def invite_bot_to_calibration(app_id: str, chat_id: str) -> dict[str, Any]:
    args = [
        "im",
        "chat.members",
        "create",
        "--params",
        json.dumps(
            {
                "chat_id": chat_id,
                "member_id_type": "app_id",
                "succeed_type": 1,
            }
        ),
        "--data",
        json.dumps({"id_list": [app_id]}),
        *_user_args(),
    ]
    try:
        return await run_cli_async(args)
    except LarkCliError as exc:
        combined = f"{exc.stdout} {exc.stderr} {exc}"
        if any(
            x in combined.lower()
            for x in ("already", "exist", "duplicate", "in the chat", "已在")
        ):
            return {"ok": True, "skipped": True, "warning": combined[:500]}
        raise RegisterError(
            f"invite bot to calibration chat failed: {exc}",
            details={"stdout": exc.stdout, "stderr": exc.stderr},
        ) from exc


async def resolve_bot_open_id_from_members(app_id: str, chat_id: str) -> Optional[str]:
    bots = await _list_calib_bots(chat_id)
    for item in bots:
        member_id = str(
            item.get("member_id") or item.get("open_id") or item.get("id") or ""
        ).strip()
        item_app = str(item.get("app_id") or "")
        raw = json.dumps(item, ensure_ascii=False)
        if item_app == app_id and member_id.startswith("ou_"):
            return member_id
        if app_id in raw and member_id.startswith("ou_"):
            return member_id
    if len(bots) == 1:
        mid = str(bots[0].get("member_id") or bots[0].get("open_id") or "").strip()
        if mid.startswith("ou_"):
            return mid
    return None


async def resolve_bot_name_from_members(app_id: str, chat_id: str) -> Optional[str]:
    """Display name from calibration chat bot members (by app_id)."""
    bots = await _list_calib_bots(chat_id)
    for item in bots:
        if str(item.get("app_id") or "") != app_id:
            continue
        name = str(item.get("name") or "").strip()
        if name:
            return name
    return None


async def _list_calib_bots(chat_id: str) -> list[dict[str, Any]]:
    args = [
        "im",
        "+chat-members-list",
        "--chat-id",
        chat_id,
        *_user_args(),
    ]
    result = await run_cli_async(args)
    data = result.get("data") or result
    bots = []
    if isinstance(data, dict):
        bots = data.get("bots") or []
    return [b for b in bots if isinstance(b, dict)]


async def send_calibrate_at_self_and_bot(
    chat_id: str, user_open_id: str, bot_open_id_for_send: str
) -> dict[str, Any]:
    text = (
        f'<at user_id="{user_open_id}"></at> '
        f'<at user_id="{bot_open_id_for_send}"></at> calibrate'
    )
    args = [
        "im",
        "+messages-send",
        "--chat-id",
        chat_id,
        "--text",
        text,
        *_user_args(),
    ]
    return await run_cli_async(args)


def _pick_open_ids_from_calib(
    mentions: list[dict],
    *,
    bot_name: str,
) -> tuple[Optional[str], Optional[str]]:
    """Return (bot_open_id, self_open_id) from receive_v1 mention objects."""
    bot_oid = None
    self_oid = None
    for m in mentions:
        mid = str(m.get("id") or m.get("open_id") or "").strip()
        name = str(m.get("name") or "")
        if not mid.startswith("ou_"):
            continue
        if bot_name and (name == bot_name or bot_name in name or name in bot_name):
            bot_oid = mid
        else:
            # first non-bot mention treated as self
            if self_oid is None:
                self_oid = mid
    # if name match failed but exactly 2 mentions, assign heuristically later
    return bot_oid, self_oid


async def register_bot(
    *,
    app_id: str,
    app_secret: str,
    bot_id: Optional[str],
    name: Optional[str],
    store: BotStore,
    manager: BotManager,
) -> dict[str, Any]:
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if not app_id or not app_secret:
        raise RegisterError("app_id and app_secret are required")

    existing = store.get_bot_by_app_id(app_id)
    # Same app_id → reuse id; otherwise allocate numeric id (1,2,3…). Optional override via bot_id.
    bid = (bot_id or "").strip() or (existing["id"] if existing else store.next_bot_id())
    if bid == app_id:
        raise RegisterError("id must differ from app_id (lark-cli profile name constraint)")

    name_override = (name or "").strip() or None
    bname = name_override or (existing.get("name") if existing else None) or bid
    calib = calibration_chat_id()

    store.upsert_bot(
        bot_id=bid,
        name=bname,
        app_id=app_id,
        app_secret=app_secret,
        enabled=True,
        status=STATUS_REGISTERING,
        last_error=None,
    )

    consumer = None
    try:
        await invite_bot_to_calibration(app_id, calib)

        if not name_override:
            fetched = await resolve_bot_name_from_members(app_id, calib)
            if fetched:
                bname = fetched
                store.update_bot_fields(bid, name=bname)

        cfg = BotConfig(
            id=bid,
            name=bname,
            app_id=app_id,
            app_secret=app_secret,
            open_id=None,
            self_open_id=None,
            allowed_chats=[],
        )
        consumer = await manager.start_bot(cfg, wait_ready=True)

        bot_oid_send = await resolve_bot_open_id_from_members(app_id, calib)
        if not bot_oid_send:
            raise RegisterError(
                "could not resolve bot open_id from calibration chat members (for @bot send)"
            )

        user_oid = auth_user_open_id()
        if not user_oid:
            raise RegisterError(
                "could not resolve user open_id from lark-cli auth status "
                "(need user OAuth for calibrate send)"
            )

        wait_task = asyncio.create_task(consumer.wait_calibration_mentions(timeout=30.0))
        await asyncio.sleep(0.5)
        await send_calibrate_at_self_and_bot(calib, user_oid, bot_oid_send)
        try:
            mentions = await wait_task
        except asyncio.TimeoutError as exc:
            raise RegisterError(
                "timeout waiting for calibration @self/@bot event from bot consume"
            ) from exc

        bot_oid, self_oid = _pick_open_ids_from_calib(mentions, bot_name=bname)
        if not bot_oid or not self_oid:
            # fallback: two ou_ ids — prefer members id only if it appears in event
            ids = []
            for m in mentions:
                mid = str(m.get("id") or "").strip()
                if mid.startswith("ou_"):
                    ids.append(mid)
            ids = list(dict.fromkeys(ids))
            if not bot_oid and bot_oid_send in ids:
                bot_oid = bot_oid_send
            if not self_oid:
                for mid in ids:
                    if mid != bot_oid:
                        self_oid = mid
                        break
            if not bot_oid and len(ids) >= 2:
                # last resort: non-self is bot
                bot_oid = next((x for x in ids if x != self_oid), None)

        if not bot_oid or not self_oid:
            raise RegisterError(
                "calibration event missing bot/self open_id",
                details={"mentions": mentions, "bot_name": bname},
            )

        consumer.set_open_ids(open_id=bot_oid, self_open_id=self_oid)

        bot = store.upsert_bot(
            bot_id=bid,
            name=bname,
            app_id=app_id,
            app_secret=app_secret,
            open_id=bot_oid,
            self_open_id=self_oid,
            enabled=True,
            status=STATUS_READY,
            last_error=None,
        )
        manager.update_chats(bid, bot.get("chats") or [])
        return bot
    except Exception as exc:
        err = str(exc)
        try:
            store.update_bot_fields(bid, status=STATUS_ERROR, last_error=err[:1000])
        except Exception:
            pass
        if consumer:
            try:
                await manager.stop_bot(bid)
            except Exception:
                pass
        if isinstance(exc, RegisterError):
            raise
        if isinstance(exc, LarkCliError):
            raise RegisterError(
                err,
                details={"stdout": exc.stdout, "stderr": exc.stderr},
            ) from exc
        raise RegisterError(err) from exc
