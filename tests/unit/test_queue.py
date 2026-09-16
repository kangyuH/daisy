import pytest

from app.services.queue.service import ItemQueue


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

    done = await q.ack(items[0]["id"])
    assert done["status"] == "done"

    stats = await q.stats("inbound")
    assert stats["counts"]["done"] == 1


@pytest.mark.asyncio
async def test_queue_nack_requeue(db_file):
    q = ItemQueue(str(db_file))
    await q.enqueue("inbound", {"x": 1})
    items = await q.claim("inbound", limit=1)
    n = await q.nack(items[0]["id"], requeue=True)
    assert n["status"] == "pending"
