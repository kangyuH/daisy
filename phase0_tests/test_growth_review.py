"""Review snapshots and action/reference boundaries without application imports."""
import copy
import xml.etree.ElementTree as ET

import pytest

from phase0_eval.growth_review import render_html, render_xml, tree_lines


@pytest.fixture
def example():
    root = {"id": "root", "parent": None, "title": "交付项目", "period": None, "status": "active", "outcome": "持续交付", "done_when": "项目结束"}
    child = {"id": "month-sep", "parent": "root", "title": "九月交付", "period": "2026-09", "status": "waiting", "outcome": "交付九月数据", "done_when": "对方确认", "wait_for": "补充源表"}
    bundle = {"schema": 1, "project": "项目", "map": "只有根；月份随消息出现", "streams": [{"id": "real-seq", "kind": "real", "events": [
        {"id": "E1", "time": "2026-09-28", "thread": "T1", "text": "九月源表还没齐", "context": []},
        {"id": "E2", "time": "2026-09-29", "thread": "T1", "text": "九月源表还没齐", "replay_of": "E1"},
    ]}]}
    result = {"schema": 1, "streams": [{"id": "real-seq", "kind": "real", "steps": [
        {"event_id": "E1", "status": "applied", "before": {"tasks": [root], "open_pending": []},
         "after": {"tasks": [root, child], "open_pending": []}, "applied": {"created": [child], "updates": [], "routes": [{"message_id": "E1", "quote": "九月源表还没齐", "target": "month-sep", "kind": "maintain"}], "moves": []}},
        {"event_id": "E2", "status": "duplicate", "before": {"tasks": [root, child]}, "after": {"tasks": [root, child]}, "applied": {}},
    ], "final": {"tasks": [root, child], "open_pending": []}}], "score": {"streams": [{"id": "real-seq", "steps": [{"event_id": "E1", "reference": {"matched": True}}]}]}}
    reference = {"schema": 1, "status": "draft", "streams": {"real-seq": {"events": {"E1": {"note": "应有独立交付承诺，等待源表", "options": [{"require": [{"op": "create", "period": "2026-09"}]}]}}}}}
    return bundle, result, reference


def test_both_formats_show_snapshots_effects_and_semantic_draft_note(example):
    for render in (render_html, render_xml):
        output = render(*example)
        assert "九月源表还没齐" in output
        assert "交给owner维护" in output
        assert "应有独立交付承诺，等待源表" in output
        assert "与参考草稿的动作和期次约束一致" in output
        assert "业务语义已经通过人工验收" in output
        assert "最终树" in output


def test_xml_is_valid_and_never_creates_resource_mentions(example):
    output = render_xml(*example)
    ET.fromstring("<review>" + output + "</review>")
    for forbidden in ("<cite", "<task ", "<chat_card", "<time "):
        assert forbidden not in output


def test_render_does_not_mutate_run_or_frozen_inputs(example):
    saved = copy.deepcopy(example)
    render_html(*example)
    render_xml(*example)
    assert example == saved


def test_unknown_and_unrun_events_are_not_hidden(example):
    bundle, result, reference = example
    result["streams"][0]["steps"] = result["streams"][0]["steps"][:1]
    assert "尚未运行" in render_html(bundle, result, reference)
    result["streams"][0]["steps"].append({"event_id": "OTHER", "status": "error"})
    with pytest.raises(ValueError, match="unknown event"):
        render_xml(bundle, result, reference)


def test_task_tree_keeps_actual_topology_and_rejects_cycles(example):
    tasks = example[1]["streams"][0]["final"]["tasks"]
    lines = tree_lines({"tasks": tasks}, contracts=True)
    assert any("└ month-sep" in line for line in lines)
    assert any("停止条件：对方确认" in line for line in lines)
    tasks[0]["parent"] = "month-sep"
    with pytest.raises(ValueError, match="Cycle or rootless"):
        tree_lines({"tasks": tasks})


def test_message_html_is_escaped_and_page_makes_no_network_requests(example):
    bundle, result, reference = example
    bundle["streams"][0]["events"][0]["text"] = '</script><img src="https://bad.invalid/">'
    output = render_html(bundle, result, reference)
    assert '&lt;/script&gt;&lt;img' in output
    assert 'connect-src \'none\'' in output
    assert '<script src=' not in output
    assert 'src="https://bad.invalid/"' not in output


def test_reference_action_names_are_readable_and_prior_input_is_visible(example):
    bundle, result, reference = example
    reference["streams"]["real-seq"]["events"]["E1"]["options"] = [{"require": ["create", "resume"]}]
    result["streams"][0]["steps"][0]["input"] = {"same_thread_before": [{"id": "P0", "text": "此前已知消息"}]}
    for render in (render_html, render_xml):
        output = render(bundle, result, reference)
        assert "恢复推进" in output
        assert "此前已知消息" in output


def test_unperiodized_work_is_not_mislabeled_as_project_root(example):
    snapshot = example[1]["streams"][0]["final"]
    snapshot["tasks"][1]["period"] = None
    assert "业务期未定" in "\n".join(tree_lines(snapshot))


def test_automatic_feedback_is_visible_without_dumping_raw_proposal(example):
    bundle, result, reference = example
    step = result["streams"][0]["steps"][0]
    step["attempts"] = [{"number": 1, "status": "rejected", "error": {"detail": "缺少停止条件"}, "raw": "DO_NOT_PRINT_RAW"},
                        {"number": 2, "status": "applied"}]
    for render in (render_html, render_xml):
        output = render(bundle, result, reference)
        assert "自动反馈修正" in output
        assert "缺少停止条件" in output
        assert "DO_NOT_PRINT_RAW" not in output


def test_baseline_comparison_does_not_replace_current_snapshots(example):
    bundle, result, reference = example
    result["baseline"] = copy.deepcopy(result)
    for render in (render_html, render_xml):
        output = render(bundle, result, reference)
        assert "首跑与本轮的简短对照" in output
        assert "九月交付" in output
