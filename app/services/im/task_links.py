from __future__ import annotations

import json
from contextlib import closing
from typing import Any

from app.infra.db import connect_sync


class MessageTaskStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def record_outbound(self, message_id: str, task_id: int) -> bool:
        """Return false if the message is already linked to a different task."""
        with closing(connect_sync(self._db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO im_message_tasks (message_id, task_id)
                VALUES (?, ?) ON CONFLICT (message_id) DO NOTHING
                """,
                (message_id, task_id),
            )
            row = conn.execute(
                "SELECT task_id FROM im_message_tasks WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            return row["task_id"] == task_id

    def find_inbound_task(self, inbound_id: int) -> int | None:
        with closing(connect_sync(self._db_path)) as conn:
            row = conn.execute(
                """
                SELECT tasks.id
                FROM dispatch_runs d JOIN tasks ON tasks.id = d.task_id
                WHERE d.inbound_id = ? AND d.decision IN ('create', 'followup')
                """,
                (inbound_id,),
            ).fetchone()
            return row["id"] if row else None

    def find_refs(self, message_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
        if not message_ids:
            return {}

        with closing(connect_sync(self._db_path)) as conn:
            rows = conn.execute(
                """
                WITH requested AS (SELECT value FROM json_each(?)),
                links AS (
                    SELECT message_id, task_id, 'outbound' AS source
                    FROM im_message_tasks
                    WHERE message_id IN requested
                    UNION
                    SELECT json_extract(q.payload, '$.message_id'), d.task_id, 'inbound'
                    FROM queue_items q
                    JOIN dispatch_runs d ON d.inbound_id = q.id
                    WHERE q.queue = 'inbound' AND json_valid(q.payload)
                      AND json_extract(q.payload, '$.message_id') IN requested
                      AND d.decision IN ('create', 'followup')
                )
                SELECT links.message_id, links.task_id, links.source, tasks.status
                FROM links JOIN tasks ON tasks.id = links.task_id
                ORDER BY links.message_id, links.task_id, links.source
                """,
                (json.dumps(sorted(message_ids)),),
            ).fetchall()

        refs: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            ref = dict(row)
            refs.setdefault(ref.pop("message_id"), []).append(ref)
        return refs
