from unittest.mock import MagicMock, patch

from workers.client import GatewayError
from workers.simple_worker import PROCESS_SLEEP_SECONDS, REPLY_TEXT, SimpleWorker


def _item(payload: dict, item_id: int = 1) -> dict:
    return {"id": item_id, "queue": "inbound", "payload": payload}


def test_simple_worker_at_bot_responds():
    client = MagicMock()
    worker = SimpleWorker(client)
    with patch("workers.simple_worker.time.sleep") as mock_sleep:
        result = worker.handle(
            _item(
                {
                    "bot_id": "gemi",
                    "message_id": "om_1",
                    "bot_open_id": "ou_bot",
                    "self_open_id": "ou_self",
                    "sender_open_id": "ou_sender",
                    "thread_id": "omt_1",
                    "matched_mentions": ["ou_bot"],
                }
            )
        )
    assert result.status == "ok"
    mock_sleep.assert_called_once_with(PROCESS_SLEEP_SECONDS)
    client.respond.assert_called_once_with(
        inbound_id=1,
        text=REPLY_TEXT,
        mention_open_ids=["ou_self"],
    )


def test_simple_worker_self_only_skips_with_sleep():
    client = MagicMock()
    worker = SimpleWorker(client)
    with patch("workers.simple_worker.time.sleep") as mock_sleep:
        result = worker.handle(
            _item(
                {
                    "bot_id": "gemi",
                    "message_id": "om_1",
                    "bot_open_id": "ou_bot",
                    "self_open_id": "ou_self",
                    "matched_mentions": ["ou_self"],
                }
            )
        )
    assert result.status == "skip"
    mock_sleep.assert_called_once_with(PROCESS_SLEEP_SECONDS)
    client.respond.assert_not_called()


def test_simple_worker_missing_id_fail():
    client = MagicMock()
    worker = SimpleWorker(client)
    result = worker.handle({"payload": {"bot_open_id": "ou_bot", "matched_mentions": ["ou_bot"]}})
    assert result.status == "fail"
    client.respond.assert_not_called()


def test_simple_worker_502_retry():
    client = MagicMock()
    client.respond.side_effect = GatewayError("boom", status_code=502)
    worker = SimpleWorker(client)
    with patch("workers.simple_worker.time.sleep"):
        result = worker.handle(
            _item(
                {
                    "bot_open_id": "ou_bot",
                    "matched_mentions": ["ou_bot"],
                }
            )
        )
    assert result.status == "retry"


def test_daemon_maps_result_to_ack_nack():
    from workers.daemon import Daemon
    from workers.result import WorkerResult

    client = MagicMock()
    client.claim.return_value = [
        {
            "id": 9,
            "payload": {
                "bot_open_id": "ou_bot",
                "matched_mentions": ["ou_bot"],
            },
        }
    ]
    worker = MagicMock()
    worker.handle.return_value = WorkerResult.ok()
    daemon = Daemon(client, worker, idle_sleep=0)
    assert daemon.run_once().status == "ok"
    client.ack.assert_called_once_with("inbound", 9, error=None)

    client.reset_mock()
    worker.handle.return_value = WorkerResult.retry("tmp")
    client.claim.return_value = [{"id": 10, "payload": {}}]
    assert daemon.run_once().status == "retry"
    client.nack.assert_called_once()
    assert client.nack.call_args.kwargs["requeue"] is True
