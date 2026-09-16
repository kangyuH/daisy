from __future__ import annotations

from typing import Optional

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


async def reply_message(
    *,
    message_id: str,
    text: str,
    mention_open_ids: Optional[list[str]] = None,
    reply_in_thread: bool = False,
    profile: str,
    idempotency_key: Optional[str] = None,
) -> dict:
    content = compose_text(text, mention_open_ids)
    args = [
        "im",
        "+messages-reply",
        "--as",
        "bot",
        "--profile",
        profile,
        "--message-id",
        message_id,
        "--text",
        content,
    ]
    if reply_in_thread:
        args.append("--reply-in-thread")
    if idempotency_key:
        args.extend(["--idempotency-key", idempotency_key[:50]])
    return await run_cli_async(args)


async def send_message(
    *,
    chat_id: str,
    text: str,
    mention_open_ids: Optional[list[str]] = None,
    profile: str,
    idempotency_key: Optional[str] = None,
) -> dict:
    content = compose_text(text, mention_open_ids)
    args = [
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--profile",
        profile,
        "--chat-id",
        chat_id,
        "--text",
        content,
    ]
    if idempotency_key:
        args.extend(["--idempotency-key", idempotency_key[:50]])
    return await run_cli_async(args)


__all__ = [
    "LarkCliError",
    "build_mention_prefix",
    "compose_text",
    "reply_message",
    "send_message",
]
