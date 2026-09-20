from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.services.agent.constants import LEDGER_COMMANDS

_CMD_RE = re.compile(
    r"^/(research|plan|exec|stop|status)(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)


def extract_plain_text(content: Any) -> str:
    """Best-effort plain text from Feishu message content (str or JSON)."""
    if content is None:
        return ""
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
        return json.dumps(content, ensure_ascii=False)
    s = str(content).strip()
    if not s:
        return ""
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return s
        if isinstance(obj, dict):
            text = obj.get("text")
            if isinstance(text, str):
                return text.strip()
    return s


def parse_ledger_command(text: str) -> Optional[dict[str, str]]:
    """
    Parse a ledger slash command.

    Returns {"cmd": "research"|"plan"|"exec"|"stop"|"status", "rest": "..."}
    or None if not a supported command.
    """
    raw = (text or "").strip()
    # Strip leading @mentions like <at user_id="...">name</at>
    raw = re.sub(r"<at[^>]*>.*?</at>", "", raw, flags=re.I | re.S).strip()
    m = _CMD_RE.match(raw)
    if not m:
        return None
    cmd = m.group(1).lower()
    if cmd not in LEDGER_COMMANDS:
        return None
    rest = (m.group(2) or "").strip()
    return {"cmd": cmd, "rest": rest}
