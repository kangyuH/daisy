from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.infra.db import connect_sync

TZ_CN = timezone(timedelta(hours=8))

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _row_to_job(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("payload")
    if isinstance(raw, str):
        try:
            d["payload"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return d


class JobQueue:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path

    def _connect(self):
        return connect_sync(self._db_path)

    def _enqueue_sync(
        self,
        queue: str,
        payload: Any,
        *,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload_s = json.dumps(payload, ensure_ascii=False)
        created = _now_iso()
        conn = self._connect()
        try:
            if idempotency_key:
                cur = conn.execute(
                    "SELECT * FROM queue_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing:
                    job = _row_to_job(existing)
                    job["deduped"] = True
                    return job

            try:
                cur = conn.execute(
                    """
                    INSERT INTO queue_jobs
                        (queue, status, payload, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (queue, STATUS_PENDING, payload_s, idempotency_key, created),
                )
                conn.commit()
                job_id = cur.lastrowid
            except Exception as exc:
                # Unique race on idempotency_key
                if idempotency_key and "UNIQUE" in str(exc).upper():
                    cur = conn.execute(
                        "SELECT * FROM queue_jobs WHERE idempotency_key = ?",
                        (idempotency_key,),
                    )
                    existing = cur.fetchone()
                    if existing:
                        job = _row_to_job(existing)
                        job["deduped"] = True
                        return job
                raise

            cur = conn.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            assert row is not None
            job = _row_to_job(row)
            job["deduped"] = False
            return job
        finally:
            conn.close()

    def _claim_sync(
        self,
        queue: str,
        *,
        limit: int = 1,
        claimed_by: str = "worker",
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        claimed_at = _now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                SELECT id FROM queue_jobs
                WHERE queue = ? AND status = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (queue, STATUS_PENDING, limit),
            )
            ids = [int(r["id"]) for r in cur.fetchall()]
            if not ids:
                conn.commit()
                return []

            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""
                UPDATE queue_jobs
                SET status = ?, claimed_at = ?, claimed_by = ?
                WHERE id IN ({placeholders}) AND status = ?
                """,
                (STATUS_CLAIMED, claimed_at, claimed_by, *ids, STATUS_PENDING),
            )
            conn.commit()
            cur = conn.execute(
                f"SELECT * FROM queue_jobs WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
                ids,
            )
            return [_row_to_job(r) for r in cur.fetchall()]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ack_sync(self, job_id: int, *, error: Optional[str] = None) -> dict[str, Any]:
        finished = _now_iso()
        status = STATUS_FAILED if error else STATUS_DONE
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"job {job_id} not found")
            if row["status"] not in (STATUS_CLAIMED, STATUS_PENDING):
                raise ValueError(f"job {job_id} status is {row['status']}, cannot ack")
            conn.execute(
                """
                UPDATE queue_jobs
                SET status = ?, finished_at = ?, error = ?
                WHERE id = ?
                """,
                (status, finished, error, job_id),
            )
            conn.commit()
            cur = conn.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,))
            return _row_to_job(cur.fetchone())
        finally:
            conn.close()

    def _nack_sync(
        self,
        job_id: int,
        *,
        requeue: bool = True,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"job {job_id} not found")
            if row["status"] != STATUS_CLAIMED:
                raise ValueError(f"job {job_id} status is {row['status']}, cannot nack")
            if requeue:
                conn.execute(
                    """
                    UPDATE queue_jobs
                    SET status = ?, claimed_at = NULL, claimed_by = NULL, error = ?
                    WHERE id = ?
                    """,
                    (STATUS_PENDING, error, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE queue_jobs
                    SET status = ?, finished_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (STATUS_FAILED, _now_iso(), error or "nack", job_id),
                )
            conn.commit()
            cur = conn.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,))
            return _row_to_job(cur.fetchone())
        finally:
            conn.close()

    def _stats_sync(self, queue: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM queue_jobs
                WHERE queue = ?
                GROUP BY status
                """,
                (queue,),
            )
            counts = {
                STATUS_PENDING: 0,
                STATUS_CLAIMED: 0,
                STATUS_DONE: 0,
                STATUS_FAILED: 0,
            }
            for row in cur.fetchall():
                counts[str(row["status"])] = int(row["n"])
            return {"queue": queue, "counts": counts, "pending": counts[STATUS_PENDING]}
        finally:
            conn.close()

    async def enqueue(
        self,
        queue: str,
        payload: Any,
        *,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._enqueue_sync, queue, payload, idempotency_key=idempotency_key
        )

    async def claim(
        self,
        queue: str,
        *,
        limit: int = 1,
        claimed_by: str = "worker",
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._claim_sync, queue, limit=limit, claimed_by=claimed_by
        )

    async def ack(self, job_id: int, *, error: Optional[str] = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._ack_sync, job_id, error=error)

    async def nack(
        self,
        job_id: int,
        *,
        requeue: bool = True,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._nack_sync, job_id, requeue=requeue, error=error
        )

    async def stats(self, queue: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync, queue)
