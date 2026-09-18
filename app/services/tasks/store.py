from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.infra.db import connect_sync
from app.services.tasks.models import (
    DEFAULT_CLUE_RELEVANCE,
    EVENT_CREATED,
    EVENT_NOTE,
    STATUS_NOTED,
    TaskConflictError,
    TaskValidationError,
    clue_relevance_rank,
    is_terminal,
    validate_clue_kind,
    validate_clue_relevance,
    validate_followup_event_type,
    validate_kind,
    validate_status,
)
from app.services.tasks.workspace import TaskWorkspace

TZ_CN = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _row_task(row: Any) -> dict[str, Any]:
    return dict(row)


def _row_event(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("payload_json", None)
    if raw:
        try:
            d["payload"] = json.loads(raw)
        except json.JSONDecodeError:
            d["payload"] = raw
    else:
        d["payload"] = None
    return d


def _row_chat_project(row: Any) -> dict[str, Any]:
    return dict(row)


def _row_clue(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("extra_json", None)
    if raw:
        try:
            d["extra"] = json.loads(raw)
        except json.JSONDecodeError:
            d["extra"] = raw
    else:
        d["extra"] = None
    return d


def _sorted_clues(clues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """relevance high→low, then updated_at newest first."""
    return sorted(
        clues,
        key=lambda c: (
            clue_relevance_rank(
                str(c.get("relevance") or DEFAULT_CLUE_RELEVANCE)
            ),
            str(c.get("updated_at") or ""),
        ),
        reverse=True,
    )


class TaskStore:
    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        workspace_root: Optional[str] = None,
    ) -> None:
        self._db_path = db_path
        self.workspace = TaskWorkspace(workspace_root)

    def _connect(self):
        return connect_sync(self._db_path)

    # --- chat_projects ---

    def upsert_chat_project(
        self,
        chat_id: str,
        project_id: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        cid = (chat_id or "").strip()
        pid = (project_id or "").strip()
        if not cid:
            raise TaskValidationError("chat_id is required")
        if not pid:
            raise TaskValidationError("project_id is required")
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO chat_projects (chat_id, project_id, bound_at, note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    bound_at = excluded.bound_at,
                    note = excluded.note
                """,
                (cid, pid, now, note or ""),
            )
            conn.commit()
            cur = conn.execute(
                "SELECT * FROM chat_projects WHERE chat_id = ?", (cid,)
            )
            return _row_chat_project(cur.fetchone())
        finally:
            conn.close()

    def get_chat_project(self, chat_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM chat_projects WHERE chat_id = ?",
                (chat_id.strip(),),
            )
            row = cur.fetchone()
            return _row_chat_project(row) if row else None
        finally:
            conn.close()

    def delete_chat_project(self, chat_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM chat_projects WHERE chat_id = ?",
                (chat_id.strip(),),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_chat_projects(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM chat_projects ORDER BY bound_at ASC"
            )
            return [_row_chat_project(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def resolve_project_id(
        self,
        *,
        chat_id: Optional[str],
        project_id: Optional[str],
    ) -> Optional[str]:
        if project_id is not None:
            pid = project_id.strip()
            return pid or None
        if not chat_id:
            return None
        bound = self.get_chat_project(chat_id)
        if not bound:
            return None
        return str(bound["project_id"])

    def resolve_bot_id(
        self,
        *,
        chat_id: Optional[str],
        bot_id: Optional[str],
    ) -> Optional[str]:
        """Explicit bot_id wins; else inherit from bot_chats for chat_id."""
        if bot_id is not None:
            bid = bot_id.strip()
            return bid or None
        if not chat_id:
            return None
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT bot_id FROM bot_chats WHERE chat_id = ?",
                (chat_id.strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return str(row["bot_id"])
        finally:
            conn.close()

    # --- tasks ---

    def create_task(
        self,
        *,
        title: str,
        kind: str,
        one_liner: str = "",
        bot_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        created_from_inbound_id: Optional[int] = None,
        actor: str = "api",
        message: Optional[str] = None,
    ) -> dict[str, Any]:
        title_s = (title or "").strip()
        if not title_s:
            raise TaskValidationError("title is required")
        kind_s = validate_kind(kind)
        status_s = validate_status(status) if status else STATUS_NOTED
        chat_s = (chat_id or "").strip() or None
        thread_s = (thread_id or "").strip() or None
        project_s = self.resolve_project_id(chat_id=chat_s, project_id=project_id)
        bot_s = self.resolve_bot_id(chat_id=chat_s, bot_id=bot_id)
        now = _now_iso()
        closed_at = now if is_terminal(status_s) else None
        actor_s = (actor or "api").strip() or "api"
        create_msg = (message or f"created task: {title_s}").strip()

        conn = self._connect()
        try:
            # placeholder workspace_path until id known
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    title, one_liner, kind, status, bot_id, chat_id, thread_id,
                    project_id, workspace_path, created_from_inbound_id,
                    created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title_s,
                    one_liner or "",
                    kind_s,
                    status_s,
                    bot_s,
                    chat_s,
                    thread_s,
                    project_s,
                    "",  # filled after id
                    created_from_inbound_id,
                    now,
                    now,
                    closed_at,
                ),
            )
            task_id = int(cur.lastrowid)
            ws_path = self.workspace.relative_path(task_id)
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (ws_path, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, event_type, message, from_status, to_status,
                    actor, inbound_id, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    EVENT_CREATED,
                    create_msg,
                    None,
                    status_s,
                    actor_s,
                    created_from_inbound_id,
                    now,
                    None,
                ),
            )
            conn.commit()
            task = self._get_task_conn(conn, task_id)
            event = self._latest_event_conn(conn, task_id)
        finally:
            conn.close()

        assert task is not None
        created_event = {
            "id": event["id"] if event else None,
            "task_id": task_id,
            "event_type": EVENT_CREATED,
            "message": create_msg,
            "from_status": None,
            "to_status": status_s,
            "actor": actor_s,
            "inbound_id": created_from_inbound_id,
            "created_at": now,
            "payload": None,
        }
        self.workspace.init_task(
            task_id,
            title=title_s,
            kind=kind_s,
            status=status_s,
            one_liner=one_liner or "",
            bot_id=bot_s,
            chat_id=chat_s,
            thread_id=thread_s,
            project_id=project_s,
            created_at=now,
            created_event=created_event,
        )
        task["events"] = [event] if event else []
        return task

    def get_task(
        self, task_id: int, *, events_limit: int = 50
    ) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            task = self._get_task_conn(conn, task_id)
            if not task:
                return None
            task["events"] = self._list_events_conn(
                conn, task_id, limit=events_limit
            )
            task["clues"] = self._list_clues_conn(conn, task_id)
            return task
        finally:
            conn.close()

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        bot_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status is not None:
            validate_status(status)
        if kind is not None:
            validate_kind(kind)
        lim = max(1, min(int(limit), 500))
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if bot_id:
            clauses.append("bot_id = ?")
            args.append(bot_id.strip())
        if chat_id:
            clauses.append("chat_id = ?")
            args.append(chat_id.strip())
        if thread_id:
            clauses.append("thread_id = ?")
            args.append(thread_id.strip())
        if project_id:
            clauses.append("project_id = ?")
            args.append(project_id.strip())
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM tasks {where} ORDER BY updated_at DESC LIMIT ?"
        args.append(lim)
        conn = self._connect()
        try:
            cur = conn.execute(sql, args)
            return [_row_task(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def add_followup(
        self,
        task_id: int,
        *,
        message: str,
        event_type: str = EVENT_NOTE,
        status: Optional[str] = None,
        actor: str = "api",
        inbound_id: Optional[int] = None,
        payload: Any = None,
    ) -> dict[str, Any]:
        msg = (message or "").strip()
        if not msg:
            raise TaskValidationError("message is required")
        et = validate_followup_event_type(event_type)
        actor_s = (actor or "api").strip() or "api"
        new_status = validate_status(status) if status is not None else None
        now = _now_iso()
        payload_s = (
            json.dumps(payload, ensure_ascii=False) if payload is not None else None
        )

        conn = self._connect()
        try:
            task = self._get_task_conn(conn, task_id)
            if not task:
                raise KeyError(f"task {task_id} not found")

            from_status = str(task["status"])
            to_status = from_status
            from_s: Optional[str] = None
            to_s: Optional[str] = None

            if new_status is not None:
                if new_status != from_status and is_terminal(from_status):
                    raise TaskConflictError(
                        f"task {task_id} is terminal ({from_status}); "
                        "status cannot change"
                    )
                to_status = new_status
                from_s = from_status
                to_s = to_status
                if is_terminal(to_status):
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = ?, updated_at = ?,
                            closed_at = COALESCE(closed_at, ?)
                        WHERE id = ?
                        """,
                        (to_status, now, now, task_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = ?, updated_at = ?, closed_at = NULL
                        WHERE id = ?
                        """,
                        (to_status, now, task_id),
                    )
            else:
                conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE id = ?",
                    (now, task_id),
                )

            cur = conn.execute(
                """
                INSERT INTO task_events (
                    task_id, event_type, message, from_status, to_status,
                    actor, inbound_id, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    et,
                    msg,
                    from_s,
                    to_s,
                    actor_s,
                    inbound_id,
                    now,
                    payload_s,
                ),
            )
            event_id = int(cur.lastrowid)
            conn.commit()
            task = self._get_task_conn(conn, task_id)
            event = self._get_event_conn(conn, event_id)
        finally:
            conn.close()

        assert task is not None and event is not None
        timeline = {
            "id": event["id"],
            "task_id": task_id,
            "event_type": et,
            "message": msg,
            "from_status": from_s,
            "to_status": to_s,
            "actor": actor_s,
            "inbound_id": inbound_id,
            "created_at": now,
            "payload": payload,
        }
        self.workspace.append_timeline(task_id, timeline)
        self.workspace.append_update(
            task_id,
            created_at=now,
            message=msg,
            event_type=et,
            from_status=from_s,
            to_status=to_s,
            actor=actor_s,
        )
        return {"task": task, "event": event}

    # --- clues ---

    def upsert_clue(
        self,
        task_id: int,
        *,
        kind: str,
        ref_key: str,
        one_liner: Optional[str] = None,
        relevance: Optional[str] = None,
        demote: bool = False,
        actor: str = "api",
        extra: Any = None,
    ) -> dict[str, Any]:
        kind_s = validate_clue_kind(kind)
        key_s = (ref_key or "").strip()
        if not key_s:
            raise TaskValidationError("ref_key is required")
        actor_s = (actor or "api").strip() or "api"
        now = _now_iso()
        extra_s = (
            json.dumps(extra, ensure_ascii=False) if extra is not None else None
        )
        new_rel = (
            validate_clue_relevance(relevance)
            if relevance is not None
            else None
        )

        conn = self._connect()
        try:
            task = self._get_task_conn(conn, task_id)
            if not task:
                raise KeyError(f"task {task_id} not found")

            cur = conn.execute(
                """
                SELECT * FROM task_clues
                WHERE task_id = ? AND kind = ? AND ref_key = ?
                """,
                (task_id, kind_s, key_s),
            )
            existing = cur.fetchone()
            created = existing is None

            if created:
                rel_s = new_rel or DEFAULT_CLUE_RELEVANCE
                one_s = (one_liner or "").strip() if one_liner is not None else ""
                cur = conn.execute(
                    """
                    INSERT INTO task_clues (
                        task_id, kind, ref_key, one_liner, relevance,
                        actor, created_at, updated_at, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        kind_s,
                        key_s,
                        one_s,
                        rel_s,
                        actor_s,
                        now,
                        now,
                        extra_s,
                    ),
                )
                clue_id = int(cur.lastrowid)
            else:
                old = _row_clue(existing)
                one_s = (
                    (one_liner or "").strip()
                    if one_liner is not None
                    else str(old.get("one_liner") or "")
                )
                old_rel = validate_clue_relevance(
                    str(old.get("relevance") or DEFAULT_CLUE_RELEVANCE)
                )
                if new_rel is None:
                    rel_s = old_rel
                elif (
                    not demote
                    and clue_relevance_rank(new_rel)
                    < clue_relevance_rank(old_rel)
                ):
                    rel_s = old_rel
                else:
                    rel_s = new_rel
                if extra is None:
                    extra_keep = existing["extra_json"]
                else:
                    extra_keep = extra_s
                clue_id = int(old["id"])
                conn.execute(
                    """
                    UPDATE task_clues
                    SET one_liner = ?, relevance = ?, actor = ?,
                        updated_at = ?, extra_json = ?
                    WHERE id = ?
                    """,
                    (one_s, rel_s, actor_s, now, extra_keep, clue_id),
                )

            conn.commit()
            clue = self._get_clue_conn(conn, clue_id)
            assert clue is not None
            clues_snapshot = self._list_clues_conn(conn, task_id)
        finally:
            conn.close()

        self.workspace.write_clues(task_id, clues_snapshot)
        return {"clue": clue, "created": created}

    def list_clues(
        self,
        task_id: int,
        *,
        min_relevance: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if not self._get_task_conn(conn, task_id):
                raise KeyError(f"task {task_id} not found")
            return self._list_clues_conn(
                conn, task_id, min_relevance=min_relevance
            )
        finally:
            conn.close()

    def get_clue(
        self, clue_id: int, *, task_id: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            clue = self._get_clue_conn(conn, clue_id)
            if not clue:
                return None
            if task_id is not None and int(clue["task_id"]) != int(task_id):
                return None
            return clue
        finally:
            conn.close()

    def find_clues(
        self,
        *,
        kind: str,
        ref_key: str,
        min_relevance: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        kind_s = validate_clue_kind(kind)
        key_s = (ref_key or "").strip()
        if not key_s:
            raise TaskValidationError("ref_key is required")
        min_rank: Optional[int] = None
        if min_relevance is not None:
            min_rank = clue_relevance_rank(min_relevance)

        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT c.*, t.title AS task_title, t.status AS task_status
                FROM task_clues c
                JOIN tasks t ON t.id = c.task_id
                WHERE c.kind = ? AND c.ref_key = ?
                """,
                (kind_s, key_s),
            )
            out: list[dict[str, Any]] = []
            for row in cur.fetchall():
                clue = _row_clue(row)
                clue["task_title"] = row["task_title"]
                clue["task_status"] = row["task_status"]
                if min_rank is not None:
                    if clue_relevance_rank(str(clue["relevance"])) < min_rank:
                        continue
                out.append(clue)
            return _sorted_clues(out)
        finally:
            conn.close()

    def _get_clue_conn(self, conn, clue_id: int) -> Optional[dict[str, Any]]:
        cur = conn.execute("SELECT * FROM task_clues WHERE id = ?", (clue_id,))
        row = cur.fetchone()
        return _row_clue(row) if row else None

    def _list_clues_conn(
        self,
        conn,
        task_id: int,
        *,
        min_relevance: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        min_rank: Optional[int] = None
        if min_relevance is not None:
            min_rank = clue_relevance_rank(min_relevance)
        cur = conn.execute(
            "SELECT * FROM task_clues WHERE task_id = ?",
            (task_id,),
        )
        clues = [_row_clue(r) for r in cur.fetchall()]
        if min_rank is not None:
            clues = [
                c
                for c in clues
                if clue_relevance_rank(str(c["relevance"])) >= min_rank
            ]
        return _sorted_clues(clues)

    def update_board_fields(
        self,
        task_id: int,
        *,
        board_chat_id: Optional[str] = None,
        board_message_id: Optional[str] = None,
        board_thread_id: Optional[str] = None,
        board_sync_error: Optional[str] = None,
        clear_sync_error: bool = False,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            task = self._get_task_conn(conn, task_id)
            if not task:
                raise KeyError(f"task {task_id} not found")
            sets: list[str] = []
            args: list[Any] = []
            if board_chat_id is not None:
                sets.append("board_chat_id = ?")
                args.append(board_chat_id.strip() or None)
            if board_message_id is not None:
                sets.append("board_message_id = ?")
                args.append(board_message_id.strip() or None)
            if board_thread_id is not None:
                sets.append("board_thread_id = ?")
                args.append(board_thread_id.strip() or None)
            if clear_sync_error:
                sets.append("board_sync_error = NULL")
            elif board_sync_error is not None:
                sets.append("board_sync_error = ?")
                args.append(board_sync_error[:2000])
            if not sets:
                return task
            sets.append("updated_at = ?")
            args.append(_now_iso())
            args.append(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                args,
            )
            conn.commit()
            updated = self._get_task_conn(conn, task_id)
            assert updated is not None
            return updated
        finally:
            conn.close()

    def _get_task_conn(self, conn, task_id: int) -> Optional[dict[str, Any]]:
        cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return _row_task(row) if row else None

    def _get_event_conn(self, conn, event_id: int) -> Optional[dict[str, Any]]:
        cur = conn.execute("SELECT * FROM task_events WHERE id = ?", (event_id,))
        row = cur.fetchone()
        return _row_event(row) if row else None

    def _latest_event_conn(
        self, conn, task_id: int
    ) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        row = cur.fetchone()
        return _row_event(row) if row else None

    def _list_events_conn(
        self, conn, task_id: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 500))
        cur = conn.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (task_id, lim),
        )
        # return chronological for readability
        rows = [_row_event(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
