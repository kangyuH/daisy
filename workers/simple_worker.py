from __future__ import annotations

import time
from typing import Any, Protocol

from workers.client import GatewayClient, GatewayError
from workers.result import WorkerResult

REPLY_TEXT = "出来干活"
PROCESS_SLEEP_SECONDS = 3.0


class Worker(Protocol):
    def handle(self, item: dict[str, Any]) -> WorkerResult: ...


class SimpleWorker:
    """Minimal worker: @bot → /im/respond; @self-only → skip (still sleeps before ack)."""

    def __init__(self, client: GatewayClient) -> None:
        self.client = client

    def handle(self, item: dict[str, Any]) -> WorkerResult:
        inbound_id = item.get("id")
        if inbound_id is None:
            return WorkerResult.fail("missing inbound item id")

        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        bot_open_id = str(payload.get("bot_open_id") or "").strip()
        matched = payload.get("matched_mentions") or []
        if not isinstance(matched, list):
            matched = []
        matched_ids = {str(x).strip() for x in matched if x}

        # Sleep even on skip so enqueue can finish writing typing_reaction_id
        # before daemon ack deletes it (avoids stuck Typing emoji).
        time.sleep(PROCESS_SLEEP_SECONDS)

        if not bot_open_id or bot_open_id not in matched_ids:
            return WorkerResult.skip("not @bot")

        self_open_id = str(payload.get("self_open_id") or "").strip() or None
        mention_open_ids = [self_open_id] if self_open_id else []

        try:
            self.client.respond(
                inbound_id=int(inbound_id),
                text=REPLY_TEXT,
                mention_open_ids=mention_open_ids,
            )
        except GatewayError as exc:
            code = exc.status_code or 0
            if code >= 500 or code == 0:
                return WorkerResult.retry(str(exc))
            return WorkerResult.fail(str(exc))
        except Exception as exc:
            return WorkerResult.retry(str(exc))

        return WorkerResult.ok()
