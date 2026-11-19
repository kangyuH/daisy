from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.im import fetch

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 11, 19, 12, 0, tzinfo=TZ)


def _thread_messages(count: int) -> list[dict]:
    """Newest-first list, as returned by threads-messages-list --order desc."""
    messages: list[dict] = []
    for i in range(count):
        when = BASE - timedelta(minutes=i)
        messages.append(
            {
                "message_id": f"om_{i}",
                "create_time": when.strftime("%Y-%m-%d %H:%M"),
                "chat_id": "oc_x",
                "thread_id": "omt_x",
                "sender": {"name": "u"},
                "content": f"om_{i}",
            }
        )
    return messages


class _FakeThread:
    """Serves pages newest-first and honours page_token, like the real CLI."""

    def __init__(self, messages: list[dict], page_size: int = 50) -> None:
        self._messages = messages
        self._page_size = page_size
        self.calls: list[str | None] = []

    async def __call__(
        self, thread: str, *, page_size: int, order: str, page_token: str | None
    ) -> tuple[list[dict], str | None, bool]:
        assert order == "desc"
        assert page_size <= 50
        self.calls.append(page_token)
        offset = int(page_token or 0)
        block = self._messages[offset : offset + self._page_size]
        next_offset = offset + self._page_size
        has_more = next_offset < len(self._messages)
        return block, (str(next_offset) if has_more else None), has_more


@pytest.mark.asyncio
async def test_walks_back_until_anchor_found(monkeypatch):
    messages = _thread_messages(120)
    anchor = messages[100]  # page 3
    fake = _FakeThread(messages)
    monkeypatch.setattr(fetch, "list_thread_messages_page", fake)

    window, found, meta = await fetch.collect_thread_window(
        "omt_x", message_id=anchor["message_id"], anchor_time=None, before=5, after=2
    )

    assert found is not None
    assert meta["anchor_missing"] is False
    assert meta["truncated"] is False
    assert meta["pages"] == 3
    assert fake.calls == [None, "50", "100"]
    ids = [m["message_id"] for m in window]
    assert ids.index(anchor["message_id"]) == 5
    assert len(window) == 8


@pytest.mark.asyncio
async def test_stops_on_first_page_when_window_fits(monkeypatch):
    messages = _thread_messages(20)
    anchor = messages[10]
    fake = _FakeThread(messages)
    monkeypatch.setattr(fetch, "list_thread_messages_page", fake)

    window, found, meta = await fetch.collect_thread_window(
        "omt_x", message_id=anchor["message_id"], anchor_time=None, before=3, after=3
    )

    assert found is not None
    assert meta["pages"] == 1
    assert len(window) == 7
    assert window[3]["message_id"] == anchor["message_id"]


@pytest.mark.asyncio
async def test_missing_anchor_returns_freshest_not_stale_page(monkeypatch):
    messages = _thread_messages(20)
    fake = _FakeThread(messages)
    monkeypatch.setattr(fetch, "list_thread_messages_page", fake)

    # Anchor timestamp is newer than everything in the thread.
    anchor_time = BASE + timedelta(minutes=5)
    window, found, meta = await fetch.collect_thread_window(
        "omt_x", message_id="om_missing", anchor_time=anchor_time, before=3, after=2
    )

    assert found is None
    assert meta["anchor_missing"] is True
    assert meta["truncated"] is True
    # Returning the freshest slice in chronological order.
    assert [m["message_id"] for m in window] == ["om_4", "om_3", "om_2", "om_1", "om_0"]


@pytest.mark.asyncio
async def test_marks_truncated_when_before_not_satisfied(monkeypatch):
    messages = _thread_messages(4)
    anchor = messages[-1]  # oldest message, nothing before it
    fake = _FakeThread(messages)
    monkeypatch.setattr(fetch, "list_thread_messages_page", fake)

    window, found, meta = await fetch.collect_thread_window(
        "omt_x", message_id=anchor["message_id"], anchor_time=None, before=10, after=0
    )

    assert found is not None
    assert meta["reached_head"] is True
    assert meta["truncated"] is True
    assert window[0]["message_id"] == anchor["message_id"]


@pytest.mark.asyncio
async def test_fetch_context_thread_sees_current_message(monkeypatch):
    messages = _thread_messages(120)
    anchor = messages[115]
    fake = _FakeThread(messages)
    monkeypatch.setattr(fetch, "list_thread_messages_page", fake)

    async def _mget(ids: list[str]) -> list[dict]:
        return [anchor]

    monkeypatch.setattr(fetch, "mget_messages", _mget)

    ctx = await fetch.fetch_context(message_id=anchor["message_id"], before=4, after=3)

    assert ctx["meta"]["mode"] == "thread"
    assert ctx["meta"]["anchor_missing"] is False
    assert ctx["anchor"]["message_id"] == anchor["message_id"]
    ids = [m["message_id"] for m in ctx["messages"]]
    assert anchor["message_id"] in ids
    # Chronological window: ends at anchor + after, never at a stale first page.
    assert ids[-1] == messages[115 - 3]["message_id"]
    assert ids[0] == messages[119]["message_id"]


@pytest.mark.asyncio
async def test_fetch_context_tolerates_unreadable_anchor(monkeypatch):
    messages = _thread_messages(10)
    fake = _FakeThread(messages)
    monkeypatch.setattr(fetch, "list_thread_messages_page", fake)

    async def _boom(ids: list[str]) -> list[dict]:
        raise fetch.LarkCliError("gone", returncode=1, stdout="", stderr="")

    monkeypatch.setattr(fetch, "mget_messages", _boom)

    ctx = await fetch.fetch_context(
        message_id="om_gone", thread_id="omt_x", before=2, after=2
    )

    assert ctx["meta"]["anchor_missing"] is True
    assert ctx["messages"]


def _same_minute_message(index: int, position: int) -> dict:
    """Single-minute thread page: create_time ties, position breaks the tie."""
    return {
        "message_id": f"om_{index}",
        "create_time": "2026-11-19 12:00",
        "thread_message_position": str(position),
        "chat_id": "oc_x",
        "thread_id": "omt_x",
        "sender": {"name": "u"},
        "content": f"om_{index}",
    }


@pytest.mark.asyncio
async def test_same_minute_messages_ordered_by_thread_position(monkeypatch):
    newest_first = [
        _same_minute_message(5, 15),
        _same_minute_message(4, 14),
        _same_minute_message(3, 13),
        _same_minute_message(2, 12),
        _same_minute_message(1, 11),
    ]

    async def _page(thread: str, *, page_size: int, order: str, page_token: str | None):
        assert order == "desc"
        return newest_first, None, False

    monkeypatch.setattr(fetch, "list_thread_messages_page", _page)

    window, found, meta = await fetch.collect_thread_window(
        "omt_x", message_id="om_3", anchor_time=None, before=2, after=1
    )

    assert found is not None
    assert meta["truncated"] is False
    assert [m["message_id"] for m in window] == ["om_1", "om_2", "om_3", "om_4"]


def test_chronological_ignores_positions_in_mixed_lists():
    positioned_old = {
        "message_id": "om_a",
        "create_time": "2026-11-19 12:00",
        "thread_message_position": "9",
    }
    root_message = {"message_id": "om_b", "create_time": "2026-11-19 12:01"}
    positioned_new = {
        "message_id": "om_c",
        "create_time": "2026-11-19 12:02",
        "thread_message_position": "1",
    }

    ordered = fetch._chronological([positioned_new, root_message, positioned_old])

    assert [m["message_id"] for m in ordered] == ["om_a", "om_b", "om_c"]
