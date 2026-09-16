import pytest

from app.services.bots.store import STATUS_READY, BotStore


def test_upsert_and_bind(seeded_store: BotStore):
    st = seeded_store
    bot = st.get_bot("gemi")
    assert bot["open_id"] == "ou_bot"
    assert bot["chats"] == []

    b = st.bind_chat("gemi", "oc_a", force=False)
    assert b["bot_id"] == "gemi"
    assert st.chats_for_bot("gemi") == ["oc_a"]


def test_bind_conflict_and_force(seeded_store: BotStore):
    st = seeded_store
    st.upsert_bot(
        bot_id="other",
        name="Other",
        app_id="cli_other",
        app_secret="s",
        enabled=True,
        status=STATUS_READY,
    )
    st.bind_chat("gemi", "oc_x", force=False)
    with pytest.raises(PermissionError):
        st.bind_chat("other", "oc_x", force=False)
    forced = st.bind_chat("other", "oc_x", force=True)
    assert forced["bot_id"] == "other"
    assert forced["forced"] is True


def test_next_bot_id_numeric(store: BotStore):
    assert store.next_bot_id() == "1"
    store.upsert_bot(
        bot_id="gemi",
        name="Gemi",
        app_id="cli_a",
        app_secret="s",
        status=STATUS_READY,
    )
    assert store.next_bot_id() == "1"
    store.upsert_bot(
        bot_id="1",
        name="One",
        app_id="cli_b",
        app_secret="s",
        status=STATUS_READY,
    )
    assert store.next_bot_id() == "2"
    store.upsert_bot(
        bot_id="3",
        name="Three",
        app_id="cli_c",
        app_secret="s",
        status=STATUS_READY,
    )
    assert store.next_bot_id() == "4"
