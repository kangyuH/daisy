from __future__ import annotations

import os
import threading
import time
from typing import Optional

from app.core.settings import queue_heartbeat_seconds
from app.services.queue.service import QUEUE_AGENT, QUEUE_INBOUND
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
        idle_sleep: float = 3.0,
        heartbeat_seconds: Optional[float] = None,
    ) -> None:
        self.client = client
        self.worker = worker
        self.queue = queue
        self.worker_id = worker_id
        self.idle_sleep = idle_sleep
        self.heartbeat_seconds = (
            float(heartbeat_seconds)
            if heartbeat_seconds is not None
            else queue_heartbeat_seconds()
        )
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
        claim_token = str(item.get("claim_token") or "").strip()
        lost = threading.Event()
        hb_stop = threading.Event()

        def _heartbeat_once() -> None:
            if not claim_token:
                return
            try:
                self.client.heartbeat(
                    self.queue, item_id, claim_token=claim_token
                )
            except GatewayError as exc:
                if exc.status_code == 409:
                    lost.set()
                    print(
                        f"[daemon] heartbeat lost ownership item={item_id}",
                        flush=True,
                    )
                    return
                print(f"[daemon] heartbeat failed item={item_id}: {exc}", flush=True)
            except Exception as exc:
                print(f"[daemon] heartbeat error item={item_id}: {exc}", flush=True)

        def _heartbeat_loop() -> None:
            while not hb_stop.wait(self.heartbeat_seconds):
                _heartbeat_once()
                if lost.is_set():
                    return

        # Fence the claim before any potentially slow processing starts.
        _heartbeat_once()
        if lost.is_set():
            return None

        hb_thread = threading.Thread(
            target=_heartbeat_loop, name=f"hb-{item_id}", daemon=True
        )
        hb_thread.start()
        try:
            result = self.worker.handle(item)
        finally:
            hb_stop.set()
            hb_thread.join(timeout=2.0)

        if lost.is_set():
            print(
                f"[daemon] skip finish item={item_id} (lease lost during handle)",
                flush=True,
            )
            return result

        self._finish(item_id, claim_token, result)
        return result

    def _finish(
        self, item_id: int, claim_token: str, result: WorkerResult
    ) -> None:
        if not claim_token:
            print(
                f"[daemon] finish item={item_id} missing claim_token; skip",
                flush=True,
            )
            return
        try:
            if result.status == "retry":
                self.client.nack(
                    self.queue,
                    item_id,
                    claim_token=claim_token,
                    requeue=True,
                    error=result.error,
                    busy=bool(getattr(result, "busy", False)),
                )
                print(
                    f"[daemon] inbound_id={item_id} nack retry: {result.error}",
                    flush=True,
                )
                return
            err = result.error if result.status == "fail" else None
            self.client.ack(
                self.queue, item_id, claim_token=claim_token, error=err
            )
            print(
                f"[daemon] inbound_id={item_id} ack status={result.status}"
                + (f" err={result.error}" if result.error else ""),
                flush=True,
            )
        except GatewayError as exc:
            if exc.status_code == 409:
                print(
                    f"[daemon] finish item={item_id} ownership lost (409)",
                    flush=True,
                )
                return
            print(f"[daemon] finish inbound_id={item_id} failed: {exc}", flush=True)
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
    idle = float(os.environ.get("WORKER_IDLE_SLEEP", "3") or "3")
    impl = (
        os.environ.get("WORKER_IMPL", "dispatcher").strip().lower() or "dispatcher"
    )
    client = GatewayClient(base, token=token)
    queue = QUEUE_INBOUND
    if impl == "simple":
        worker: Worker = SimpleWorker(client)
    elif impl == "dispatcher":
        _require_dispatcher_llm_key()
        worker = DispatcherWorker(client)
    elif impl == "agent":
        from workers.agent import AgentWorker

        worker = AgentWorker(client)
        queue = QUEUE_AGENT
        if worker_id == "daemon-1":
            worker_id = "agent-1"
    else:
        raise ValueError(
            f"unknown WORKER_IMPL={impl!r}; supported: dispatcher, simple, agent"
        )
    return Daemon(
        client, worker, queue=queue, worker_id=worker_id, idle_sleep=idle
    )
