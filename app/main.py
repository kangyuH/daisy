from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from app.api.router import api_router
from app.core.settings import (
    auth_user_open_id,
    get_settings,
    load_dotenv,
    require_calibration_chat_id,
)
from app.infra.db import db_path, init_db_sync
from app.infra.events.manager import BotManager
from app.services.bots.models import load_bots_from_db
from app.services.bots.store import BotStore
from app.services.dispatcher.store import DispatchRunStore
from app.services.queue.service import ItemQueue
from app.services.tasks.store import TaskStore
from app.services.tasks.workspace import workspace_root


class StubBotManager:
    """No-op manager for tests (no event consume)."""

    def __init__(self) -> None:
        self.consumers: dict = {}

    async def start_all(self, bots) -> None:
        return None

    async def start_bot(self, bot, *, wait_ready: bool = True):
        return None

    async def stop_bot(self, bot_id: str) -> None:
        return None

    async def stop_all(self) -> None:
        return None

    def update_chats(self, bot_id: str, chats: list[str]) -> None:
        return None

    def health(self) -> dict:
        return {"ok": True, "bots": [], "ts": ""}


def create_app(*, testing: bool = False) -> FastAPI:
    load_dotenv()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path = init_db_sync()
        app.state.db_path = str(path)
        app.state.queue = ItemQueue(str(path))
        store = BotStore(str(path))
        app.state.bot_store = store
        ws_root = workspace_root()
        ws_root.mkdir(parents=True, exist_ok=True)
        app.state.task_store = TaskStore(str(path), workspace_root=str(ws_root))
        app.state.dispatch_run_store = DispatchRunStore(str(path))
        app.state.task_board_enabled = not testing
        app.state.auth_user_open_id = None

        if testing:
            from app.core.settings import calibration_chat_id

            app.state.calibration_chat_id = calibration_chat_id()
            app.state.manager = StubBotManager()
            app.state.bots = []
            yield
            return

        app.state.calibration_chat_id = require_calibration_chat_id()

        # Board posts use CLI default-app bot (--as bot, no --profile);
        # user open_id is for @ in the board thread opener.
        oid = auth_user_open_id()
        app.state.auth_user_open_id = oid
        print(
            "[gateway] task board uses CLI default bot (--as bot, no --profile)",
            flush=True,
        )
        if oid:
            print(
                f"[gateway] board thread will @ user open_id={oid}",
                flush=True,
            )
        else:
            print(
                "[gateway] NOTE: lark-cli user identity unavailable; "
                "board thread will be created without @user",
                flush=True,
            )

        bots = load_bots_from_db(store)
        app.state.bots = bots
        manager = BotManager()
        app.state.manager = manager
        try:
            await manager.start_all(bots)
        except Exception:
            await manager.stop_all()
            app.state.manager = None
            raise
        try:
            yield
        finally:
            await manager.stop_all()
            app.state.manager = None

    application = FastAPI(title="lark-superbot", lifespan=lifespan)
    application.include_router(api_router)
    return application


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    host = os.environ.get("HOST", settings.host)
    port = int(os.environ.get("PORT", str(settings.port)))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
