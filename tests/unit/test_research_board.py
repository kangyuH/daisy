from __future__ import annotations

from pathlib import Path

from app.services.agent.research_board import (
    extract_research_layers,
    format_clues_followup,
    format_research_summary_followup,
)


SAMPLE = """# Research：示例

## 1. 诉求澄清

对方要确认口径是否冻结。

## 2. 事实层

代码里默认 noted，无自动调研。

## 3. 方案建议

- 选项 A：自动 research
- 选项 B：人工
"""


def test_extract_three_layers():
    layers = extract_research_layers(SAMPLE)
    assert "口径" in layers["诉求澄清"]
    assert "noted" in layers["事实层"]
    assert "选项 A" in layers["方案建议"]


def test_extract_fallback_without_headings():
    text = "第一段诉求相关说明。\n\n第二段事实材料。\n\n第三段建议动作。"
    layers = extract_research_layers(text)
    assert layers["诉求澄清"]
    assert layers["事实层"]
    assert layers["方案建议"]


def test_format_clues_unbound():
    msg = format_clues_followup(project_id=None, add_dirs=[], brief="无绑定")
    assert "未绑定" in msg or "无可用" in msg
    assert "已加载项目/仓库线索" in msg


def test_format_research_summary(tmp_path: Path):
    p = tmp_path / "research.md"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = format_research_summary_followup(research_path=p, returncode=0)
    assert "诉求澄清" in msg
    assert "事实层" in msg
    assert "方案建议" in msg
    assert "口径" in msg


def test_format_research_missing(tmp_path: Path):
    p = tmp_path / "research.md"
    msg = format_research_summary_followup(
        research_path=p, returncode=1, missing=True
    )
    assert "未产出" in msg


def test_run_phase_research_posts_summary(db_file, monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_TASK_WORKSPACE", str(tmp_path / "ws"))
    from app.services.tasks.store import TaskStore
    from app.services.agent import runtime as rt

    store = TaskStore(str(db_file))
    task = store.create_task(
        title="台账摘要",
        kind="readonly",
        one_liner="测 Gateway 硬同步",
        project_id="lark-superbot",
    )
    tid = int(task["id"])

    class FakeProc:
        def wait(self, timeout=None):
            return 0

    def fake_spawn(argv, *, cwd=None, log_path=None):
        d = Path(cwd)
        (d / "research.md").write_text(SAMPLE, encoding="utf-8")
        if log_path:
            Path(log_path).write_text(
                '{"type":"result","session_id":"sess-test-1"}\n',
                encoding="utf-8",
            )
        return FakeProc(), 4242

    monkeypatch.setattr(rt, "spawn_agent", fake_spawn)
    monkeypatch.setattr(rt, "wait_agent", lambda proc, timeout=None: 0)
    monkeypatch.setattr(rt, "parse_session_id_from_log", lambda p: "sess-test-1")
    monkeypatch.setattr(
        rt,
        "build_agent_argv",
        lambda **kw: ["agent", "-p", "x"],
    )
    monkeypatch.setattr(
        "app.services.agent.runtime.existing_repo_paths",
        lambda project: [{"path": "/tmp/repo", "remote": "", "evidence": ""}],
    )
    monkeypatch.setattr(
        "app.services.agent.runtime.load_project_yaml",
        lambda pid: {"id": pid, "repos": [{"path": "/tmp/repo"}]},
    )
    monkeypatch.setattr(
        "app.services.agent.runtime.copy_project_yaml_to_workspace",
        lambda d, pid: None,
    )

    out = rt.run_phase(store, tid, mode="auto_research", source="test")
    assert out.get("ok") is True
    assert out.get("research_summary_event")
    summary = out["research_summary_event"]["event"]["message"]
    assert "调研成果摘要" in summary
    assert "诉求澄清" in summary
    assert "口径" in summary

    fresh = store.get_task(tid, events_limit=20)
    msgs = [e["message"] for e in fresh["events"]]
    assert any("已加载项目/仓库线索" in m for m in msgs)
    assert any("调研成果摘要" in m for m in msgs)
    assert fresh["status"] == "waiting_human"
