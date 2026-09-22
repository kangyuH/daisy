from __future__ import annotations

from typing import Any, Optional

from app.services.agent.constants import MODE_AUTO_RESEARCH
from app.services.queue.service import QUEUE_AGENT, ItemQueue


def research_idempotency_key(task_id: int) -> str:
    return f"agent:research:{int(task_id)}"


async def enqueue_auto_research(
    queue: ItemQueue,
    *,
    task_id: int,
    source: str = "create",
    trigger: Optional[str] = None,
    inbound_id: Optional[int] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Enqueue one auto-research job per task (idempotent)."""
    tid = int(task_id)
    src = (source or trigger or "create").strip() or "create"
    payload: dict[str, Any] = {
        "task_id": tid,
        "mode": MODE_AUTO_RESEARCH,
        "phase": "research",  # CLI phase alias for older consumers
        "source": src,
        "trigger": src,  # audit alias
    }
    if inbound_id is not None:
        payload["inbound_id"] = int(inbound_id)
    item = await queue.enqueue(
        QUEUE_AGENT,
        payload,
        idempotency_key=research_idempotency_key(tid),
        force=bool(force),
    )
    return item
