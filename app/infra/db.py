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
CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL DEFAULT 'inbound',
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    claim_token TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_items_idempotency
    ON queue_items(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_queue_items_pending
    ON queue_items(queue, status, created_at);

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

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    one_liner TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'noted',
    bot_id TEXT,
    chat_id TEXT,
    thread_id TEXT,
    project_id TEXT,
    workspace_path TEXT NOT NULL,
    created_from_inbound_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    board_chat_id TEXT,
    board_message_id TEXT,
    board_thread_id TEXT,
    board_sync_error TEXT,
    agent_session_id TEXT,
    agent_phase TEXT,
    agent_pgid INTEGER,
    agent_run_id TEXT,
    agent_run_started_at TEXT,
    agent_lease_until TEXT,
    agent_pid INTEGER,
    agent_proc_start TEXT,
    agent_last_command_message_id TEXT,
    agent_typing_message_id TEXT,
    agent_typing_reaction_id TEXT,
    agent_typing_bot_id TEXT,
    plan_doc_token TEXT,
    contract_revision INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
    ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_status
    ON tasks(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_thread
    ON tasks(thread_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_bot
    ON tasks(bot_id);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT NOT NULL DEFAULT 'api',
    inbound_id INTEGER,
    created_at TEXT NOT NULL,
    payload_json TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events(task_id, created_at);

CREATE TABLE IF NOT EXISTS chat_projects (
    chat_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dispatch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id INTEGER NOT NULL UNIQUE,
    decision TEXT NOT NULL,
    task_id INTEGER,
    reason TEXT NOT NULL,
    evidence_json TEXT,
    actor TEXT NOT NULL DEFAULT 'dispatcher',
    created_at TEXT NOT NULL,
    ack_status TEXT,
    ack_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_dispatch_runs_created
    ON dispatch_runs(created_at);

CREATE TABLE IF NOT EXISTS task_clues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_key TEXT NOT NULL,
    one_liner TEXT NOT NULL DEFAULT '',
    relevance TEXT NOT NULL DEFAULT 'related',
    actor TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra_json TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    UNIQUE (task_id, kind, ref_key)
);
CREATE INDEX IF NOT EXISTS idx_task_clues_task
    ON task_clues(task_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_clues_ref
    ON task_clues(kind, ref_key);
"""


def db_path() -> Path:
    raw = os.environ.get("GATEWAY_DB_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


def init_db_sync(path: Path | None = None) -> Path:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        # WAL once at init — avoid PRAGMA journal_mode on every connect (slow on NFS).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return p


def _add_columns_if_missing(
    conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]
) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {str(row[1]) for row in cur.fetchall()}
    for col, decl in columns:
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Existing DBs: ADD COLUMN / indexes for fields introduced after first schema."""
    _migrate_tasks_columns(conn)
    _add_columns_if_missing(
        conn,
        "queue_items",
        [
            ("claim_token", "TEXT"),
            ("lease_until", "TEXT"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("not_before", "TEXT"),
        ],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_items_lease "
        "ON queue_items(queue, status, lease_until)"
    )
    _add_columns_if_missing(
        conn,
        "dispatch_runs",
        [
            ("ack_status", "TEXT"),
            ("ack_error", "TEXT"),
        ],
    )
    _maybe_unique_inbound_index(conn)


def _maybe_unique_inbound_index(conn: sqlite3.Connection) -> None:
    """Unique created_from_inbound_id when history has no duplicates."""
    cur = conn.execute(
        """
        SELECT created_from_inbound_id, COUNT(*) AS n
        FROM tasks
        WHERE created_from_inbound_id IS NOT NULL
        GROUP BY created_from_inbound_id
        HAVING n > 1
        LIMIT 1
        """
    )
    if cur.fetchone():
        print(
            "[db] skip unique index on tasks.created_from_inbound_id "
            "(duplicate rows already present)",
            flush=True,
        )
        return
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_created_from_inbound "
        "ON tasks(created_from_inbound_id) "
        "WHERE created_from_inbound_id IS NOT NULL"
    )


def _migrate_tasks_columns(conn: sqlite3.Connection) -> None:
    """Existing DBs: ADD COLUMN for fields introduced after first tasks schema."""
    _add_columns_if_missing(
        conn,
        "tasks",
        [
            ("bot_id", "TEXT"),
            ("board_chat_id", "TEXT"),
            ("board_message_id", "TEXT"),
            ("board_thread_id", "TEXT"),
            ("board_sync_error", "TEXT"),
            ("agent_session_id", "TEXT"),
            ("agent_phase", "TEXT"),
            ("agent_pgid", "INTEGER"),
            ("agent_run_id", "TEXT"),
            ("agent_run_started_at", "TEXT"),
            ("agent_lease_until", "TEXT"),
            ("agent_pid", "INTEGER"),
            ("agent_proc_start", "TEXT"),
            ("agent_last_command_message_id", "TEXT"),
            ("agent_typing_message_id", "TEXT"),
            ("agent_typing_reaction_id", "TEXT"),
            ("agent_typing_bot_id", "TEXT"),
            ("plan_doc_token", "TEXT"),
            ("contract_revision", "INTEGER NOT NULL DEFAULT 0"),
        ],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_bot ON tasks(bot_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_board_thread "
        "ON tasks(board_thread_id)"
    )


def connect_sync(path: Path | str | None = None) -> sqlite3.Connection:
    if path is None:
        p = db_path()
    else:
        p = Path(path)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
