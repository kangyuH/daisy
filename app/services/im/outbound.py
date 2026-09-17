from __future__ import annotations

from typing import Any, Optional

from app.core.settings import user_cli_profile_args
from app.infra.lark.cli import LarkCliError, run_cli_async


def build_mention_prefix(open_ids: list[str] | None) -> str:
    if not open_ids:
        return ""
    parts = [f'<at user_id="{oid}"></at>' for oid in open_ids if oid]
    if not parts:
        return ""
    return " ".join(parts) + "\n"


def compose_text(text: str, mention_open_ids: list[str] | None) -> str:
    prefix = build_mention_prefix(mention_open_ids)
    body = text if text is not None else ""
    return f"{prefix}{body}"


def merge_mention_open_ids(
    sender_open_id: Optional[str],
    extra: list[str] | None = None,
) -> list[str]:
    """Sender first (if present), then extra in order, deduped."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(oid: Optional[str]) -> None:
        if not oid:
            return
        s = str(oid).strip()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    _add(sender_open_id)
    for oid in extra or []:
        _add(oid)
    return out


def should_reply_in_thread(thread_id: Optional[str]) -> bool:
    return bool(thread_id and str(thread_id).strip())


def _identity_args(*, as_user: bool, profile: Optional[str]) -> list[str]:
    """Build --as / --profile args.

    as_user=True → user identity (optional LARK_USER_PROFILE).
    as_user=False + profile → named bot profile (Gateway bot id / lark-cli profile).
    as_user=False + no profile → CLI default-app bot.
    """
    if as_user:
        return ["--as", "user", *user_cli_profile_args()]
    args = ["--as", "bot"]
    if profile:
        args.extend(["--profile", profile])
    return args


def extract_message_id(result: Any) -> Optional[str]:
    """Best-effort message_id from lark-cli JSON."""
    if not isinstance(result, dict):
        return None
    for key in ("message_id", "messageId"):
        val = result.get(key)
        if val:
            return str(val).strip() or None
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("message_id", "messageId"):
            val = data.get(key)
            if val:
                return str(val).strip() or None
        msg = data.get("message")
        if isinstance(msg, dict):
            for key in ("message_id", "messageId", "id"):
                val = msg.get(key)
                if val:
                    return str(val).strip() or None
    return None


def extract_thread_id(result: Any) -> Optional[str]:
    """Best-effort thread_id from lark-cli JSON."""
    if not isinstance(result, dict):
        return None
    for key in ("thread_id", "threadId"):
        val = result.get(key)
        if val:
            return str(val).strip() or None
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("thread_id", "threadId"):
            val = data.get(key)
            if val:
                return str(val).strip() or None
        msg = data.get("message")
        if isinstance(msg, dict):
            for key in ("thread_id", "threadId"):
                val = msg.get(key)
                if val:
                    return str(val).strip() or None
    return None


async def reply_message(
    *,
    message_id: str,
    text: str,
    mention_open_ids: Optional[list[str]] = None,
    reply_in_thread: bool = False,
    profile: Optional[str] = None,
    as_user: bool = False,
    markdown: bool = False,
    idempotency_key: Optional[str] = None,
) -> dict:
    content = compose_text(text, mention_open_ids)
    args = [
        "im",
        "+messages-reply",
        *_identity_args(as_user=as_user, profile=profile),
        "--message-id",
        message_id,
    ]
    if markdown:
        args.extend(["--markdown", content])
    else:
        args.extend(["--text", content])
    if reply_in_thread:
        args.append("--reply-in-thread")
    if idempotency_key:
        args.extend(["--idempotency-key", idempotency_key[:50]])
    return await run_cli_async(args)


async def respond_to_message(
    *,
    message_id: str,
    text: str,
    profile: str,
    thread_id: Optional[str] = None,
    sender_open_id: Optional[str] = None,
    mention_open_ids: Optional[list[str]] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Controlled reply: auto thread flag + sender-first mention merge."""
    mentions = merge_mention_open_ids(sender_open_id, mention_open_ids)
    return await reply_message(
        message_id=message_id,
        text=text,
        mention_open_ids=mentions,
        reply_in_thread=should_reply_in_thread(thread_id),
        profile=profile,
        idempotency_key=idempotency_key,
    )


async def send_message(
    *,
    chat_id: str,
    text: str,
    mention_open_ids: Optional[list[str]] = None,
    profile: Optional[str] = None,
    as_user: bool = False,
    markdown: bool = False,
    idempotency_key: Optional[str] = None,
) -> dict:
    content = compose_text(text, mention_open_ids)
    args = [
        "im",
        "+messages-send",
        *_identity_args(as_user=as_user, profile=profile),
        "--chat-id",
        chat_id,
    ]
    if markdown:
        args.extend(["--markdown", content])
    else:
        args.extend(["--text", content])
    if idempotency_key:
        args.extend(["--idempotency-key", idempotency_key[:50]])
    return await run_cli_async(args)


__all__ = [
    "LarkCliError",
    "build_mention_prefix",
    "compose_text",
    "merge_mention_open_ids",
    "should_reply_in_thread",
    "extract_message_id",
    "extract_thread_id",
    "reply_message",
    "respond_to_message",
    "send_message",
]
