from app.services.bots.models import BotConfig
from app.services.im.mentions import (
    extract_mention_open_ids,
    normalize_inbound_event,
    should_enqueue,
)
from app.services.im.outbound import compose_text


def test_extract_flat_mentions():
    ev = {
        "type": "im.message.receive_v1",
        "mentions": [{"key": "@_user_1", "id": "ou_bot", "name": "Gemi"}],
        "message_id": "om_x",
    }
    assert extract_mention_open_ids(ev) == ["ou_bot"]


def test_should_enqueue():
    assert should_enqueue(
        ["ou_bot"], self_open_id="ou_self", bot_open_id="ou_bot"
    )
    assert should_enqueue(
        ["ou_self"], self_open_id="ou_self", bot_open_id="ou_bot"
    )
    assert not should_enqueue(
        ["ou_other"], self_open_id="ou_self", bot_open_id="ou_bot"
    )


def test_compose_text_mentions():
    text = compose_text("hello", ["ou_a", "ou_b"])
    assert text.startswith('<at user_id="ou_a"></at>')
    assert "hello" in text
    assert compose_text("hi", None) == "hi"


def test_normalize_inbound_open_ids():
    bot = BotConfig(
        id="gemi",
        name="Gemi",
        app_id="cli_x",
        app_secret="s",
        open_id="ou_bot",
        self_open_id="ou_self",
    )
    raw = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "thread_id": "omt_1",
        "sender_id": "ou_sender",
        "sender_type": "user",
        "content": "hi",
    }
    payload = normalize_inbound_event(bot, raw)
    assert payload["bot_open_id"] == "ou_bot"
    assert payload["self_open_id"] == "ou_self"
    assert payload["sender_open_id"] == "ou_sender"
    assert payload["message_id"] == "om_1"
    assert payload["thread_id"] == "omt_1"
