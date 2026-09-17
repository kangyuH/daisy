from __future__ import annotations

import os
import time
from typing import Optional

from app.services.queue.service import QUEUE_INBOUND
from workers.client import GatewayClient, GatewayError
from workers.dispatcher import DispatcherWorker
from workers.result import WorkerResult
from workers.simple_worker import SimpleWorker, Worker


class Daemon:
    """Queue poller only: claim → worker.handle → ack/nack. No @bot/@self logic."""

    def __init__(
        self,
        client: GatewayClient,
        worker: Worker,
        *,
        queue: str = QUEUE_INBOUND,
        worker_id: str = "daemon-1",
        idle_sleep: float = 1.0,
    ) -> None:
        self.client = client
        self.worker = worker
        self.queue = queue
        self.worker_id = worker_id
        self.idle_sleep = idle_sleep
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        while not self._stop:
            self.run_once()

    def run_once(self) -> Optional[WorkerResult]:
        try:
            items = self.client.claim(
                self.queue, limit=1, claimed_by=self.worker_id
            )
        except GatewayError as exc:
            print(f"[daemon] claim failed: {exc}", flush=True)
            time.sleep(self.idle_sleep)
            return None
        except Exception as exc:
            print(f"[daemon] claim error: {exc}", flush=True)
            time.sleep(self.idle_sleep)
            return None

        if not items:
            time.sleep(self.idle_sleep)
            return None

        item = items[0]
        item_id = int(item["id"])
        result = self.worker.handle(item)
        self._finish(item_id, result)
        return result

    def _finish(self, item_id: int, result: WorkerResult) -> None:
        try:
            if result.status == "retry":
                self.client.nack(
                    self.queue,
                    item_id,
                    requeue=True,
                    error=result.error,
                )
                print(
                    f"[daemon] inbound_id={item_id} nack retry: {result.error}",
                    flush=True,
                )
                return
            err = result.error if result.status == "fail" else None
            self.client.ack(self.queue, item_id, error=err)
            print(
                f"[daemon] inbound_id={item_id} ack status={result.status}"
                + (f" err={result.error}" if result.error else ""),
                flush=True,
            )
        except Exception as exc:
            print(f"[daemon] finish inbound_id={item_id} failed: {exc}", flush=True)


def _require_dispatcher_llm_key() -> None:
    """Fail fast when dispatcher has no DeepSeek / generic LLM key."""
    key = (
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        or os.environ.get("DISPATCHER_LLM_API_KEY", "").strip()
    )
    if not key:
        raise ValueError(
            "DEEPSEEK_API_KEY is required when WORKER_IMPL=dispatcher "
            "(or set DISPATCHER_LLM_API_KEY)"
        )


def build_daemon_from_env() -> Daemon:
    base = os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8000").strip()
    token = os.environ.get("GATEWAY_TOKEN", "").strip()
    worker_id = os.environ.get("WORKER_ID", "daemon-1").strip() or "daemon-1"
    idle = float(os.environ.get("WORKER_IDLE_SLEEP", "1") or "1")
    impl = (
        os.environ.get("WORKER_IMPL", "dispatcher").strip().lower() or "dispatcher"
    )
    client = GatewayClient(base, token=token)
    if impl == "simple":
        worker: Worker = SimpleWorker(client)
    elif impl == "dispatcher":
        _require_dispatcher_llm_key()
        worker = DispatcherWorker(client)
    else:
        raise ValueError(
            f"unknown WORKER_IMPL={impl!r}; supported: dispatcher, simple"
        )
    return Daemon(client, worker, worker_id=worker_id, idle_sleep=idle)
