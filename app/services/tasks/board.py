from __future__ import annotations

from typing import Any, Optional

from app.core.settings import calibration_chat_id
from app.infra.lark.cli import LarkCliError
from app.services.im.outbound import (
    extract_message_id,
    extract_thread_id,
    reply_message,
    send_message,
)
from app.services.tasks.store import TaskStore


def _board_root_text(task: dict[str, Any]) -> str:
    tid = task.get("id")
    title = task.get("title") or ""
    kind = task.get("kind") or ""
    status = task.get("status") or ""
    one = task.get("one_liner") or ""
    project = task.get("project_id") or ""
    lines = [
        f"**[task #{tid}] {title}**",
        f"- kind: `{kind}`",
        f"- status: `{status}`",
    ]
    if one:
        lines.append(f"- one_liner: {one}")
    if project:
        lines.append(f"- project_id: `{project}`")
    return "\n".join(lines)


def _board_open_thread_text(task: dict[str, Any]) -> str:
    return f"台账已创建 · status=`{task.get('status') or ''}`"


def _followup_text(event: dict[str, Any]) -> str:
    et = event.get("event_type") or "note"
    actor = event.get("actor") or "api"
    msg = event.get("message") or ""
    from_s = event.get("from_status")
    to_s = event.get("to_status")
    status_bit = ""
    if from_s or to_s:
        status_bit = f" `{from_s or '?'}` → `{to_s or '?'}`"
    header = f"**[{et}/{actor}]**{status_bit}"
    return f"{header}\n\n{msg}".strip()


class TaskBoardSync:
    """Sync task ledger posts to calibration chat as CLI default-app bot."""

    def __init__(
        self,
        store: TaskStore,
        *,
        chat_id: Optional[str] = None,
        enabled: bool = True,
        mention_user_open_id: Optional[str] = None,
    ) -> None:
        self.store = store
        self.chat_id = (chat_id or calibration_chat_id()).strip()
        self.enabled = enabled
        self.mention_user_open_id = (mention_user_open_id or "").strip() or None

    def _thread_mentions(self) -> list[str]:
        return [self.mention_user_open_id] if self.mention_user_open_id else []

    async def ensure_board(self, task: dict[str, Any]) -> dict[str, Any]:
        """Create root + open thread if missing. Never raises; returns board_sync info."""
        if not self.enabled:
            return {"ok": True, "skipped": True, "reason": "disabled"}

        tid = int(task["id"])
        if task.get("board_message_id"):
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_has_board",
                "board_message_id": task.get("board_message_id"),
                "board_thread_id": task.get("board_thread_id"),
            }

        try:
            # CLI default app bot — as_user=False, no --profile.
            send_res = await send_message(
                chat_id=self.chat_id,
                text=_board_root_text(task),
                as_user=False,
                profile=None,
                markdown=True,
                idempotency_key=f"task-board-{tid}"[:50],
            )
            root_mid = extract_message_id(send_res)
            if not root_mid:
                raise RuntimeError(
                    f"board send missing message_id: {str(send_res)[:300]}"
                )

            reply_res = await reply_message(
                message_id=root_mid,
                text=_board_open_thread_text(task),
                mention_open_ids=self._thread_mentions(),
                reply_in_thread=True,
                as_user=False,
                profile=None,
                markdown=True,
                idempotency_key=f"task-board-open-{tid}"[:50],
            )
            thread_id = extract_thread_id(reply_res) or extract_thread_id(send_res)

            updated = self.store.update_board_fields(
                tid,
                board_chat_id=self.chat_id,
                board_message_id=root_mid,
                board_thread_id=thread_id,
                clear_sync_error=True,
            )
            self.store.workspace.write_board_meta(
                tid,
                board_chat_id=self.chat_id,
                board_message_id=root_mid,
                board_thread_id=thread_id,
            )
            return {
                "ok": True,
                "board_message_id": root_mid,
                "board_thread_id": thread_id,
                "task": updated,
            }
        except (LarkCliError, Exception) as exc:
            err = str(exc)[:2000]
            try:
                self.store.update_board_fields(tid, board_sync_error=err)
                self.store.workspace.write_board_meta(
                    tid, board_sync_error=err
                )
            except Exception:
                pass
            return {"ok": False, "error": err}

    async def sync_followup(
        self, task: dict[str, Any], event: dict[str, Any]
    ) -> dict[str, Any]:
        """Ensure board exists, then post followup into thread. Never raises."""
        if not self.enabled:
            return {"ok": True, "skipped": True, "reason": "disabled"}

        ensure = await self.ensure_board(task)
        # refresh task after ensure
        fresh = self.store.get_task(int(task["id"]), events_limit=1)
        if not fresh:
            return {"ok": False, "error": "task disappeared", "ensure": ensure}
        task = fresh

        if not task.get("board_message_id"):
            return {
                "ok": False,
                "error": task.get("board_sync_error")
                or (ensure.get("error") if not ensure.get("ok") else "no board"),
                "ensure": ensure,
            }

        try:
            reply_res = await reply_message(
                message_id=str(task["board_message_id"]),
                text=_followup_text(event),
                reply_in_thread=True,
                as_user=False,
                profile=None,
                markdown=True,
                idempotency_key=f"task-fu-{task['id']}-{event.get('id')}"[:50],
            )
            thread_id = extract_thread_id(reply_res) or task.get("board_thread_id")
            updates: dict[str, Any] = {"clear_sync_error": True}
            if thread_id and thread_id != task.get("board_thread_id"):
                updates["board_thread_id"] = thread_id
            updated = self.store.update_board_fields(int(task["id"]), **updates)
            return {
                "ok": True,
                "ensure": ensure,
                "board_message_id": updated.get("board_message_id"),
                "board_thread_id": updated.get("board_thread_id"),
                "task": updated,
            }
        except (LarkCliError, Exception) as exc:
            err = str(exc)[:2000]
            try:
                self.store.update_board_fields(
                    int(task["id"]), board_sync_error=err
                )
            except Exception:
                pass
            return {"ok": False, "error": err, "ensure": ensure}
