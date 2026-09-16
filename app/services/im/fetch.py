from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.infra.lark.cli import run_cli_async

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


async def list_thread_messages(
    thread: str,
    *,
    page_size: int = 50,
    order: str = "asc",
) -> list[dict[str, Any]]:
    args = [
        "im",
        "+threads-messages-list",
        "--as",
        "user",
        "--thread",
        thread,
        "--page-size",
        str(max(1, min(page_size, 500))),
        "--order",
        order,
        "--no-reactions",
        *_profile_args(),
    ]
    result = await run_cli_async(args)
    return _extract_messages(result)


def _slice_around(
    messages: list[dict[str, Any]],
    *,
    anchor_id: Optional[str],
    before: int,
    after: int,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    messages = sorted(
        messages,
        key=lambda m: (_msg_time(m) or datetime.min.replace(tzinfo=TZ_CN), _msg_id(m) or ""),
    )
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
        found = await mget_messages([message_id])
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
        msgs = await list_thread_messages(resolved_thread, page_size=min(500, max(50, before + after + 1)))
        sliced, found_anchor = _slice_around(
            msgs, anchor_id=message_id, before=before, after=after
        )
        if found_anchor:
            anchor = found_anchor
        meta["mode"] = "thread"
        return {"anchor": anchor, "messages": sliced, "meta": meta}

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
