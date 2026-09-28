from __future__ import annotations

from typing import Any, Optional

from app.infra.lark.cli import LarkCliError
from app.services.im.outbound import respond_to_message
from app.services.queue.service import QUEUE_INBOUND, ItemQueue
from app.services.tasks.store import TaskStore

ACK_DECISIONS = frozenset({"create", "followup"})
TITLE_MAX_LEN = 40
# Business-chat text ack is offline. Typing emoji on the inbound message
# is added elsewhere and is not affected by this flag.
TEXT_ACK_ENABLED = False


def _payload_str(payload: dict[str, Any], key: str) -> Optional[str]:
    val = payload.get(key)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _trim_title(title: str) -> str:
    t = (title or "").strip()
    if len(t) <= TITLE_MAX_LEN:
        return t
    return t[: TITLE_MAX_LEN - 1] + "…"


def build_ack_text(decision: str, title: Optional[str] = None) -> str:
    """Short business-chat ack; never includes task id / number."""
    t = _trim_title(title or "")
    if decision == "create":
        if t:
            return f"收到，已狸解您的需求并创建任务「{t}」。我们将安排专人为您处狸，请您耐心等待。"
        return "收到，已创建任务。"
    if decision == "followup":
        if t:
            return f"收到，已狸解您的需求并跟进任务「{t}」。我们将安排专人为您处狸，请您耐心等待。"
        return "收到，已跟进该任务。"
    raise ValueError(f"unsupported ack decision: {decision!r}")


async def maybe_ack_dispatch_run(
    *,
    request: Any,
    queue: ItemQueue,
    task_store: TaskStore,
    inbound_id: int,
    decision: str,
    task_id: Optional[int],
    enabled: bool = True,
) -> dict[str, Any]:
    """Send create/followup short reply. Never raises; returns reply_sync info."""
    # Lazy import: app.core.deps imports DispatchRunStore via this package.
    from app.core.deps import bot_profile

    if not TEXT_ACK_ENABLED or not enabled:
        return {"ok": True, "skipped": True, "reason": "disabled"}

    decision_s = (decision or "").strip()
    if decision_s not in ACK_DECISIONS:
        return {"ok": True, "skipped": True, "reason": "decision_not_ackable"}

    try:
        item = await queue.get(int(inbound_id))
    except KeyError:
        print(
            f"[dispatcher-ack] inbound {inbound_id} not found; skip reply",
            flush=True,
        )
        return {"ok": True, "skipped": True, "reason": "inbound_not_found"}

    if str(item.get("queue") or "") != QUEUE_INBOUND:
        return {"ok": True, "skipped": True, "reason": "not_inbound_queue"}

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    message_id = _payload_str(payload, "message_id")
    if not message_id:
        print(
            f"[dispatcher-ack] inbound {inbound_id} missing message_id; skip reply",
            flush=True,
        )
        return {"ok": True, "skipped": True, "reason": "missing_message_id"}

    title: Optional[str] = None
    if task_id is not None:
        task = task_store.get_task(int(task_id), events_limit=1)
        if task:
            title = str(task.get("title") or "").strip() or None

    text = build_ack_text(decision_s, title)
    bot_id = _payload_str(payload, "bot_id")
    profile = bot_profile(request, bot_id)
    thread_id = _payload_str(payload, "thread_id")
    sender_open_id = _payload_str(payload, "sender_open_id")
    idem = f"ack:{inbound_id}"

    try:
        result = await respond_to_message(
            message_id=message_id,
            text=text,
            profile=profile,
            thread_id=thread_id,
            sender_open_id=sender_open_id,
            mention_open_ids=[],
            idempotency_key=idem,
        )
        return {
            "ok": True,
            "text": text,
            "message_id": message_id,
            "profile": profile,
            "idempotency_key": idem,
            "result": result,
        }
    except (LarkCliError, Exception) as exc:
        err = str(exc)[:2000]
        print(
            f"[dispatcher-ack] reply failed inbound={inbound_id}: {err}",
            flush=True,
        )
        return {"ok": False, "error": err, "text": text}


__all__ = [
    "ACK_DECISIONS",
    "TEXT_ACK_ENABLED",
    "TITLE_MAX_LEN",
    "build_ack_text",
    "maybe_ack_dispatch_run",
]
