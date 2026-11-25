"""Review round-trip tests need no application fixtures, credentials or network."""
import copy

import pytest

from phase0_eval.review import import_review, render_html, render_xml


@pytest.fixture
def example():
    bundle = {"version": 1, "root": "project", "tasks": [
        {"id": "sep", "parent": "project", "title": "九月"},
        {"id": "oct", "parent": "project", "title": "十月"},
    ], "cases": [
        {"id": "C1", "text": "十月源表到了，九月排除退款", "context": [{"id": "M1", "text": "先收材料"}], "tags": []},
        {"id": "C2", "text": "午饭吃什么", "context": [], "tags": []},
    ]}
    labels = {"version": "draft-v1", "status": "draft", "cases": {
        "C1": {"options": [[{"quote": "十月源表到了", "target": "oct", "wake": False},
                               {"quote": "九月排除退款", "target": "sep", "wake": True}],
                              [{"quote": "十月源表到了，九月排除退款", "target": "project", "wake": True}]], "reason": "跨月"},
        "C2": {"options": [[]], "reason": "无关"},
    }}
    return bundle, labels


def response(content):
    return {"ok": True, "data": {"document": {"document_id": "doc", "revision_id": 7, "content": content}}}


def test_native_tables_roundtrip_multi_target_alternatives_and_unrelated(example):
    bundle, labels = example
    before = copy.deepcopy(labels)
    revised = import_review(bundle, labels, response(render_xml(bundle, labels)))
    assert revised["cases"] == labels["cases"]
    assert revised["version"] == "draft-v1.reviewed-r7"
    assert revised["provenance"]["review"]["revision_id"] == 7
    assert labels == before


def test_user_edit_is_imported(example):
    bundle, labels = example
    xml = render_xml(bundle, labels).replace("<p>否</p>", "<p>是</p>", 1)
    revised = import_review(bundle, labels, response(xml))
    assert revised["cases"]["C1"]["options"][0][0]["wake"] is True


@pytest.mark.parametrize("old,new,match", [
    ("<p>oct</p></td><td><p>否</p>", "<p>production-42</p></td><td><p>否</p>", "unknown task"),
    ("<p>C1</p>", "<p>C404</p>", "Unknown case"),
    ("<p>十月源表到了</p>", "<p>事后知道的答案</p>", "exact substring"),
    ("<p>否</p>", "<p>也许</p>", "wake must"),
])
def test_invalid_edits_fail_closed(example, old, new, match):
    bundle, labels = example
    xml = render_xml(bundle, labels).replace(old, new, 1)
    with pytest.raises(ValueError, match=match):
        import_review(bundle, labels, response(xml))


def test_missing_case_fails(example):
    bundle, labels = example
    shortened = copy.deepcopy(bundle)
    shortened["cases"] = shortened["cases"][:1]
    with pytest.raises(ValueError, match="Source text edited or missing"):
        import_review(bundle, labels, response(render_xml(shortened, labels)))


def test_html_escapes_untrusted_message_and_has_no_network_resources(example):
    bundle, labels = example
    bundle["cases"][1]["text"] = '</script><img src="https://bad.invalid/">'
    rendered = render_html(bundle, labels)
    assert '\\u003c/script>' in rendered
    assert 'connect-src \'none\'' in rendered
    assert 'src="https://bad.invalid/"' not in rendered
    assert '<script src=' not in rendered


def test_failed_fetch_is_never_accepted(example):
    with pytest.raises(ValueError, match="ok=true"):
        import_review(*example, {"ok": False})


def test_source_or_task_snapshot_edits_are_rejected(example):
    bundle, labels = example
    xml = render_xml(bundle, labels)
    with pytest.raises(ValueError, match="Task snapshot edited"):
        import_review(bundle, labels, response(xml.replace("<p>oct</p>", "<p>changed</p>", 1)))
    with pytest.raises(ValueError, match="Source text edited"):
        import_review(bundle, labels, response(xml.replace("入站原文：</b>十月源表到了", "入站原文：</b>后来才知道", 1)))


def test_legacy_result_never_claims_a_wake_decision(example):
    from phase0_eval.review import _engine_result
    result = {"prediction": {"decision": "create", "relations": [
        {"target": "project", "wake": None}], "reason": "test"},
        "outcome": {"exact_targets": True}}
    text = _engine_result("legacy", result, {})
    assert "不表示唤醒决策" in text
    assert "新建提议在评测中映射项目根" in text
    assert "与草稿目标集合一致" in text
    assert "归属正确" not in text
    assert "，留证" not in text


def test_task_contract_details_are_visible_in_both_formats(example):
    bundle, labels = example
    bundle["tasks"][0].update(scope="保留已知边界", acceptance="独立核验结果", stop="确认或有据不适用")
    for render in (render_xml, render_html):
        text = render(bundle, labels)
        assert "保留已知边界" in text
        assert "独立核验结果" in text
        assert "确认或有据不适用" in text
