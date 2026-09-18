from __future__ import annotations

import pytest

from app.services.dispatcher.ack import TITLE_MAX_LEN, build_ack_text


def test_build_ack_create_with_title():
    assert build_ack_text("create", "查上个月口径") == "收到，已创建任务「查上个月口径」。"


def test_build_ack_followup_with_title():
    assert build_ack_text("followup", "查上个月口径") == "收到，已跟进任务「查上个月口径」。"


def test_build_ack_empty_title_fallback():
    assert build_ack_text("create", "") == "收到，已创建任务。"
    assert build_ack_text("create", None) == "收到，已创建任务。"
    assert build_ack_text("followup", "  ") == "收到，已跟进该任务。"
    assert build_ack_text("followup", None) == "收到，已跟进该任务。"


def test_build_ack_trims_long_title():
    long_title = "甲" * (TITLE_MAX_LEN + 10)
    text = build_ack_text("create", long_title)
    assert "「" in text and "」。" in text
    inner = text.split("「", 1)[1].rsplit("」", 1)[0]
    assert len(inner) == TITLE_MAX_LEN
    assert inner.endswith("…")
    assert str(TITLE_MAX_LEN) not in text  # no numeric leak from trim helper
    assert "#12" not in text
    assert "Task" not in text


def test_build_ack_never_includes_task_id_in_template():
    # title itself must not be treated as an id field in the template
    text = build_ack_text("create", "补数口径确认")
    assert "task_id" not in text
    assert "#" not in text
    assert "编号" not in text


def test_build_ack_rejects_noop():
    with pytest.raises(ValueError):
        build_ack_text("noop", "x")
