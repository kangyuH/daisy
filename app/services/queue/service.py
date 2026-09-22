from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.settings import (
    queue_lease_seconds,
    queue_max_attempts,
)
from app.infra.db import connect_sync


TZ_CN = timezone(timedelta(hours=8))

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Message queue name (distinct from future task list).
QUEUE_INBOUND = "inbound"
# Auto-research only (ledger commands spawn in-process, not via this queue).
QUEUE_AGENT = "agent"


class ClaimOwnershipError(ValueError):
    """ack/nack/heartbeat rejected: claim_token mismatch or not claimed."""


def _now() -> datetime:
    return datetime.now(TZ_CN)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _lease_until_iso(now: Optional[datetime] = None) -> str:
    base = now or _now()
    return (base + timedelta(seconds=queue_lease_seconds())).isoformat(
        timespec="seconds"
    )


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff capped at 300s: 5 * 2^(attempts-1)."""
    n = max(1, int(attempts))
    return min(300, 5 * (2 ** (n - 1)))


def _row_to_item(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("payload")
    if isinstance(raw, str):
        try:
            d["payload"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return d


def _claimable_sql() -> str:
    """pending ready, or claimed with expired/missing lease_until."""
    return """
        (
          (status = ? AND (not_before IS NULL OR not_before = '' OR not_before <= ?))
          OR
          (status = ? AND (lease_until IS NULL OR lease_until = '' OR lease_until <= ?))
        )
    """


class ItemQueue:
    """Generic named queues (inbound messages now; task list later)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path

    def _connect(self):
        return connect_sync(self._db_path)

    def _get_sync(self, item_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"queue item {item_id} not found")
            return _row_to_item(row)
        finally:
            conn.close()

    def _reset_for_force(self, conn: Any, item_id: int, payload_s: str) -> dict[str, Any]:
        created = _now_iso()
        conn.execute(
            """
            UPDATE queue_items
            SET status = ?,
                payload = ?,
                claimed_at = NULL,
                claimed_by = NULL,
                claim_token = NULL,
                lease_until = NULL,
                not_before = NULL,
                attempts = 0,
                finished_at = NULL,
                error = NULL,
                created_at = ?
            WHERE id = ?
            """,
            (STATUS_PENDING, payload_s, created, item_id),
        )
        conn.commit()
        cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
        item = _row_to_item(cur.fetchone())
        item["deduped"] = False
        item["forced"] = True
        return item

    def _enqueue_sync(
        self,
        queue: str,
        payload: Any,
        *,
        idempotency_key: Optional[str] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        payload_s = json.dumps(payload, ensure_ascii=False)
        created = _now_iso()
        conn = self._connect()
        try:
            if idempotency_key:
                cur = conn.execute(
                    "SELECT * FROM queue_items WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing:
                    item = _row_to_item(existing)
                    status = str(item.get("status") or "")
                    if force and status in (STATUS_FAILED, STATUS_DONE):
                        return self._reset_for_force(conn, int(item["id"]), payload_s)
                    item["deduped"] = True
                    return item

            try:
                cur = conn.execute(
                    """
                    INSERT INTO queue_items
                        (queue, status, payload, idempotency_key, created_at, attempts)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (queue, STATUS_PENDING, payload_s, idempotency_key, created),
                )
                conn.commit()
                item_id = cur.lastrowid
            except Exception as exc:
                if idempotency_key and "UNIQUE" in str(exc).upper():
                    cur = conn.execute(
                        "SELECT * FROM queue_items WHERE idempotency_key = ?",
                        (idempotency_key,),
                    )
                    existing = cur.fetchone()
                    if existing:
                        item = _row_to_item(existing)
                        status = str(item.get("status") or "")
                        if force and status in (STATUS_FAILED, STATUS_DONE):
                            return self._reset_for_force(
                                conn, int(item["id"]), payload_s
                            )
                        item["deduped"] = True
                        return item
                raise

            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            assert row is not None
            item = _row_to_item(row)
            item["deduped"] = False
            return item
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
        now = _now()
        now_s = now.isoformat(timespec="seconds")
        claimed_at = now_s
        lease_until = _lease_until_iso(now)
        claimable = _claimable_sql()
        conn = self._connect()
        try:
            # Cheap read first: empty queues must not take a write lock every poll.
            cur = conn.execute(
                f"""
                SELECT id FROM queue_items
                WHERE queue = ? AND {claimable}
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (
                    queue,
                    STATUS_PENDING,
                    now_s,
                    STATUS_CLAIMED,
                    now_s,
                    limit,
                ),
            )
            peek_ids = [int(r["id"]) for r in cur.fetchall()]
            if not peek_ids:
                return []

            # End deferred read txn before taking the write lock.
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"""
                SELECT id FROM queue_items
                WHERE queue = ? AND {claimable}
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (
                    queue,
                    STATUS_PENDING,
                    now_s,
                    STATUS_CLAIMED,
                    now_s,
                    limit,
                ),
            )
            ids = [int(r["id"]) for r in cur.fetchall()]
            if not ids:
                conn.commit()
                return []

            claimed: list[dict[str, Any]] = []
            for item_id in ids:
                token = secrets.token_hex(16)
                cur = conn.execute(
                    f"""
                    UPDATE queue_items
                    SET status = ?,
                        claimed_at = ?,
                        claimed_by = ?,
                        claim_token = ?,
                        lease_until = ?,
                        not_before = NULL,
                        attempts = COALESCE(attempts, 0) + 1
                    WHERE id = ? AND queue = ? AND {claimable}
                    """,
                    (
                        STATUS_CLAIMED,
                        claimed_at,
                        claimed_by,
                        token,
                        lease_until,
                        item_id,
                        queue,
                        STATUS_PENDING,
                        now_s,
                        STATUS_CLAIMED,
                        now_s,
                    ),
                )
                if cur.rowcount != 1:
                    continue
                row = conn.execute(
                    "SELECT * FROM queue_items WHERE id = ?", (item_id,)
                ).fetchone()
                if row:
                    claimed.append(_row_to_item(row))
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _raise_ownership_error(
        self, conn: Any, item_id: int, claim_token: str
    ) -> None:
        token = (claim_token or "").strip()
        if not token:
            raise ClaimOwnershipError(
                f"queue item {item_id} claim_token is required"
            )
        cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"queue item {item_id} not found")
        if row["status"] != STATUS_CLAIMED:
            raise ClaimOwnershipError(
                f"queue item {item_id} status is {row['status']}, cannot operate"
            )
        current = str(row["claim_token"] or "").strip()
        if current != token:
            raise ClaimOwnershipError(
                f"queue item {item_id} claim_token mismatch"
            )
        raise ClaimOwnershipError(
            f"queue item {item_id} ownership update failed"
        )

    def _ack_sync(
        self,
        item_id: int,
        *,
        claim_token: str,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        finished = _now_iso()
        status = STATUS_FAILED if error else STATUS_DONE
        conn = self._connect()
        try:
            token = (claim_token or "").strip()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE queue_items
                SET status = ?,
                    finished_at = ?,
                    error = ?,
                    claim_token = NULL,
                    lease_until = NULL,
                    claimed_by = NULL
                WHERE id = ? AND status = ? AND claim_token = ?
                """,
                (
                    status,
                    finished,
                    error,
                    item_id,
                    STATUS_CLAIMED,
                    token,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                self._raise_ownership_error(conn, item_id, token)
            conn.commit()
            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            return _row_to_item(cur.fetchone())
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _nack_sync(
        self,
        item_id: int,
        *,
        claim_token: str,
        requeue: bool = True,
        error: Optional[str] = None,
        busy: bool = False,
    ) -> dict[str, Any]:
        """
        Release a claim.

        busy=True: agent already running — rewind claim attempt, light delay,
        track busy_attempts in payload (cap 20). Does not burn QUEUE_MAX_ATTEMPTS.
        """
        conn = self._connect()
        try:
            token = (claim_token or "").strip()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM queue_items
                WHERE id = ? AND status = ? AND claim_token = ?
                """,
                (item_id, STATUS_CLAIMED, token),
            ).fetchone()
            if not row:
                conn.rollback()
                self._raise_ownership_error(conn, item_id, token)
            item = _row_to_item(row)
            attempts = int(row["attempts"] or 0)
            max_attempts = queue_max_attempts()
            payload = item.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            if busy and requeue:
                busy_n = int(payload.get("busy_attempts") or 0) + 1
                payload["busy_attempts"] = busy_n
                if busy_n >= 20:
                    conn.execute(
                        """
                        UPDATE queue_items
                        SET status = ?,
                            finished_at = ?,
                            error = ?,
                            claim_token = NULL,
                            lease_until = NULL,
                            claimed_by = NULL,
                            not_before = NULL,
                            attempts = MAX(0, COALESCE(attempts, 0) - 1),
                            payload = ?
                        WHERE id = ? AND status = ? AND claim_token = ?
                        """,
                        (
                            STATUS_FAILED,
                            _now_iso(),
                            error or f"busy retry exhausted ({busy_n})",
                            json.dumps(payload, ensure_ascii=False),
                            item_id,
                            STATUS_CLAIMED,
                            token,
                        ),
                    )
                else:
                    delay = min(60, 5 * busy_n)
                    not_before = (_now() + timedelta(seconds=delay)).isoformat(
                        timespec="seconds"
                    )
                    conn.execute(
                        """
                        UPDATE queue_items
                        SET status = ?,
                            claimed_at = NULL,
                            claimed_by = NULL,
                            claim_token = NULL,
                            lease_until = NULL,
                            not_before = ?,
                            error = ?,
                            attempts = MAX(0, COALESCE(attempts, 0) - 1),
                            payload = ?
                        WHERE id = ? AND status = ? AND claim_token = ?
                        """,
                        (
                            STATUS_PENDING,
                            not_before,
                            error,
                            json.dumps(payload, ensure_ascii=False),
                            item_id,
                            STATUS_CLAIMED,
                            token,
                        ),
                    )
            elif requeue and attempts < max_attempts:
                delay = backoff_seconds(attempts)
                not_before = (_now() + timedelta(seconds=delay)).isoformat(
                    timespec="seconds"
                )
                conn.execute(
                    """
                    UPDATE queue_items
                    SET status = ?,
                        claimed_at = NULL,
                        claimed_by = NULL,
                        claim_token = NULL,
                        lease_until = NULL,
                        not_before = ?,
                        error = ?
                    WHERE id = ? AND status = ? AND claim_token = ?
                    """,
                    (
                        STATUS_PENDING,
                        not_before,
                        error,
                        item_id,
                        STATUS_CLAIMED,
                        token,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE queue_items
                    SET status = ?,
                        finished_at = ?,
                        error = ?,
                        claim_token = NULL,
                        lease_until = NULL,
                        claimed_by = NULL,
                        not_before = NULL
                    WHERE id = ? AND status = ? AND claim_token = ?
                    """,
                    (
                        STATUS_FAILED,
                        _now_iso(),
                        error or "nack",
                        item_id,
                        STATUS_CLAIMED,
                        token,
                    ),
                )
            conn.commit()
            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            return _row_to_item(cur.fetchone())
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _heartbeat_sync(
        self, item_id: int, *, claim_token: str
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            token = (claim_token or "").strip()
            lease_until = _lease_until_iso()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE queue_items
                SET lease_until = ?
                WHERE id = ? AND status = ? AND claim_token = ?
                """,
                (lease_until, item_id, STATUS_CLAIMED, token),
            )
            if cur.rowcount != 1:
                conn.rollback()
                self._raise_ownership_error(conn, item_id, token)
            conn.commit()
            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            return _row_to_item(cur.fetchone())
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _stats_sync(self, queue: str) -> dict[str, Any]:
        now_s = _now_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM queue_items
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

            stale = conn.execute(
                """
                SELECT COUNT(*) AS n FROM queue_items
                WHERE queue = ? AND status = ?
                  AND (lease_until IS NULL OR lease_until = '' OR lease_until <= ?)
                """,
                (queue, STATUS_CLAIMED, now_s),
            ).fetchone()
            delayed = conn.execute(
                """
                SELECT COUNT(*) AS n FROM queue_items
                WHERE queue = ? AND status = ?
                  AND not_before IS NOT NULL AND not_before != '' AND not_before > ?
                """,
                (queue, STATUS_PENDING, now_s),
            ).fetchone()
            return {
                "queue": queue,
                "counts": counts,
                "pending": counts[STATUS_PENDING],
                "claimed": counts[STATUS_CLAIMED],
                "stale_claimed": int(stale["n"] if stale else 0),
                "delayed": int(delayed["n"] if delayed else 0),
            }
        finally:
            conn.close()

    def _merge_payload_sync(self, item_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"queue item {item_id} not found")
            item = _row_to_item(row)
            payload = item.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            merged = {**payload, **patch}
            conn.execute(
                "UPDATE queue_items SET payload = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), item_id),
            )
            conn.commit()
            cur = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            return _row_to_item(cur.fetchone())
        finally:
            conn.close()

    async def get(self, item_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_sync, item_id)

    async def merge_payload(self, item_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._merge_payload_sync, item_id, patch)

    async def enqueue(
        self,
        queue: str,
        payload: Any,
        *,
        idempotency_key: Optional[str] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._enqueue_sync,
            queue,
            payload,
            idempotency_key=idempotency_key,
            force=force,
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

    async def ack(
        self,
        item_id: int,
        *,
        claim_token: str,
        error: Optional[str] = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._ack_sync, item_id, claim_token=claim_token, error=error
        )

    async def nack(
        self,
        item_id: int,
        *,
        claim_token: str,
        requeue: bool = True,
        error: Optional[str] = None,
        busy: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._nack_sync,
            item_id,
            claim_token=claim_token,
            requeue=requeue,
            error=error,
            busy=busy,
        )

    async def heartbeat(
        self, item_id: int, *, claim_token: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._heartbeat_sync, item_id, claim_token=claim_token
        )

    async def stats(self, queue: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync, queue)


# Back-compat alias
JobQueue = ItemQueue
