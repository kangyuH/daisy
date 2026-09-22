import pytest

from app.services.queue.service import ItemQueue, backoff_seconds


@pytest.mark.asyncio
async def test_queue_enqueue_claim_ack(db_file):
    q = ItemQueue(str(db_file))
    i1 = await q.enqueue("inbound", {"a": 1}, idempotency_key="k1")
    assert i1["status"] == "pending"
    assert i1["deduped"] is False

    i2 = await q.enqueue("inbound", {"a": 2}, idempotency_key="k1")
    assert i2["deduped"] is True
    assert i2["id"] == i1["id"]

    items = await q.claim("inbound", limit=1, claimed_by="t")
    assert len(items) == 1
    assert items[0]["status"] == "claimed"
    token = items[0]["claim_token"]
    assert token

    done = await q.ack(items[0]["id"], claim_token=token)
    assert done["status"] == "done"

    stats = await q.stats("inbound")
    assert stats["counts"]["done"] == 1


@pytest.mark.asyncio
async def test_queue_nack_requeue(db_file):
    q = ItemQueue(str(db_file))
    await q.enqueue("inbound", {"x": 1})
    items = await q.claim("inbound", limit=1)
    n = await q.nack(
        items[0]["id"], claim_token=items[0]["claim_token"], requeue=True
    )
    assert n["status"] == "pending"
    assert n.get("not_before")


@pytest.mark.asyncio
async def test_queue_claim_empty_then_pending(db_file):
    q = ItemQueue(str(db_file))
    assert await q.claim("inbound", limit=1, claimed_by="t") == []
    await q.enqueue("inbound", {"x": 1})
    items = await q.claim("inbound", limit=1, claimed_by="t")
    assert len(items) == 1
    assert items[0]["status"] == "claimed"


@pytest.mark.asyncio
async def test_queue_stale_claim_reclaimed_old_token_409(db_file, monkeypatch):
    from app.services.queue import service as qs

    monkeypatch.setattr(qs, "queue_lease_seconds", lambda: 30)
    q = ItemQueue(str(db_file))
    await q.enqueue("inbound", {"x": 1})
    first = (await q.claim("inbound", limit=1, claimed_by="a"))[0]
    old_token = first["claim_token"]

    # Expire lease
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=5)).isoformat(
        timespec="seconds"
    )
    conn = q._connect()
    try:
        conn.execute(
            "UPDATE queue_items SET lease_until = ? WHERE id = ?",
            (past, first["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    second = (await q.claim("inbound", limit=1, claimed_by="b"))[0]
    assert second["id"] == first["id"]
    assert second["claim_token"] != old_token
    assert second["attempts"] == 2

    owned_fields = {
        key: second.get(key)
        for key in (
            "status",
            "claim_token",
            "lease_until",
            "claimed_by",
            "attempts",
            "not_before",
            "error",
        )
    }
    stale_calls = (
        lambda: q.ack(first["id"], claim_token=old_token),
        lambda: q.nack(
            first["id"], claim_token=old_token, requeue=True, error="stale"
        ),
        lambda: q.heartbeat(first["id"], claim_token=old_token),
    )
    for call in stale_calls:
        with pytest.raises(qs.ClaimOwnershipError):
            await call()
        current = await q.get(first["id"])
        assert {key: current.get(key) for key in owned_fields} == owned_fields

    done = await q.ack(second["id"], claim_token=second["claim_token"])
    assert done["status"] == "done"


@pytest.mark.asyncio
async def test_queue_not_before_blocks_claim(db_file):
    from datetime import datetime, timedelta, timezone

    q = ItemQueue(str(db_file))
    await q.enqueue("inbound", {"x": 1})
    item = (await q.claim("inbound", limit=1))[0]
    future = (
        datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=1)
    ).isoformat(timespec="seconds")
    await q.nack(
        item["id"], claim_token=item["claim_token"], requeue=True, error="tmp"
    )
    conn = q._connect()
    try:
        conn.execute(
            "UPDATE queue_items SET not_before = ? WHERE id = ?",
            (future, item["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    assert await q.claim("inbound", limit=1) == []


@pytest.mark.asyncio
async def test_queue_attempts_exhausted_failed(db_file, monkeypatch):
    from app.services.queue import service as qs

    monkeypatch.setattr(qs, "queue_max_attempts", lambda: 2)
    q = ItemQueue(str(db_file))
    await q.enqueue("inbound", {"x": 1})
    out = None
    for _ in range(2):
        item = (await q.claim("inbound", limit=1))[0]
        out = await q.nack(
            item["id"],
            claim_token=item["claim_token"],
            requeue=True,
            error="fail",
        )
        if out["status"] == "pending":
            # clear backoff so next claim can proceed immediately
            conn = q._connect()
            try:
                conn.execute(
                    "UPDATE queue_items SET not_before = NULL WHERE id = ?",
                    (item["id"],),
                )
                conn.commit()
            finally:
                conn.close()
    assert out is not None
    assert out["status"] == "failed"


@pytest.mark.asyncio
async def test_queue_force_reopens_failed(db_file):
    q = ItemQueue(str(db_file))
    a = await q.enqueue("agent", {"task_id": 1}, idempotency_key="agent:research:1")
    item = (await q.claim("agent", limit=1))[0]
    await q.ack(item["id"], claim_token=item["claim_token"], error="boom")
    failed = await q.get(a["id"])
    assert failed["status"] == "failed"

    again = await q.enqueue(
        "agent",
        {"task_id": 1},
        idempotency_key="agent:research:1",
        force=False,
    )
    assert again["deduped"] is True
    assert again["status"] == "failed"

    forced = await q.enqueue(
        "agent",
        {"task_id": 1, "source": "manual"},
        idempotency_key="agent:research:1",
        force=True,
    )
    assert forced.get("forced") is True
    assert forced["status"] == "pending"
    assert forced["attempts"] == 0


def test_backoff_seconds():
    assert backoff_seconds(1) == 5
    assert backoff_seconds(2) == 10
    assert backoff_seconds(10) == 300
