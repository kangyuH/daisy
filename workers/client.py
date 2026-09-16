from __future__ import annotations

from typing import Any, Optional

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
        timeout: float = 30.0,
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
