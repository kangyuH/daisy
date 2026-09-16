from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.infra.db import connect_sync

TZ_CN = timezone(timedelta(hours=8))

STATUS_REGISTERING = "registering"
STATUS_READY = "ready"
STATUS_ERROR = "error"


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _row_bot(row: Any, *, include_secret: bool = False) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    if not include_secret:
        d.pop("app_secret", None)
        d["has_secret"] = True
    return d


class BotStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path

    def _connect(self):
        return connect_sync(self._db_path)

    def count_bots(self) -> int:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT COUNT(*) AS n FROM bots")
            return int(cur.fetchone()["n"])
        finally:
            conn.close()

    def next_bot_id(self) -> str:
        """Allocate next numeric id as string: 1, 2, 3, ... (non-numeric ids ignored)."""
        conn = self._connect()
        try:
            cur = conn.execute("SELECT id FROM bots")
            max_n = 0
            for row in cur.fetchall():
                s = str(row["id"]).strip()
                if s.isdigit():
                    max_n = max(max_n, int(s))
            return str(max_n + 1)
        finally:
            conn.close()

    def list_bots(self, *, include_secret: bool = False) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM bots ORDER BY created_at ASC")
            bots = [_row_bot(r, include_secret=include_secret) for r in cur.fetchall()]
            for b in bots:
                b["chats"] = self._chats_for(conn, b["id"])
            return bots
        finally:
            conn.close()

    def get_bot(self, bot_id: str, *, include_secret: bool = False) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
            row = cur.fetchone()
            if not row:
                return None
            bot = _row_bot(row, include_secret=include_secret)
            bot["chats"] = self._chats_for(conn, bot_id)
            return bot
        finally:
            conn.close()

    def get_bot_by_app_id(self, app_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM bots WHERE app_id = ?", (app_id,))
            row = cur.fetchone()
            return _row_bot(row, include_secret=True) if row else None
        finally:
            conn.close()

    def _chats_for(self, conn, bot_id: str) -> list[str]:
        cur = conn.execute(
            "SELECT chat_id FROM bot_chats WHERE bot_id = ? ORDER BY bound_at ASC",
            (bot_id,),
        )
        return [str(r["chat_id"]) for r in cur.fetchall()]

    def chats_for_bot(self, bot_id: str) -> list[str]:
        conn = self._connect()
        try:
            return self._chats_for(conn, bot_id)
        finally:
            conn.close()

    def get_chat_binding(self, chat_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM bot_chats WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def upsert_bot(
        self,
        *,
        bot_id: str,
        name: str,
        app_id: str,
        app_secret: str,
        open_id: Optional[str] = None,
        self_open_id: Optional[str] = None,
        enabled: bool = True,
        status: str = STATUS_REGISTERING,
        last_error: Optional[str] = None,
        event_key: str = "im.message.receive_v1",
    ) -> dict[str, Any]:
        now = _now_iso()
        conn = self._connect()
        try:
            cur = conn.execute("SELECT id FROM bots WHERE id = ?", (bot_id,))
            exists = cur.fetchone() is not None
            if exists:
                conn.execute(
                    """
                    UPDATE bots SET
                        name = ?, app_id = ?, app_secret = ?,
                        open_id = COALESCE(?, open_id),
                        self_open_id = COALESCE(?, self_open_id),
                        enabled = ?, status = ?, last_error = ?,
                        event_key = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        app_id,
                        app_secret,
                        open_id,
                        self_open_id,
                        1 if enabled else 0,
                        status,
                        last_error,
                        event_key,
                        now,
                        bot_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO bots (
                        id, name, app_id, app_secret, open_id, self_open_id,
                        enabled, status, last_error, event_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bot_id,
                        name,
                        app_id,
                        app_secret,
                        open_id,
                        self_open_id,
                        1 if enabled else 0,
                        status,
                        last_error,
                        event_key,
                        now,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        bot = self.get_bot(bot_id, include_secret=False)
        assert bot is not None
        return bot

    def update_bot_fields(self, bot_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "name",
            "open_id",
            "self_open_id",
            "enabled",
            "status",
            "last_error",
            "app_secret",
            "event_key",
        }
        sets = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            bot = self.get_bot(bot_id)
            if not bot:
                raise KeyError(bot_id)
            return bot
        sets.append("updated_at = ?")
        vals.append(_now_iso())
        vals.append(bot_id)
        conn = self._connect()
        try:
            conn.execute(f"UPDATE bots SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()
        bot = self.get_bot(bot_id)
        if not bot:
            raise KeyError(bot_id)
        return bot

    def bind_chat(
        self,
        bot_id: str,
        chat_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if not self.get_bot(bot_id, include_secret=False):
            raise KeyError(f"bot {bot_id} not found")
        existing = self.get_chat_binding(chat_id)
        if existing and existing["bot_id"] != bot_id:
            if not force:
                raise PermissionError(
                    f"chat {chat_id} already bound to bot {existing['bot_id']}"
                )
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO bot_chats (chat_id, bot_id, bound_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    bound_at = excluded.bound_at
                """,
                (chat_id, bot_id, now),
            )
            conn.commit()
        finally:
            conn.close()
        return {"chat_id": chat_id, "bot_id": bot_id, "bound_at": now, "forced": bool(existing and existing["bot_id"] != bot_id)}

    def enabled_bots_full(self) -> list[dict[str, Any]]:
        """Bots with secrets for runtime start."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM bots WHERE enabled = 1 AND status = ? ORDER BY created_at ASC",
                (STATUS_READY,),
            )
            out = []
            for row in cur.fetchall():
                bot = _row_bot(row, include_secret=True)
                bot["chats"] = self._chats_for(conn, bot["id"])
                out.append(bot)
            return out
        finally:
            conn.close()
