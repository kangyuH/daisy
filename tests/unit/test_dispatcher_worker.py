from __future__ import annotations

from unittest.mock import MagicMock, patch

from workers.dispatcher.worker import DispatcherWorker


def _item(payload: dict, item_id: int = 1) -> dict:
    return {"id": item_id, "queue": "inbound", "payload": payload}


def test_dispatcher_skips_without_mention():
    client = MagicMock()
    worker = DispatcherWorker(client, llm=MagicMock(), process_sleep=0)
    with patch("workers.dispatcher.worker.time.sleep") as mock_sleep:
        result = worker.handle(
            _item(
                {
                    "bot_open_id": "ou_bot",
                    "matched_mentions": ["ou_other"],
                }
            )
        )
    assert result.status == "skip"
    mock_sleep.assert_called_once_with(0)
    client.get_dispatch_run.assert_not_called()


def test_dispatcher_skips_when_already_dispatched():
    client = MagicMock()
    client.get_dispatch_run.return_value = {"inbound_id": 1, "decision": "noop"}
    worker = DispatcherWorker(client, llm=MagicMock(), process_sleep=0)
    with patch("workers.dispatcher.worker.time.sleep"):
        result = worker.handle(
            _item(
                {
                    "bot_open_id": "ou_bot",
                    "matched_mentions": ["ou_bot"],
                    "message_id": "om_1",
                }
            )
        )
    assert result.status == "skip"
    assert result.error == "already dispatched"


def test_dispatcher_forces_finalize_when_agent_skips_it():
    client = MagicMock()
    client.get_dispatch_run.return_value = None
    client.record_dispatch_run.return_value = {"inbound_id": 1, "decision": "noop"}

    worker = DispatcherWorker(client, llm=MagicMock(), process_sleep=0)

    with patch("workers.dispatcher.worker.time.sleep"):
        with patch(
            "workers.dispatcher.worker.build_agent_executor"
        ) as mock_build:
            with patch(
                "workers.dispatcher.worker.run_dispatcher_agent"
            ) as mock_run:
                mock_build.return_value = MagicMock()
                mock_run.return_value = {"output": "done"}
                result = worker.handle(
                    _item(
                        {
                            "bot_open_id": "ou_bot",
                            "matched_mentions": ["ou_bot"],
                            "message_id": "om_1",
                            "chat_id": "oc_a",
                            "content": {"text": "你好"},
                        }
                    )
                )

    assert result.status == "ok"
    client.record_dispatch_run.assert_called_once()
    kwargs = client.record_dispatch_run.call_args.kwargs
    assert kwargs["decision"] == "noop"
    assert kwargs["reason"] == "agent_ended_without_finalize"


def test_dispatcher_accepts_at_self():
    client = MagicMock()
    client.get_dispatch_run.return_value = {"inbound_id": 1}
    worker = DispatcherWorker(client, llm=MagicMock(), process_sleep=0)
    with patch("workers.dispatcher.worker.time.sleep") as mock_sleep:
        result = worker.handle(
            _item(
                {
                    "bot_open_id": "ou_bot",
                    "self_open_id": "ou_self",
                    "matched_mentions": ["ou_self"],
                }
            )
        )
    assert result.status == "skip"
    assert result.error == "already dispatched"
    mock_sleep.assert_called_once_with(0)
