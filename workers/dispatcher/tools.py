from __future__ import annotations

import json
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from workers.client import GatewayClient, GatewayError
from workers.dispatcher.state import (
    ALLOWED_DECISIONS,
    ALLOWED_KINDS,
    OPEN_STATUSES,
    RunState,
)


def _trim_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "one_liner": task.get("one_liner"),
        "status": task.get("status"),
        "kind": task.get("kind"),
        "chat_id": task.get("chat_id"),
        "thread_id": task.get("thread_id"),
        "project_id": task.get("project_id"),
        "bot_id": task.get("bot_id"),
        "updated_at": task.get("updated_at"),
    }


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _ok(payload: Any) -> str:
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False)


class FetchContextArgs(BaseModel):
    before: int = Field(default=8, ge=0, le=30)
    after: int = Field(default=3, ge=0, le=15)


class ListOpenTasksArgs(BaseModel):
    use_thread: bool = Field(
        default=True, description="Prefer filter by inbound thread_id when set"
    )
    use_chat: bool = Field(default=True, description="Filter by inbound chat_id")
    use_project: bool = Field(
        default=False, description="Also filter by project_id if known"
    )
    project_id: Optional[str] = Field(default=None)
    limit: int = Field(default=20, ge=1, le=50)


class GetTaskArgs(BaseModel):
    task_id: int


class CreateTaskArgs(BaseModel):
    title: str
    kind: str = "readonly"
    one_liner: str = ""
    reason: str = Field(description="Why create; will be stored on the task event")


class FollowupTaskArgs(BaseModel):
    task_id: int
    message: str = Field(description="Follow-up note including brief judgment")


class FinalizeArgs(BaseModel):
    decision: str = Field(description="create | followup | noop")
    reason: str = Field(description="One sentence: evidence → judgment → action")
    task_id: Optional[int] = None


