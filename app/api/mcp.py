from __future__ import annotations

import asyncio
import os
import secrets
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, StringConstraints
from starlette.datastructures import Headers, State
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.im_payload import Message, OutboundRequest, RespondBody
from app.infra.lark.cli import LarkCliError
from app.services.im import gateway as im
from app.services.tasks import gateway as tasks
from app.services.tasks.board import TaskBoardSync
from app.services.tasks.models import TaskConflictError, TaskValidationError

PositiveId = Annotated[int, Field(gt=0)]
Count = Annotated[int, Field(ge=1, le=100)]
ContextCount = Annotated[int, Field(ge=0, le=50)]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FollowupKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ReplyKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]

_TASK_FIELDS = (
    "id", "title", "kind", "status", "one_liner", "bot_id", "chat_id", "thread_id",
    "project_id", "created_from_inbound_id", "created_at", "updated_at", "closed_at",
    "board_chat_id", "board_message_id", "board_thread_id", "contract_revision",
    "contract", "clues", "events",
)


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    return {key: task[key] for key in _TASK_FIELDS if key in task}


def _env_list(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


class _BearerAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.authorization = f"Bearer {token}".encode() if token else b""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.authorization:
            response = JSONResponse({"detail": "MCP access is disabled"}, status_code=503)
        elif not secrets.compare_digest(
            Headers(scope=scope).get("authorization", "").encode(), self.authorization
        ):
            response = JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


def build_mcp(state: State) -> tuple[MCPServer, ASGIApp]:
    server = MCPServer("lark-superbot")
    allowed_chats = frozenset(_env_list("GATEWAY_MCP_CHAT_IDS"))

    async def bound_bot(chat_id: str | None) -> str:
        if not chat_id or (allowed_chats and chat_id not in allowed_chats):
            raise ToolError("Chat is outside this MCP client's scope")
        binding = await asyncio.to_thread(state.bot_store.get_chat_binding, chat_id)
        if not binding:
            raise ToolError("Chat is outside this MCP client's scope")
        return str(binding["bot_id"])

    async def scoped_task(task_id: int, *, events_limit: int = 1) -> dict[str, Any]:
        try:
            result = await tasks.get_task(state.task_store, task_id, events_limit=events_limit)
            task = result["task"]
            await bound_bot(task.get("chat_id"))
        except (KeyError, ToolError) as exc:
            raise ToolError("Task is outside this MCP client's scope or does not exist") from exc
        return task

    async def scoped_inbound(inbound_id: int) -> im.InboundMessage:
        denied = "Inbound message is outside this MCP client's scope or does not exist"
        try:
            inbound = await im.get_inbound_message(state.queue, inbound_id)
            bot_id = await bound_bot(inbound.chat_id)
        except (im.ImGatewayError, ToolError) as exc:
            raise ToolError(denied) from exc
        if inbound.bot_id != bot_id:
            raise ToolError(denied)
        return inbound

    @server.tool()
    async def list_tasks(
        chat_id: NonBlank, status: str | None = None, limit: Count = 50
    ) -> dict[str, Any]:
        """List recent tasks for a bound Feishu business chat."""
        await bound_bot(chat_id)
        try:
            result = await tasks.list_tasks(state.task_store, chat_id=chat_id, status=status, limit=limit)
        except TaskValidationError as exc:
            raise ToolError(str(exc)) from exc
        return {"ok": True, "tasks": [_task_view(task) for task in result["tasks"]]}

    @server.tool()
    async def get_task(task_id: PositiveId, events_limit: Count = 20) -> dict[str, Any]:
        """Read a task, its current contract and recent followups."""
        task = await scoped_task(task_id, events_limit=events_limit)
        return {"ok": True, "task": _task_view(task)}

    @server.tool()
    async def get_message_context(
        inbound_id: PositiveId, before: ContextCount = 8, after: ContextCount = 3
    ) -> dict[str, Any]:
        """Read Feishu context around a Gateway inbound message, including its direct reply target."""
        inbound = await scoped_inbound(inbound_id)
        try:
            result = await im.get_message_context(
                state.message_task_store,
                message_id=inbound.message_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
                task_chat_id=inbound.chat_id,
                reply_to_message_id=inbound.reply_to_message_id,
                before=before,
                after=after,
            )
        except im.ImGatewayError as exc:
            raise ToolError(str(exc)) from exc
        except LarkCliError as exc:
            raise ToolError("Feishu context retrieval failed") from exc
        messages = [result["anchor"], result["reply_target"], *result["messages"]]
        if any(m and m.get("chat_id") not in (None, inbound.chat_id) for m in messages):
            raise ToolError("Feishu context does not match the inbound chat")
        return result

    @server.tool()
    async def append_task_followup(
        task_id: PositiveId, message: NonBlank, idempotency_key: FollowupKey
    ) -> dict[str, Any]:
        """Append a note to a task and sync the ledger. Reuse the same key when retrying."""
        await scoped_task(task_id)
        board = TaskBoardSync(
            state.task_store,
            chat_id=state.calibration_chat_id,
            enabled=state.task_board_enabled,
            mention_user_open_id=state.auth_user_open_id,
        )
        try:
            result = await tasks.append_followup(
                state.task_store,
                board,
                task_id,
                message=message,
                actor="mcp",
                idempotency_key=idempotency_key,
            )
        except (TaskConflictError, TaskValidationError) as exc:
            raise ToolError(str(exc)) from exc
        board_result = result["board_sync"]
        board_sync = {
            key: board_result[key]
            for key in ("ok", "skipped", "reason", "message_id", "board_message_id", "board_thread_id")
            if key in board_result
        }
        if not board_result["ok"]:
            board_sync["error"] = "Feishu ledger sync failed. Retry with the same idempotency key."
        return {**result, "task": _task_view(result["task"]), "board_sync": board_sync}

    @server.tool()
    async def reply_to_inbound(
        inbound_id: PositiveId,
        text: NonBlank | None = None,
        message: Message | None = None,
        idempotency_key: ReplyKey | None = None,
    ) -> dict[str, Any]:
        """Reply as the bound bot to an inbound message. Supply text or a structured message."""
        if (text is None) == (message is None):
            raise ToolError("Supply either text or a structured message")
        inbound = await scoped_inbound(inbound_id)
        content = {"message": message} if message is not None else {"text": text}
        try:
            body = RespondBody(inbound_id=inbound_id, **content)
            return await im.respond_to_inbound(
                inbound,
                profile=inbound.bot_id,
                task_store=state.task_store,
                message_task_store=state.message_task_store,
                content=OutboundRequest(body).content_kwargs(),
                idempotency_key=idempotency_key,
                inferred_task_chat_id=inbound.chat_id,
            )
        except im.ImGatewayError as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError("Invalid Feishu reply content") from exc
        except LarkCliError as exc:
            raise ToolError("Feishu reply failed") from exc

    http_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[
                "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*",
                *_env_list("GATEWAY_MCP_ALLOWED_HOSTS"),
            ],
            allowed_origins=_env_list("GATEWAY_MCP_ALLOWED_ORIGINS"),
        ),
    )
    return server, _BearerAuth(http_app, os.environ.get("GATEWAY_MCP_TOKEN", "").strip())
