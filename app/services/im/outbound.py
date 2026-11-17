from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO, Optional

from app.core.settings import user_cli_profile_args
from app.infra.lark.cli import LarkCliError, run_cli, run_cli_async


@dataclass(frozen=True)
class UploadAttachment:
    kind: str
    filename: str
    file: BinaryIO


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


def _structured_content_args(
    message: dict[str, Any], mention_open_ids: Optional[list[str]]
) -> list[str]:
    kind = message["type"]
    if kind == "markdown":
        markdown = compose_text(message["text"], mention_open_ids)
        kind = "post"
        content = {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}
    elif kind == "image":
        content = {"image_key": message["image_key"]}
    elif kind == "card":
        kind = "interactive"
        content = message["content"]
    elif kind in ("file", "audio", "video"):
        content = {"file_key": message["file_key"]}
        if kind == "video":
            kind = "media"
            if message.get("image_key") is not None:
                content["image_key"] = message["image_key"]
    else:
        raise ValueError(f"unsupported outbound message type: {kind}")
    return ["--msg-type", kind, "--content", json.dumps(content, ensure_ascii=False)]


def _safe_attachment_name(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip()
    if name in {"", ".", ".."}:
        return "attachment"
    if len(name.encode("utf-8")) <= 255:
        return name
    suffix = Path(name).suffix
    ending = suffix.encode("utf-8")[:32].decode("utf-8", errors="ignore")
    stem = name.removesuffix(suffix).encode("utf-8")
    return stem[: 255 - len(ending.encode("utf-8"))].decode(
        "utf-8", errors="ignore"
    ) + ending


def _run_upload(
    args: list[str], options: list[str], upload: UploadAttachment
) -> dict:
    filename = _safe_attachment_name(upload.filename)
    relative_path = f"./{filename}"
    try:
        with TemporaryDirectory(prefix="lark-im-") as temporary:
            directory = Path(temporary)
            staged_path = directory / filename
            with staged_path.open("wb") as staged:
                shutil.copyfileobj(upload.file, staged)
            try:
                return run_cli(
                    [*args, f"--{upload.kind}", relative_path, *options],
                    cwd=directory,
                )
            except LarkCliError as exc:

                def safe_error(value: str) -> str:
                    return (
                        value.replace(str(staged_path), "<attachment>")
                        .replace(str(directory), "<upload>")
                        .replace(relative_path, "<attachment>")
                    )

                raise LarkCliError(
                    safe_error(str(exc)),
                    returncode=exc.returncode,
                    stdout=safe_error(exc.stdout),
                    stderr=safe_error(exc.stderr),
                ) from exc
    except OSError as exc:
        raise LarkCliError(
            "could not process uploaded attachment", returncode=1, stdout="", stderr=""
        ) from exc


async def _run_outbound(
    args: list[str],
    *,
    text: str,
    markdown: bool,
    mention_open_ids: Optional[list[str]],
    message: Optional[dict[str, Any]],
    upload: Optional[UploadAttachment],
    reply_in_thread: bool = False,
    idempotency_key: Optional[str] = None,
) -> dict:
    options = ["--reply-in-thread"] if reply_in_thread else []
    if idempotency_key:
        options.extend(["--idempotency-key", idempotency_key[:50]])

    if upload is not None:
        operation = asyncio.get_running_loop().run_in_executor(
            None, _run_upload, args, options, upload
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Finish reading the caller's stream before cancellation can close it.
            while not operation.done():
                try:
                    await asyncio.wait({operation})
                except asyncio.CancelledError:
                    continue
            if not operation.cancelled():
                operation.exception()
            raise

    if message is None:
        content = compose_text(text, mention_open_ids)
        content_args = ["--markdown" if markdown else "--text", content]
    else:
        content_args = _structured_content_args(message, mention_open_ids)
    return await run_cli_async([*args, *content_args, *options])


async def reply_message(
    *,
    message_id: str,
    text: str = "",
    mention_open_ids: Optional[list[str]] = None,
    reply_in_thread: bool = False,
    profile: Optional[str] = None,
    as_user: bool = False,
    markdown: bool = False,
    idempotency_key: Optional[str] = None,
    message: Optional[dict[str, Any]] = None,
    upload: Optional[UploadAttachment] = None,
) -> dict:
    args = [
        "im",
        "+messages-reply",
        *_identity_args(as_user=as_user, profile=profile),
        "--message-id",
        message_id,
    ]
    return await _run_outbound(
        args,
        text=text,
        markdown=markdown,
        mention_open_ids=mention_open_ids,
        message=message,
        upload=upload,
        reply_in_thread=reply_in_thread,
        idempotency_key=idempotency_key,
    )


async def respond_to_message(
    *,
    message_id: str,
    text: str = "",
    profile: str,
    thread_id: Optional[str] = None,
    sender_open_id: Optional[str] = None,
    mention_open_ids: Optional[list[str]] = None,
    idempotency_key: Optional[str] = None,
    message: Optional[dict[str, Any]] = None,
    upload: Optional[UploadAttachment] = None,
) -> dict:
    """Reply in the inbound thread and mention the sender for text and Markdown."""
    mentions = None
    if upload is None and (message is None or message["type"] == "markdown"):
        mentions = merge_mention_open_ids(sender_open_id, mention_open_ids)
    return await reply_message(
        message_id=message_id,
        text=text,
        mention_open_ids=mentions,
        reply_in_thread=should_reply_in_thread(thread_id),
        profile=profile,
        idempotency_key=idempotency_key,
        message=message,
        upload=upload,
    )


async def send_message(
    *,
    chat_id: str,
    text: str = "",
    mention_open_ids: Optional[list[str]] = None,
    profile: Optional[str] = None,
    as_user: bool = False,
    markdown: bool = False,
    idempotency_key: Optional[str] = None,
    message: Optional[dict[str, Any]] = None,
    upload: Optional[UploadAttachment] = None,
) -> dict:
    args = [
        "im",
        "+messages-send",
        *_identity_args(as_user=as_user, profile=profile),
        "--chat-id",
        chat_id,
    ]
    return await _run_outbound(
        args,
        text=text,
        markdown=markdown,
        mention_open_ids=mention_open_ids,
        message=message,
        upload=upload,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "LarkCliError",
    "UploadAttachment",
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