def build_dispatcher_tools(
    client: GatewayClient, state: RunState
) -> list[StructuredTool]:
    def fetch_message_context(before: int = 8, after: int = 3) -> str:
        if not state.message_id and not state.chat_id and not state.thread_id:
            return _err("no message_id/chat_id/thread_id on inbound")
        try:
            data = client.fetch_context(
                message_id=state.message_id or None,
                chat_id=state.chat_id or None,
                thread_id=state.thread_id or None,
                before=min(max(int(before), 0), 30),
                after=min(max(int(after), 0), 15),
            )
        except GatewayError as exc:
            return _err(str(exc))
        state.note_tool("fetch_message_context", {"before": before, "after": after})
        return _ok(data)

    def get_chat_project() -> str:
        if not state.chat_id:
            return _err("inbound missing chat_id")
        try:
            item = client.get_chat_project(state.chat_id)
        except GatewayError as exc:
            return _err(str(exc))
        state.note_tool("get_chat_project", {"chat_id": state.chat_id})
        if not item:
            return _ok({"bound": False, "project_id": None})
        return _ok({"bound": True, "item": item})

    def list_open_tasks(
        use_thread: bool = True,
        use_chat: bool = True,
        use_project: bool = False,
        project_id: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        lim = min(max(int(limit), 1), 50)
        merged: dict[int, dict[str, Any]] = {}

        def _pull(**filters: Any) -> None:
            for status in OPEN_STATUSES:
                try:
                    tasks = client.list_tasks(status=status, limit=lim, **filters)
                except GatewayError:
                    continue
                for t in tasks:
                    tid = t.get("id")
                    if tid is None:
                        continue
                    tid_i = int(tid)
                    merged[tid_i] = t
                    state.mark_seen(tid_i)

        try:
            if use_thread and state.thread_id:
                _pull(thread_id=state.thread_id)
            if use_chat and state.chat_id:
                _pull(chat_id=state.chat_id)
            pid = (project_id or "").strip()
            if use_project and pid:
                _pull(project_id=pid)
            if not merged and state.chat_id:
                _pull(chat_id=state.chat_id)
        except GatewayError as exc:
            return _err(str(exc))

        items = [_trim_task(t) for t in list(merged.values())[:lim]]
        state.note_tool("list_open_tasks", {"count": len(items)})
        return _ok({"tasks": items, "count": len(items)})

    def get_task(task_id: int) -> str:
        try:
            task = client.get_task(int(task_id))
        except GatewayError as exc:
            return _err(str(exc))
        if not task:
            return _err(f"task {task_id} not found")
        state.mark_seen(int(task["id"]))
        state.note_tool("get_task", {"task_id": int(task["id"])})
        return _ok(_trim_task(task))

    def create_task(
        title: str,
        kind: str = "readonly",
        one_liner: str = "",
        reason: str = "",
    ) -> str:
        if state.has_written():
            return _err("already performed a write for this inbound")
        title_s = (title or "").strip()
        if not title_s:
            return _err("title is required")
        kind_s = (kind or "readonly").strip()
        if kind_s not in ALLOWED_KINDS:
            return _err(f"invalid kind {kind!r}; allowed: {sorted(ALLOWED_KINDS)}")
        reason_s = (reason or "").strip() or title_s
        try:
            task = client.create_task(
                title=title_s,
                kind=kind_s,
                one_liner=(one_liner or "").strip(),
                bot_id=state.bot_id or None,
                chat_id=state.chat_id or None,
                thread_id=state.thread_id or None,
                status="noted",
                created_from_inbound_id=state.inbound_id,
                actor="dispatcher",
                message=reason_s,
            )
        except GatewayError as exc:
            return _err(str(exc))
        tid = int(task["id"])
        state.created_task_id = tid
        state.mark_seen(tid)
        state.note_tool("create_task", {"task_id": tid, "title": title_s})
        return _ok({"task": _trim_task(task)})

    def followup_task(task_id: int, message: str) -> str:
        if state.has_written():
            return _err("already performed a write for this inbound")
        tid = int(task_id)
        msg = (message or "").strip()
        if not msg:
            return _err("message is required")
        if tid not in state.seen_task_ids:
            return _err(
                f"task_id {tid} not in seen set; call list_open_tasks or get_task first"
            )
        try:
            result = client.add_followup(
                tid,
                message=msg,
                event_type="note",
                actor="dispatcher",
                inbound_id=state.inbound_id,
                payload={"source": "dispatcher"},
            )
        except GatewayError as exc:
            return _err(str(exc))
        state.followed_up_task_id = tid
        state.note_tool("followup_task", {"task_id": tid})
        task = result.get("task") if isinstance(result, dict) else None
        return _ok(
            {
                "task_id": tid,
                "task": _trim_task(task) if isinstance(task, dict) else None,
            }
        )

    def finalize_dispatch(
        decision: str,
        reason: str,
        task_id: Optional[int] = None,
    ) -> str:
        if state.finalized:
            return _err("already finalized")
        decision_s = (decision or "").strip()
        if decision_s not in ALLOWED_DECISIONS:
            return _err(
                f"invalid decision {decision!r}; allowed: {sorted(ALLOWED_DECISIONS)}"
            )
        reason_s = (reason or "").strip()
        if not reason_s:
            return _err("reason is required")

        resolved_task_id: Optional[int] = None
        if decision_s == "create":
            if state.created_task_id is None:
                return _err("decision=create but create_task was not called")
            resolved_task_id = state.created_task_id
        elif decision_s == "followup":
            if state.followed_up_task_id is None:
                return _err("decision=followup but followup_task was not called")
            resolved_task_id = state.followed_up_task_id
        else:
            if state.has_written():
                return _err("decision=noop but a write already happened")
            resolved_task_id = None

        if task_id is not None and resolved_task_id is not None:
            if int(task_id) != int(resolved_task_id):
                return _err(
                    f"task_id mismatch: arg={task_id} vs write={resolved_task_id}"
                )

        evidence = {
            "message_id": state.message_id,
            "chat_id": state.chat_id,
            "thread_id": state.thread_id,
            "seen_task_ids": sorted(state.seen_task_ids),
            "tool_trace": state.tool_trace,
            "content_summary": state.content_summary[:500],
        }
        try:
            run = client.record_dispatch_run(
                inbound_id=state.inbound_id,
                decision=decision_s,
                reason=reason_s,
                task_id=resolved_task_id,
                evidence=evidence,
                actor="dispatcher",
            )
        except GatewayError as exc:
            return _err(str(exc))

        state.finalized = True
        state.final_decision = decision_s
        state.final_task_id = resolved_task_id
        state.final_reason = reason_s
        state.note_tool("finalize_dispatch", {"decision": decision_s})
        return _ok({"run": run})

    return [
        StructuredTool.from_function(
            name="fetch_message_context",
            description="Fetch Feishu message context around the inbound message.",
            func=fetch_message_context,
            args_schema=FetchContextArgs,
        ),
        StructuredTool.from_function(
            name="get_chat_project",
            description="Get project_id bound to the inbound chat, if any.",
            func=get_chat_project,
        ),
        StructuredTool.from_function(
            name="list_open_tasks",
            description="List non-terminal tasks filtered by thread/chat/project.",
            func=list_open_tasks,
            args_schema=ListOpenTasksArgs,
        ),
        StructuredTool.from_function(
            name="get_task",
            description="Get one task by id (marks it as seen for followup).",
            func=get_task,
            args_schema=GetTaskArgs,
        ),
        StructuredTool.from_function(
            name="create_task",
            description="Create a new noted task for this inbound (once).",
            func=create_task,
            args_schema=CreateTaskArgs,
        ),
        StructuredTool.from_function(
            name="followup_task",
            description="Append a note followup to a previously seen open task (once).",
            func=followup_task,
            args_schema=FollowupTaskArgs,
        ),
        StructuredTool.from_function(
            name="finalize_dispatch",
            description="Record the final decision+reason audit trail (required once).",
            func=finalize_dispatch,
            args_schema=FinalizeArgs,
        ),
    ]
