from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from app.infra.db import ROOT

DEFAULT_WORKSPACE_ROOT = ROOT / "data" / "task_workspace"


def workspace_root(path: Optional[str | Path] = None) -> Path:
    if path is not None:
        return Path(path)
    raw = os.environ.get("GATEWAY_TASK_WORKSPACE", "").strip()
    return Path(raw) if raw else DEFAULT_WORKSPACE_ROOT


class TaskWorkspace:
    """Filesystem layout under data/task_workspace/<task_id>/."""

    def __init__(self, root: Optional[str | Path] = None) -> None:
        self.root = workspace_root(root)

    def task_dir(self, task_id: int) -> Path:
        return self.root / str(task_id)

    def relative_path(self, task_id: int) -> str:
        return str(self.task_dir(task_id))

    def init_task(
        self,
        task_id: int,
        *,
        title: str,
        kind: str,
        status: str,
        one_liner: str = "",
        bot_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        created_at: str,
        created_event: dict[str, Any],
    ) -> Path:
        d = self.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "scratch").mkdir(exist_ok=True)
        (d / "artifacts").mkdir(exist_ok=True)

        lines = [
            f"# Task {task_id}: {title}",
            "",
            f"- kind: `{kind}`",
            f"- status: `{status}`",
            f"- one_liner: {one_liner or '(none)'}",
            f"- bot_id: `{bot_id or ''}`",
            f"- chat_id: `{chat_id or ''}`",
            f"- thread_id: `{thread_id or ''}`",
            f"- project_id: `{project_id or ''}`",
            f"- board_chat_id: ``",
            f"- board_message_id: ``",
            f"- board_thread_id: ``",
            f"- created_at: `{created_at}`",
            "",
            "## Updates",
            "",
        ]
        (d / "TASK.md").write_text("\n".join(lines), encoding="utf-8")
        self.append_timeline(task_id, created_event)
        return d

    def write_board_meta(
        self,
        task_id: int,
        *,
        board_chat_id: Optional[str] = None,
        board_message_id: Optional[str] = None,
        board_thread_id: Optional[str] = None,
        board_sync_error: Optional[str] = None,
    ) -> None:
        """Append board field snapshot (append-only; does not rewrite header)."""
        d = self.task_dir(task_id)
        md = d / "TASK.md"
        parts = [
            f"- board_chat_id: `{board_chat_id or ''}`",
            f"- board_message_id: `{board_message_id or ''}`",
            f"- board_thread_id: `{board_thread_id or ''}`",
        ]
        if board_sync_error:
            parts.append(f"- board_sync_error: {board_sync_error}")
        with md.open("a", encoding="utf-8") as f:
            f.write("\n".join(parts) + "\n")

    def append_timeline(self, task_id: int, event: dict[str, Any]) -> None:
        d = self.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "timeline.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def append_update(
        self,
        task_id: int,
        *,
        created_at: str,
        message: str,
        event_type: str,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        actor: str = "api",
    ) -> None:
        d = self.task_dir(task_id)
        md = d / "TASK.md"
        status_bit = ""
        if from_status or to_status:
            status_bit = f" [{from_status or '?'} -> {to_status or '?'}]"
        line = (
            f"- `{created_at}` ({event_type}/{actor}){status_bit}: {message}\n"
        )
        with md.open("a", encoding="utf-8") as f:
            f.write(line)

    def write_clues(self, task_id: int, clues: list[dict[str, Any]]) -> None:
        """Overwrite clues.json snapshot (not append-only TASK.md)."""
        d = self.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "clues.json"
        path.write_text(
            json.dumps(clues, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
