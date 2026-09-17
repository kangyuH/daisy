from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Set


OPEN_STATUSES = (
    "noted",
    "waiting_human",
    "waiting_external",
    "agent_running",
    "blocked",
)

ALLOWED_KINDS = frozenset({"readonly", "operational"})
ALLOWED_DECISIONS = frozenset({"create", "followup", "noop"})


@dataclass
class RunState:
    """Per-inbound mutable state shared by tools."""

    inbound_id: int
    message_id: str = ""
    chat_id: str = ""
    thread_id: str = ""
    bot_id: str = ""
    sender_open_id: str = ""
    content_summary: str = ""

    seen_task_ids: Set[int] = field(default_factory=set)
    created_task_id: Optional[int] = None
    followed_up_task_id: Optional[int] = None
    finalized: bool = False
    final_decision: Optional[str] = None
    final_task_id: Optional[int] = None
    final_reason: str = ""
    tool_trace: list[dict[str, Any]] = field(default_factory=list)

    def note_tool(self, name: str, detail: Any = None) -> None:
        self.tool_trace.append({"tool": name, "detail": detail})

    def mark_seen(self, task_id: int) -> None:
        self.seen_task_ids.add(int(task_id))

    def has_written(self) -> bool:
        return self.created_task_id is not None or self.followed_up_task_id is not None
