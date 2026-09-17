from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel

from workers.client import GatewayClient, GatewayError
from workers.dispatcher.agent import build_agent_executor, run_dispatcher_agent
from workers.dispatcher.llm.factory import (
    UnknownLLMProviderError,
    build_chat_model_from_env,
)
from workers.dispatcher.prompts import build_user_prompt
from workers.dispatcher.state import RunState
from workers.dispatcher.tools import build_dispatcher_tools
from workers.result import WorkerResult

PROCESS_SLEEP_SECONDS = 3.0


def _payload_str(payload: dict[str, Any], key: str) -> str:
    val = payload.get(key)
    if val is None:
        return ""
    s = str(val).strip()
    return s


def _content_summary(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if text:
            return str(text)[:800]
        return json.dumps(content, ensure_ascii=False)[:800]
    s = str(content).strip()
    if not s:
        return ""
    if s.startswith("{") or s.startswith("["):
        try:
            obj = json.loads(s)
            return _content_summary(obj)
        except json.JSONDecodeError:
            pass
    return s[:800]


class DispatcherWorker:
    """LangChain tool-calling agent: task dispatch only (no IM reply)."""

    def __init__(
        self,
        client: GatewayClient,
        *,
        llm: Optional[BaseChatModel] = None,
        max_iterations: Optional[int] = None,
        process_sleep: float = PROCESS_SLEEP_SECONDS,
    ) -> None:
        self.client = client
        self._llm = llm
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else int(os.environ.get("DISPATCHER_AGENT_MAX_ITERATIONS", "6") or "6")
        )
        self.process_sleep = process_sleep

    def _get_llm(self) -> BaseChatModel:
        if self._llm is not None:
            return self._llm
        return build_chat_model_from_env()

    def handle(self, item: dict[str, Any]) -> WorkerResult:
        inbound_id = item.get("id")
        if inbound_id is None:
            return WorkerResult.fail("missing inbound item id")
        inbound_id = int(inbound_id)

        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        bot_open_id = _payload_str(payload, "bot_open_id")
        self_open_id = _payload_str(payload, "self_open_id")
        matched = payload.get("matched_mentions") or []
        if not isinstance(matched, list):
            matched = []
        matched_ids = {str(x).strip() for x in matched if x}

        time.sleep(self.process_sleep)

        targets = {x for x in (bot_open_id, self_open_id) if x}
        if not targets or not (matched_ids & targets):
            return WorkerResult.skip("not @bot/@self")

        try:
            existing = self.client.get_dispatch_run(inbound_id)
        except GatewayError as exc:
            code = exc.status_code or 0
            if code >= 500 or code == 0:
                return WorkerResult.retry(str(exc))
            return WorkerResult.fail(str(exc))
        except Exception as exc:
            return WorkerResult.retry(str(exc))

        if existing:
            return WorkerResult.skip("already dispatched")

        state = RunState(
            inbound_id=inbound_id,
            message_id=_payload_str(payload, "message_id"),
            chat_id=_payload_str(payload, "chat_id"),
            thread_id=_payload_str(payload, "thread_id"),
            bot_id=_payload_str(payload, "bot_id"),
            sender_open_id=_payload_str(payload, "sender_open_id"),
            content_summary=_content_summary(payload.get("content")),
        )

        try:
            llm = self._get_llm()
        except (UnknownLLMProviderError, ValueError) as exc:
            return WorkerResult.fail(str(exc))
        except Exception as exc:
            return WorkerResult.fail(f"llm init failed: {exc}")

        tools = build_dispatcher_tools(self.client, state)
        executor = build_agent_executor(
            llm, tools, max_iterations=self.max_iterations
        )
        user_input = build_user_prompt(
            {
                "inbound_id": state.inbound_id,
                "message_id": state.message_id,
                "chat_id": state.chat_id,
                "thread_id": state.thread_id,
                "bot_id": state.bot_id,
                "sender_open_id": state.sender_open_id,
                "content_summary": state.content_summary,
            }
        )

        try:
            run_dispatcher_agent(executor, user_input=user_input)
        except GatewayError as exc:
            # Do not write dispatch_runs on retry/fail mid-flight — keeps inbound
            # reprocessable after config/transient fixes.
            code = exc.status_code or 0
            if code >= 500 or code == 0:
                return WorkerResult.retry(str(exc))
            return WorkerResult.fail(str(exc))
        except Exception as exc:
            msg = str(exc)
            # Permanent client/config errors (e.g. invalid model name): fail, no poison noop.
            if "invalid_request_error" in msg or "Error code: 400" in msg:
                return WorkerResult.fail(msg)
            return WorkerResult.retry(msg)

        if not state.finalized:
            self._ensure_finalize(
                state, reason="agent_ended_without_finalize"
            )

        return WorkerResult.ok()

    def _ensure_finalize(self, state: RunState, *, reason: str) -> None:
        if state.finalized:
            return
        if state.created_task_id is not None:
            decision = "create"
            task_id = state.created_task_id
        elif state.followed_up_task_id is not None:
            decision = "followup"
            task_id = state.followed_up_task_id
        else:
            decision = "noop"
            task_id = None
        evidence = {
            "forced": True,
            "message_id": state.message_id,
            "chat_id": state.chat_id,
            "thread_id": state.thread_id,
            "seen_task_ids": sorted(state.seen_task_ids),
            "tool_trace": state.tool_trace,
        }
        try:
            self.client.record_dispatch_run(
                inbound_id=state.inbound_id,
                decision=decision,
                reason=reason,
                task_id=task_id,
                evidence=evidence,
                actor="dispatcher",
            )
            state.finalized = True
            state.final_decision = decision
            state.final_task_id = task_id
            state.final_reason = reason
        except Exception as exc:
            print(
                f"[dispatcher] forced finalize failed inbound_id={state.inbound_id}: {exc}",
                flush=True,
            )
