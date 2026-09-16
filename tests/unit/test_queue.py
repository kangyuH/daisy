import pytest

from app.services.queue.service import JobQueue


@pytest.mark.asyncio
async def test_queue_enqueue_claim_ack(db_file):
    q = JobQueue(str(db_file))
    j1 = await q.enqueue("inbound", {"a": 1}, idempotency_key="k1")
    assert j1["status"] == "pending"
    assert j1["deduped"] is False

    j2 = await q.enqueue("inbound", {"a": 2}, idempotency_key="k1")
    assert j2["deduped"] is True
    assert j2["id"] == j1["id"]

    jobs = await q.claim("inbound", limit=1, claimed_by="t")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "claimed"

    done = await q.ack(jobs[0]["id"])
    assert done["status"] == "done"

    stats = await q.stats("inbound")
    assert stats["counts"]["done"] == 1


@pytest.mark.asyncio
async def test_queue_nack_requeue(db_file):
    q = JobQueue(str(db_file))
    j = await q.enqueue("inbound", {"x": 1})
    jobs = await q.claim("inbound", limit=1)
    n = await q.nack(jobs[0]["id"], requeue=True)
    assert n["status"] == "pending"
