from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.settings import DEFAULT_EVENT_KEY


@dataclass
class BotConfig:
    id: str
    name: str
    app_id: str
    app_secret: str
    enabled: bool = True
    event_key: str = DEFAULT_EVENT_KEY
    open_id: Optional[str] = None
    self_open_id: Optional[str] = None
    allowed_chats: list[str] = field(default_factory=list)


def bot_row_to_config(row: dict[str, Any]) -> BotConfig:
    return BotConfig(
        id=str(row["id"]),
        name=str(row.get("name") or row["id"]),
        app_id=str(row["app_id"]),
        app_secret=str(row.get("app_secret") or ""),
        enabled=bool(row.get("enabled", True)),
        event_key=str(row.get("event_key") or DEFAULT_EVENT_KEY),
        open_id=(str(row["open_id"]).strip() if row.get("open_id") else None),
        self_open_id=(
            str(row["self_open_id"]).strip() if row.get("self_open_id") else None
        ),
        allowed_chats=list(row.get("chats") or []),
    )


def load_bots_from_db(store, *, enabled_ready_only: bool = True) -> list[BotConfig]:
    if enabled_ready_only:
        rows = store.enabled_bots_full()
    else:
        rows = []
        for b in store.list_bots(include_secret=False):
            full = store.get_bot(b["id"], include_secret=True)
            if full:
                rows.append(full)
    return [bot_row_to_config(r) for r in rows]
