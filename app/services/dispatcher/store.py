from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.infra.db import connect_sync

TZ_CN = timezone(timedelta(hours=8))

DECISIONS = frozenset({"create", "followup", "noop"})


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _row_to_run(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("evidence_json", None)
    if raw:
        try:
            d["evidence"] = json.loads(raw)
        except json.JSONDecodeError:
            d["evidence"] = raw
    else:
        d["evidence"] = None
    return d


class DispatchRunStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path

    def _connect(self):
        return connect_sync(self._db_path)

    def get_by_inbound(self, inbound_id: int) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM dispatch_runs WHERE inbound_id = ?",
                (int(inbound_id),),
            )
            row = cur.fetchone()
            return _row_to_run(row) if row else None
        finally:
            conn.close()

    def create_run(
        self,
        *,
        inbound_id: int,
        decision: str,
        reason: str,
        task_id: Optional[int] = None,
        evidence: Any = None,
        actor: str = "dispatcher",
    ) -> dict[str, Any]:
        """Insert dispatch run. If inbound_id already exists, return existing (idempotent)."""
        decision_s = (decision or "").strip()
        if decision_s not in DECISIONS:
            raise ValueError(
                f"invalid decision {decision!r}; allowed: {sorted(DECISIONS)}"
            )
        reason_s = (reason or "").strip()
        if not reason_s:
            raise ValueError("reason is required")
        actor_s = (actor or "dispatcher").strip() or "dispatcher"
        evidence_s = (
            json.dumps(evidence, ensure_ascii=False) if evidence is not None else None
        )
        now = _now_iso()
        inbound = int(inbound_id)

        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM dispatch_runs WHERE inbound_id = ?",
                (inbound,),
            )
            existing = cur.fetchone()
            if existing:
                run = _row_to_run(existing)
                run["deduped"] = True
                return run

            try:
                cur = conn.execute(
                    """
                    INSERT INTO dispatch_runs (
                        inbound_id, decision, task_id, reason,
                        evidence_json, actor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inbound,
                        decision_s,
                        int(task_id) if task_id is not None else None,
                        reason_s,
                        evidence_s,
                        actor_s,
                        now,
                    ),
                )
                conn.commit()
                run_id = int(cur.lastrowid)
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    cur = conn.execute(
                        "SELECT * FROM dispatch_runs WHERE inbound_id = ?",
                        (inbound,),
                    )
                    row = cur.fetchone()
                    if row:
                        run = _row_to_run(row)
                        run["deduped"] = True
                        return run
                raise

            cur = conn.execute(
                "SELECT * FROM dispatch_runs WHERE id = ?", (run_id,)
            )
            row = cur.fetchone()
            assert row is not None
            run = _row_to_run(row)
            run["deduped"] = False
            return run
        finally:
            conn.close()
