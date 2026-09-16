from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.bots.models import BotConfig
from app.infra.events.consumer import EventConsumer

TZ_CN = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


class BotManager:
    def __init__(self) -> None:
        self.consumers: dict[str, EventConsumer] = {}

    def get(self, bot_id: str) -> Optional[EventConsumer]:
        return self.consumers.get(bot_id)

    async def start_all(self, bots: list[BotConfig]) -> None:
        errors: list[str] = []
        for bot in bots:
            try:
                await self.start_bot(bot)
            except Exception as exc:
                errors.append(f"{bot.id}: {exc}")
                print(f"[gateway] failed to start bot {bot.id}: {exc}", flush=True)
        if bots and not self.consumers and errors:
            # All failed — still allow empty gateway for /bots/register
            print(
                f"[gateway] no bots running ({len(errors)} start errors); API still up",
                flush=True,
            )

    async def start_bot(self, bot: BotConfig, *, wait_ready: bool = True) -> EventConsumer:
        existing = self.consumers.get(bot.id)
        if existing:
            await existing.stop()
            self.consumers.pop(bot.id, None)

        consumer = EventConsumer(bot, allowed_chats=set(bot.allowed_chats))
        await consumer.prepare()
        await consumer.start(prepare=False)
        self.consumers[bot.id] = consumer
        if wait_ready:
            await consumer.wait_ready()
        return consumer

    async def stop_bot(self, bot_id: str) -> None:
        c = self.consumers.pop(bot_id, None)
        if c:
            await c.stop()

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(c.stop() for c in self.consumers.values()),
            return_exceptions=True,
        )
        self.consumers.clear()

    def update_chats(self, bot_id: str, chats: list[str]) -> None:
        c = self.consumers.get(bot_id)
        if c:
            c.set_allowed_chats(chats)

    def health(self) -> dict:
        bots = [c.status() for c in self.consumers.values()]
        all_running = bool(bots) and all(b["running"] for b in bots)
        # ok if no bots yet (empty gateway) or all running
        ok = True if not bots else all_running
        return {
            "ok": ok,
            "bots": bots,
            "ts": _now_iso(),
        }
