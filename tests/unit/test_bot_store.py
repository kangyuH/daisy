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
