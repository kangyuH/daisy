from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.services.queue.service import QUEUE_INBOUND


class GatewayError(Exception):
    def __init__(self, message: str, *, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.request(method, url, json=json, headers=self._headers())
        if resp.status_code >= 400:
            raise GatewayError(
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return resp.json()

    def claim(
        self,
        queue: str = QUEUE_INBOUND,
        *,
        limit: int = 1,
        claimed_by: str,
    ) -> list[dict]:
        data = self._request(
            "POST",
            f"/queue/{queue}/claim",
            json={"limit": limit, "claimed_by": claimed_by},
        )
        return list(data.get("items") or [])

    def ack(self, queue: str, item_id: int, *, error: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"id": item_id}
        if error:
            body["error"] = error
        return self._request("POST", f"/queue/{queue}/ack", json=body)

    def nack(
        self,
        queue: str,
        item_id: int,
        *,
        requeue: bool = True,
        error: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"id": item_id, "requeue": requeue}
        if error:
            body["error"] = error
        return self._request("POST", f"/queue/{queue}/nack", json=body)

    def respond(
        self,
        *,
        inbound_id: int,
        text: str,
        mention_open_ids: Optional[list[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "inbound_id": inbound_id,
            "text": text,
            "mention_open_ids": mention_open_ids or [],
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return self._request("POST", "/im/respond", json=body)

    def fetch_context(
        self,
        *,
        message_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        before: int = 10,
        after: int = 5,
    ) -> dict:
        body: dict[str, Any] = {
            "before": before,
            "after": after,
        }
        if message_id:
            body["message_id"] = message_id
        if chat_id:
            body["chat_id"] = chat_id
        if thread_id:
            body["thread_id"] = thread_id
        return self._request("POST", "/im/messages/context", json=body)

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        bot_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        q: dict[str, Any] = {"limit": int(limit)}
        if status:
            q["status"] = status
        if bot_id:
            q["bot_id"] = bot_id
        if chat_id:
            q["chat_id"] = chat_id
        if thread_id:
            q["thread_id"] = thread_id
        if project_id:
            q["project_id"] = project_id
        if kind:
            q["kind"] = kind
        data = self._request("GET", f"/tasks?{urlencode(q)}")
        return list(data.get("tasks") or [])

    def get_task(self, task_id: int, *, events_limit: int = 20) -> dict:
        data = self._request(
            "GET", f"/tasks/{int(task_id)}?events_limit={int(events_limit)}"
        )
        return dict(data.get("task") or {})

    def get_chat_project(self, chat_id: str) -> Optional[dict]:
        try:
            data = self._request("GET", f"/chat-projects/{chat_id}")
        except GatewayError as exc:
            if exc.status_code == 404:
                return None
            raise
        return dict(data.get("item") or {})

    def create_task(
        self,
        *,
        title: str,
        kind: str,
        one_liner: str = "",
        bot_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        created_from_inbound_id: Optional[int] = None,
        actor: str = "dispatcher",
        message: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "title": title,
            "kind": kind,
            "one_liner": one_liner,
            "actor": actor,
        }
        if bot_id:
            body["bot_id"] = bot_id
        if chat_id:
            body["chat_id"] = chat_id
        if thread_id:
            body["thread_id"] = thread_id
        if project_id:
            body["project_id"] = project_id
        if status:
            body["status"] = status
        if created_from_inbound_id is not None:
            body["created_from_inbound_id"] = created_from_inbound_id
        if message is not None:
            body["message"] = message
        data = self._request("POST", "/tasks", json=body)
        return dict(data.get("task") or {})

    def add_followup(
        self,
        task_id: int,
        *,
        message: str,
        event_type: str = "note",
        status: Optional[str] = None,
        actor: str = "dispatcher",
        inbound_id: Optional[int] = None,
        payload: Any = None,
    ) -> dict:
        body: dict[str, Any] = {
            "message": message,
            "event_type": event_type,
            "actor": actor,
        }
        if status is not None:
            body["status"] = status
        if inbound_id is not None:
            body["inbound_id"] = inbound_id
        if payload is not None:
            body["payload"] = payload
        return self._request(
            "POST", f"/tasks/{int(task_id)}/followups", json=body
        )

    def upsert_clue(
        self,
        task_id: int,
        *,
        kind: str,
        ref_key: str,
        one_liner: Optional[str] = None,
        relevance: Optional[str] = None,
        demote: bool = False,
        actor: str = "dispatcher",
        extra: Any = None,
    ) -> dict:
        body: dict[str, Any] = {
            "kind": kind,
            "ref_key": ref_key,
            "actor": actor,
            "demote": demote,
        }
        if one_liner is not None:
            body["one_liner"] = one_liner
        if relevance is not None:
            body["relevance"] = relevance
        if extra is not None:
            body["extra"] = extra
        return self._request(
            "POST", f"/tasks/{int(task_id)}/clues", json=body
        )

    def list_clues(
        self,
        task_id: int,
        *,
        min_relevance: Optional[str] = None,
    ) -> list:
        q = ""
        if min_relevance:
            q = f"?{urlencode({'min_relevance': min_relevance})}"
        data = self._request("GET", f"/tasks/{int(task_id)}/clues{q}")
        return list(data.get("clues") or [])

    def find_clues(
        self,
        *,
        kind: str,
        ref_key: str,
        min_relevance: Optional[str] = None,
    ) -> list:
        q: dict[str, str] = {"kind": kind, "ref_key": ref_key}
        if min_relevance:
            q["min_relevance"] = min_relevance
        data = self._request("GET", f"/clues?{urlencode(q)}")
        return list(data.get("clues") or [])

    def get_dispatch_run(self, inbound_id: int) -> Optional[dict]:
        try:
            data = self._request("GET", f"/dispatcher/runs/{int(inbound_id)}")
        except GatewayError as exc:
            if exc.status_code == 404:
                return None
            raise
        return dict(data.get("run") or {})

    def record_dispatch_run(
        self,
        *,
        inbound_id: int,
        decision: str,
        reason: str,
        task_id: Optional[int] = None,
        evidence: Any = None,
        actor: str = "dispatcher",
    ) -> dict:
        body: dict[str, Any] = {
            "inbound_id": inbound_id,
            "decision": decision,
            "reason": reason,
            "actor": actor,
        }
        if task_id is not None:
            body["task_id"] = task_id
        if evidence is not None:
            body["evidence"] = evidence
        data = self._request("POST", "/dispatcher/runs", json=body)
        return dict(data.get("run") or {})
