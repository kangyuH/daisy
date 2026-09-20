from __future__ import annotations

from typing import Any

from app.services.agent.constants import MODE_AUTO_RESEARCH
from app.services.queue.service import QUEUE_AGENT
from workers.client import GatewayClient, GatewayError
from workers.result import WorkerResult


class AgentWorker:
    """Serial auto-research consumer for queue=agent."""

    def __init__(self, client: GatewayClient) -> None:
        self.client = client

    def handle(self, item: dict[str, Any]) -> WorkerResult:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        task_id = payload.get("task_id")
        mode = str(payload.get("mode") or MODE_AUTO_RESEARCH).strip().lower()
        source = str(
            payload.get("source") or payload.get("trigger") or "create"
        ).strip() or "create"

        if task_id is None:
            return WorkerResult.fail("agent item missing task_id")
        if mode != MODE_AUTO_RESEARCH:
            return WorkerResult.fail(f"unsupported agent mode {mode!r}")

        try:
            task = self.client.get_task(int(task_id), events_limit=3)
        except GatewayError as exc:
            code = exc.status_code or 0
            if code >= 500 or code == 0:
                return WorkerResult.retry(str(exc))
            return WorkerResult.fail(str(exc))
        except Exception as exc:
            return WorkerResult.retry(str(exc))

        if not task:
            return WorkerResult.fail(f"task {task_id} not found")

        if task.get("agent_run_id") or task.get("agent_pgid") is not None:
            return WorkerResult.skip(
                f"task {task_id} already running "
                f"run={task.get('agent_run_id')} pgid={task.get('agent_pgid')}; "
                "use /stop then retry"
            )

        try:
            self.client.run_agent_phase(
                task_id=int(task_id),
                mode=MODE_AUTO_RESEARCH,
                rest="",
                source=source,
            )
        except GatewayError as exc:
            code = exc.status_code or 0
            if code >= 500 or code == 0:
                return WorkerResult.retry(str(exc))
            return WorkerResult.fail(str(exc))
        except Exception as exc:
            return WorkerResult.retry(str(exc))

        return WorkerResult.ok()


AGENT_QUEUE = QUEUE_AGENT
