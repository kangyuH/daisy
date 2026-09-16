from app.services.im.mentions import extract_mention_open_ids, should_enqueue
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
