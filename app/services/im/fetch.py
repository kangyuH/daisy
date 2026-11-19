from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.infra.lark.cli import LarkCliError, run_cli_async
from app.services.im.task_links import MessageTaskStore

TZ_CN = timezone(timedelta(hours=8))


def user_profile() -> Optional[str]:
    p = os.environ.get("LARK_USER_PROFILE", "").strip()
    return p or None


def _profile_args() -> list[str]:
    p = user_profile()
    return ["--profile", p] if p else []


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Feishu often uses ms or s epoch
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=TZ_CN)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_ts(int(s))
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CN)
        return dt.astimezone(TZ_CN)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ_CN)
            return dt
        except ValueError:
            continue
    return None


def _msg_id(msg: dict[str, Any]) -> Optional[str]:
    return msg.get("message_id") or msg.get("msg_id") or msg.get("id")


def _msg_time(msg: dict[str, Any]) -> Optional[datetime]:
    for key in ("create_time", "created_at", "create_time_ms", "timestamp"):
        dt = _parse_ts(msg.get(key))
        if dt:
            return dt
    return None


def _thread_position(msg: dict[str, Any]) -> Optional[int]:
    raw = msg.get("thread_message_position")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _chronological(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return messages oldest-first.

    `create_time` only has minute precision, so same-minute messages tie. Thread
    replies carry a monotonic `thread_message_position`, which orders them
    exactly; use it only when every message has one, since a mixed list (thread
    replies plus root messages) would otherwise be split into two groups.
    """
    if messages and all(_thread_position(message) is not None for message in messages):
        return sorted(messages, key=lambda m: (_thread_position(m), _msg_id(m) or ""))
    # Fall back to time alone; the sort is stable, so messages sharing a
    # timestamp keep the order the API returned them in.
    return sorted(
        messages,
        key=lambda m: _msg_time(m) or datetime.min.replace(tzinfo=TZ_CN),
    )


def _extract_messages(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data")
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        for key in ("messages", "items", "list"):
            block = data.get(key)
            if isinstance(block, list):
                return [m for m in block if isinstance(m, dict)]
        # mget may return map or messages under data
        if "message_id" in data:
            return [data]
    # top-level
    for key in ("messages", "items"):
        block = result.get(key)
        if isinstance(block, list):
            return [m for m in block if isinstance(m, dict)]
    return []


async def mget_messages(message_ids: list[str]) -> list[dict[str, Any]]:
    if not message_ids:
        return []
    args = [
        "im",
        "+messages-mget",
        "--as",
        "user",
        "--message-ids",
        ",".join(message_ids),
        "--no-reactions",
        *_profile_args(),
    ]
    result = await run_cli_async(args)
    return _extract_messages(result)


async def resolve_user_sender_open_id(message_id: str) -> Optional[str]:
    """
    Resolve the real user open_id for a message via user-identity mget.

    Bot event-consume streams often rewrite sender_id to the consuming bot's
    self_open_id; do not trust that field for ledger auth.
    """
    mid = (message_id or "").strip()
    if not mid:
        return None
    try:
        msgs = await mget_messages([mid])
    except Exception:
        return None
    if not msgs:
        return None
    msg = msgs[0]
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    if str(sender.get("sender_type") or "").lower() not in ("", "user"):
        # still allow if id looks like ou_
        pass
    oid = str(sender.get("id") or sender.get("open_id") or "").strip()
    return oid or None


async def list_chat_messages(
    chat_id: str,
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    page_size: int = 50,
    order: str = "asc",
) -> list[dict[str, Any]]:
    args = [
        "im",
        "+chat-messages-list",
        "--as",
        "user",
        "--chat-id",
        chat_id,
        "--page-size",
        str(max(1, min(page_size, 50))),
        "--order",
        order,
        "--no-reactions",
        *_profile_args(),
    ]
    if start:
        args.extend(["--start", start.isoformat()])
    if end:
        args.extend(["--end", end.isoformat()])
    result = await run_cli_async(args)
    return _extract_messages(result)


THREAD_PAGE_SIZE = 50
# Feishu rejects page_size > 50 for threads-messages-list; the cap below bounds
# how far we walk back (pages * 50 messages) when the anchor is far behind.
THREAD_MAX_PAGES = 40


def _page_meta(result: dict[str, Any]) -> tuple[Optional[str], bool]:
    data = result.get("data")
    if not isinstance(data, dict):
        return None, False
    token = data.get("page_token") if data.get("page_token") is not None else data.get("pageToken")
    has_more = data.get("has_more") if data.get("has_more") is not None else data.get("hasMore")
    return (str(token) if token else None), bool(has_more)


async def list_thread_messages_page(
    thread: str,
    *,
    page_size: int = THREAD_PAGE_SIZE,
    order: str = "asc",
    page_token: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[str], bool]:
    """One page of thread messages: (messages, next_page_token, has_more)."""
    args = [
        "im",
        "+threads-messages-list",
        "--as",
        "user",
        "--thread",
        thread,
        "--page-size",
        str(max(1, min(page_size, THREAD_PAGE_SIZE))),
        "--order",
        order,
        "--no-reactions",
        *_profile_args(),
    ]
    if page_token:
        args.extend(["--page-token", page_token])
    result = await run_cli_async(args)
    token, has_more = _page_meta(result)
    return _extract_messages(result), token, has_more


async def list_thread_messages(
    thread: str,
    *,
    page_size: int = THREAD_PAGE_SIZE,
    order: str = "asc",
) -> list[dict[str, Any]]:
    messages, _, _ = await list_thread_messages_page(
        thread, page_size=page_size, order=order
    )
    return messages


def _slice_around(
    messages: list[dict[str, Any]],
    *,
    anchor_id: Optional[str],
    before: int,
    after: int,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    messages = _chronological(messages)
    if not anchor_id:
        # latest window
        return messages[-(before + after) :], None

    idx = None
    anchor = None
    for i, m in enumerate(messages):
        if _msg_id(m) == anchor_id:
            idx = i
            anchor = m
            break
    if idx is None:
        return messages, None

    start = max(0, idx - before)
    end = min(len(messages), idx + 1 + after)
    return messages[start:end], anchor


def _merge_messages(
    blocks: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Dedupe pages by message_id and return them in chronological order."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for block in blocks:
        for message in block:
            message_id = _msg_id(message)
            if message_id:
                if message_id in seen:
                    continue
                seen.add(message_id)
            merged.append(message)
    return _chronological(merged)


def _block_earliest(block: list[dict[str, Any]]) -> Optional[datetime]:
    times = [t for t in (_msg_time(message) for message in block) if t is not None]
    return min(times) if times else None


async def collect_thread_window(
    thread: str,
    *,
    message_id: Optional[str],
    anchor_time: Optional[datetime],
    before: int,
    after: int,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]], dict[str, Any]]:
    """Walk a thread from its newest message backwards until the anchor window fits.

    Threads grow without bound, so a single ascending first page silently returns
    week-old messages for long threads. We page downward from the newest message
    instead, stopping as soon as:
      * the anchor is present and has `before` older messages behind it (or the
        thread head is reached), or
      * the page already reaches back past the anchor timestamp without finding
        it (deleted / not visible), or
      * `THREAD_MAX_PAGES` is hit.
    """
    page_size = THREAD_PAGE_SIZE
    blocks: list[list[dict[str, Any]]] = []
    merged: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    pages = 0
    reached_head = False

    while pages < THREAD_MAX_PAGES:
        batch, page_token, has_more = await list_thread_messages_page(
            thread, page_size=page_size, order="desc", page_token=page_token
        )
        pages += 1
        if not batch:
            reached_head = not has_more
            break
        blocks.append(batch)
        merged = _merge_messages(blocks)

        # No anchor id: the newest page already is the window we want.
        if not message_id:
            break

        ids = [_msg_id(message) for message in merged]
        if message_id in ids:
            if ids.index(message_id) >= before or not has_more:
                reached_head = reached_head or not has_more
                break
            continue

        if not has_more:
            reached_head = True
            break
        earliest = _block_earliest(batch)
        if anchor_time is not None and earliest is not None and earliest < anchor_time:
            # Walked past the anchor's own timestamp without seeing it.
            break

    meta: dict[str, Any] = {
        "mode": "thread",
        "pages": pages,
        "reached_head": reached_head,
        "anchor_missing": False,
    }

    if not merged:
        meta["truncated"] = False
        return [], None, meta

    ids = [_msg_id(message) for message in merged]
    anchor: Optional[dict[str, Any]] = None

    if message_id and message_id in ids:
        idx = ids.index(message_id)
        anchor = merged[idx]
        window = merged[max(0, idx - before) : idx + 1 + after]
        # `truncated` means the requested history window is incomplete.
        meta["truncated"] = idx < before
        return window, anchor, meta

    if message_id:
        # Anchor never showed up: hand back the freshest slice instead of a
        # stale page, and flag it so callers can refuse to act on it.
        meta["anchor_missing"] = True
        meta["truncated"] = True
        limit = max(0, before) + max(0, after)
        return (merged[-limit:] if limit else []), None, meta

    limit = max(0, before) + max(0, after)
    meta["truncated"] = False
    return (merged[-limit:] if limit else []), None, meta


async def add_task_refs(
    context: dict[str, Any],
    store: MessageTaskStore,
    *,
    chat_id: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    task_chat_id: Optional[str] = None,
) -> dict[str, Any]:
    anchor = context["anchor"]
    messages = context["messages"]
    expected_chat = (anchor or {}).get("chat_id") or chat_id
    target = None
    if reply_to_message_id and expected_chat:
        target = next(
            (message for message in messages if _msg_id(message) == reply_to_message_id),
            None,
        )
        if target is None or not target.get("chat_id"):
            try:
                candidates = await mget_messages([reply_to_message_id])
            except LarkCliError:
                candidates = []
            target = next(
                (message for message in candidates if _msg_id(message) == reply_to_message_id),
                None,
            )
        if target and target.get("chat_id") != expected_chat:
            target = None

    message_ids = {
        message_id
        for message in [*messages, anchor, target]
        if message and (message_id := _msg_id(message))
    }
    refs = await asyncio.to_thread(store.find_refs, message_ids, task_chat_id=task_chat_id)

    def annotate(message: dict[str, Any]) -> dict[str, Any]:
        return {**message, "task_refs": refs.get(_msg_id(message), [])}

    return {
        **context,
        "anchor": annotate(anchor) if anchor else None,
        "messages": [annotate(message) for message in messages],
        "reply_target": annotate(target) if target else None,
    }


async def fetch_context(
    *,
    message_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    before: int = 10,
    after: int = 5,
    before_seconds: Optional[int] = None,
    after_seconds: Optional[int] = None,
) -> dict[str, Any]:
    before = max(0, int(before))
    after = max(0, int(after))

    anchor: Optional[dict[str, Any]] = None
    resolved_chat = chat_id
    resolved_thread = thread_id
    anchor_time: Optional[datetime] = None

    if message_id:
        try:
            found = await mget_messages([message_id])
        except LarkCliError:
            # Deleted / invisible message: caller still has chat/thread hints.
            found = []
        if found:
            anchor = found[0]
            resolved_chat = resolved_chat or anchor.get("chat_id")
            resolved_thread = resolved_thread or anchor.get("thread_id")
            anchor_time = _msg_time(anchor)

    meta: dict[str, Any] = {
        "mode": None,
        "truncated": False,
        "window_start": None,
        "window_end": None,
    }

    # Thread path
    if resolved_thread:
        window, found_anchor, thread_meta = await collect_thread_window(
            resolved_thread,
            message_id=message_id,
            anchor_time=anchor_time,
            before=before,
            after=after,
        )
        if found_anchor:
            anchor = found_anchor
        meta.update(thread_meta)
        return {"anchor": anchor, "messages": window, "meta": meta}

    if not resolved_chat:
        raise ValueError("chat_id or message_id (resolvable to chat) is required")

    # Time window around anchor, expand until enough count or max
    if before_seconds is None and after_seconds is None and anchor_time:
        # start with 5 minutes each side, expand
        before_seconds = 300
        after_seconds = 300
    elif before_seconds is None and after_seconds is None:
        before_seconds = 3600
        after_seconds = 60

    before_seconds = max(1, int(before_seconds or 300))
    after_seconds = max(1, int(after_seconds or 60))

    center = anchor_time or datetime.now(TZ_CN)
    max_before = 24 * 3600
    max_after = 24 * 3600
    cur_before = before_seconds
    cur_after = after_seconds
    collected: list[dict[str, Any]] = []

    for _ in range(6):
        start = center - timedelta(seconds=cur_before)
        end = center + timedelta(seconds=cur_after)
        meta["window_start"] = start.isoformat()
        meta["window_end"] = end.isoformat()
        collected = await list_chat_messages(
            resolved_chat, start=start, end=end, page_size=50, order="asc"
        )
        sliced, found_anchor = _slice_around(
            collected, anchor_id=message_id, before=before, after=after
        )
        if found_anchor:
            anchor = found_anchor

        enough_before = True
        enough_after = True
        if message_id and found_anchor:
            # check counts relative to anchor
            ids = [_msg_id(m) for m in sliced]
            try:
                ai = ids.index(message_id)
            except ValueError:
                ai = -1
            if ai >= 0:
                enough_before = ai >= before or cur_before >= max_before
                enough_after = (len(sliced) - ai - 1) >= after or cur_after >= max_after
            else:
                enough_before = cur_before >= max_before
                enough_after = cur_after >= max_after
        else:
            enough_before = len(sliced) >= before or cur_before >= max_before
            enough_after = True

        if enough_before and enough_after:
            meta["mode"] = "chat_time_window"
            meta["truncated"] = len(collected) >= 50
            return {"anchor": anchor, "messages": sliced, "meta": meta}

        if not enough_before:
            cur_before = min(max_before, cur_before * 2)
        if not enough_after:
            cur_after = min(max_after, cur_after * 2)

    sliced, found_anchor = _slice_around(
        collected, anchor_id=message_id, before=before, after=after
    )
    if found_anchor:
        anchor = found_anchor
    meta["mode"] = "chat_time_window"
    meta["truncated"] = True
    return {"anchor": anchor, "messages": sliced, "meta": meta}
