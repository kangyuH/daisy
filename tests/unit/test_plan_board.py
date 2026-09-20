from __future__ import annotations

from pathlib import Path

from app.services.agent.plan_board import (
    extract_plan_markdown_from_log,
    persist_plan_markdown,
)


def test_extract_plan_from_create_plan_tool_call(tmp_path: Path):
    log = tmp_path / "agent_plan.log"
    plan_body = "# My Plan\n\n## Steps\n\n1. do thing\n"
    log.write_text(
        "\n".join(
            [
                '{"type":"system","subtype":"init"}',
                (
                    '{"type":"tool_call","subtype":"completed","tool_call":'
                    '{"createPlanToolCall":{"args":{"plan":'
                    + __import__("json").dumps(plan_body)
                    + "}}}}"
                ),
            ]
        ),
        encoding="utf-8",
    )
    got = extract_plan_markdown_from_log(log)
    assert got is not None
    assert "My Plan" in got
    assert "do thing" in got
    dest = persist_plan_markdown(tmp_path, got)
    assert dest.is_file()
    assert "My Plan" in dest.read_text(encoding="utf-8")
