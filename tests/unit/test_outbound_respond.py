from app.services.im.outbound import merge_mention_open_ids, should_reply_in_thread


def test_merge_sender_first_and_dedupe():
    assert merge_mention_open_ids("ou_sender", ["ou_self", "ou_sender", "ou_other"]) == [
        "ou_sender",
        "ou_self",
        "ou_other",
    ]
    assert merge_mention_open_ids(None, ["ou_a", "", "ou_a"]) == ["ou_a"]
    assert merge_mention_open_ids("ou_same", ["ou_same"]) == ["ou_same"]
    assert merge_mention_open_ids(None, None) == []


def test_should_reply_in_thread():
    assert should_reply_in_thread("omt_1") is True
    assert should_reply_in_thread("  ") is False
    assert should_reply_in_thread(None) is False
    assert should_reply_in_thread("") is False
