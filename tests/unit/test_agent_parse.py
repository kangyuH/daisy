from __future__ import annotations

from app.services.agent.parse import extract_plain_text, parse_ledger_command


def test_parse_research_with_rest():
    got = parse_ledger_command("/research 再看一眼仓库")
    assert got == {"cmd": "research", "rest": "再看一眼仓库"}


def test_parse_plan_exec_stop_status():
    assert parse_ledger_command("/plan")["cmd"] == "plan"
    assert parse_ledger_command("/exec 只跑校验")["rest"] == "只跑校验"
    assert parse_ledger_command("/stop")["cmd"] == "stop"
    assert parse_ledger_command("/status")["cmd"] == "status"


def test_parse_rejects_unknown():
    assert parse_ledger_command("/foo") is None
    assert parse_ledger_command("research without slash") is None
    assert parse_ledger_command("") is None


def test_parse_strips_at_mention():
    text = '<at user_id="ou_x">Daisy</at> /exec go'
    got = parse_ledger_command(text)
    assert got == {"cmd": "exec", "rest": "go"}


def test_extract_plain_text_json():
    assert extract_plain_text('{"text":"/plan now"}') == "/plan now"
    assert extract_plain_text({"text": "/stop"}) == "/stop"
