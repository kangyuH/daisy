from __future__ import annotations

import json
from typing import Any, Optional

from app.infra.lark.cli import run_cli_async

# Feishu emoji_type for 「敲键盘」
TYPING_EMOJI_TYPE = "Typing"


def _reaction_id_from_result(result: dict[str, Any]) -> Optional[str]:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(data, dict):
        return None
    rid = data.get("reaction_id") or data.get("id")
    if rid is None and isinstance(data.get("reaction"), dict):
        rid = data["reaction"].get("reaction_id") or data["reaction"].get("id")
    s = str(rid).strip() if rid is not None else ""
    return s or None


async def add_reaction(
    *,
    message_id: str,
    emoji_type: str,
    profile: str,
) -> str:
    """Add a message reaction as bot; return reaction_id."""
    data = json.dumps({"reaction_type": {"emoji_type": emoji_type}}, ensure_ascii=False)
    result = await run_cli_async(
        [
            "im",
            "reactions",
            "create",
            "--as",
            "bot",
            "--profile",
            profile,
            "--message-id",
            message_id,
            "--data",
            data,
        ]
    )
    rid = _reaction_id_from_result(result)
    if not rid:
        raise RuntimeError(f"reactions.create missing reaction_id: {result!r}")
    return rid


async def delete_reaction(
    *,
    message_id: str,
    reaction_id: str,
    profile: str,
) -> dict[str, Any]:
    """Remove a reaction previously added by this bot."""
    return await run_cli_async(
        [
            "im",
            "reactions",
            "delete",
            "--as",
            "bot",
            "--profile",
            profile,
            "--message-id",
            message_id,
            "--reaction-id",
            reaction_id,
        ]
    )


async def add_typing_reaction(*, message_id: str, profile: str) -> str:
    return await add_reaction(
        message_id=message_id,
        emoji_type=TYPING_EMOJI_TYPE,
        profile=profile,
    )


__all__ = [
    "TYPING_EMOJI_TYPE",
    "add_reaction",
    "delete_reaction",
    "add_typing_reaction",
]
