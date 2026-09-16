from __future__ import annotations

import sys

# khu_py310_venv ships without stdlib _sqlite3; prefer bundled pysqlite3.
try:
    import sqlite3
except ModuleNotFoundError:  # pragma: no cover
    import pysqlite3 as sqlite3

    sys.modules["sqlite3"] = sqlite3

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "gateway.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL DEFAULT 'inbound',
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_idempotency
    ON queue_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON queue_jobs(queue, status, created_at);

CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    app_id TEXT NOT NULL UNIQUE,
    app_secret TEXT NOT NULL,
    open_id TEXT,
    self_open_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'registering',
    last_error TEXT,
    event_key TEXT NOT NULL DEFAULT 'im.message.receive_v1',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_chats (
    chat_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES bots(id)
);
CREATE INDEX IF NOT EXISTS idx_bot_chats_bot ON bot_chats(bot_id);
"""


def db_path() -> Path:
    raw = os.environ.get("GATEWAY_DB_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


def init_db_sync(path: Path | None = None) -> Path:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return p


def connect_sync(path: Path | str | None = None) -> sqlite3.Connection:
    if path is None:
        p = db_path()
    else:
        p = Path(path)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
