from __future__ import annotations

from typing import Any, Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.bots.models import BotConfig


def _as_open_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        return _as_open_id(value.get("open_id") or value.get("user_id") or value.get("id"))
    s = str(value).strip()
    if not s or s.startswith("{") or s.startswith("["):
        return None
    return s


def extract_mention_open_ids(event: dict[str, Any]) -> list[str]:
    """Collect open_ids from Feishu message-receive event mentions."""
    ids: list[str] = []
    seen: set[str] = set()

    def _add(oid: Any) -> None:
        s = _as_open_id(oid)
        if s and s not in seen:
            seen.add(s)
            ids.append(s)

    candidates: list[Any] = [event.get("mentions")]

    msg = event.get("message")
    if isinstance(msg, dict):
        candidates.append(msg.get("mentions"))

    event_obj = event.get("event")
    if isinstance(event_obj, dict):
        candidates.append(event_obj.get("mentions"))
        msg = event_obj.get("message")
        if isinstance(msg, dict):
            candidates.append(msg.get("mentions"))

    data = event.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("mentions"))
        msg = data.get("message")
        if isinstance(msg, dict):
            candidates.append(msg.get("mentions"))
        ev = data.get("event")
        if isinstance(ev, dict):
            candidates.append(ev.get("mentions"))
            msg = ev.get("message")
            if isinstance(msg, dict):
                candidates.append(msg.get("mentions"))

    for block in candidates:
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            _add(item.get("open_id"))
            _add(item.get("id"))

    return ids


def should_enqueue(
    mention_ids: Iterable[str],
    *,
    self_open_id: Optional[str],
    bot_open_id: Optional[str],
) -> bool:
    targets = {x for x in (self_open_id, bot_open_id) if x}
    if not targets:
        return False
    return any(m in targets for m in mention_ids)


def normalize_inbound_event(bot: "BotConfig", raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten common event shapes into a stable inbound payload."""
    event = raw.get("event") if isinstance(raw.get("event"), dict) else None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else None
    message = None
    for candidate in (
        raw.get("message") if isinstance(raw.get("message"), dict) else None,
        event.get("message") if isinstance(event, dict) else None,
        data.get("message") if isinstance(data, dict) else None,
    ):
        if isinstance(candidate, dict):
            message = candidate
            break
    if message is None:
        message = {}

    message_id = (
        raw.get("message_id")
        or message.get("message_id")
        or message.get("msg_id")
        or raw.get("id")
        or ""
    )
    chat_id = (
        str(raw.get("chat_id") or "")
        or str(message.get("chat_id") or "")
        or (str(event.get("chat_id") or "") if isinstance(event, dict) else "")
    )

    sender = raw.get("sender") or message.get("sender")
    if sender is None and raw.get("sender_id"):
        sender = {"id": raw.get("sender_id"), "sender_type": raw.get("sender_type")}
    if sender is None and isinstance(event, dict):
        sender = event.get("sender")

    event_id = raw.get("event_id")
    header = raw.get("header")
    if not event_id and isinstance(header, dict):
        event_id = header.get("event_id")

    mentions = (
        raw.get("mentions")
        or message.get("mentions")
        or (event.get("mentions") if isinstance(event, dict) else None)
    )

    sender_open_id = _as_open_id(sender)
    if sender_open_id is None and isinstance(sender, dict):
        sender_open_id = _as_open_id(
            sender.get("sender_id") or (sender.get("id") if isinstance(sender.get("id"), dict) else None)
        )

    return {
        "bot_id": bot.id,
        "bot_name": bot.name,
        "app_id": bot.app_id,
        "bot_open_id": bot.open_id,
        "self_open_id": bot.self_open_id,
        "message_id": message_id,
        "chat_id": chat_id,
        "thread_id": raw.get("thread_id") or message.get("thread_id"),
        "content": raw.get("content") or message.get("content"),
        "message_type": raw.get("message_type") or message.get("message_type"),
        "sender": sender,
        "sender_open_id": sender_open_id,
        "mentions": mentions,
        "event_id": event_id,
        "raw": raw,
    }
