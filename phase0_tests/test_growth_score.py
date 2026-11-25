"""Growing-tree scoring tests use only synthetic event and state dictionaries."""
from copy import deepcopy

import pytest

from phase0_eval.growth_score import score, validate_reference


def task(task_id="root", parent=None, period=None, status="active", **values):
    return {"id": task_id, "parent": parent, "title": task_id, "period": period,
            "outcome": "Deliver the agreed result", "done_when": "Accepted or ruled inapplicable",
            "status": status, "created_from": "e1", **values}


def state(tasks=None, pending=None, links=None):
    return {"tasks": tasks if tasks is not None else [task()],
            "open_pending": pending or [], "links": links or []}


def step(event_id, before, after, actions, periods=None, status="applied", **applied):
    return {"event_id": event_id, "status": status, "actions": actions, "periods": periods or [],
            "before": deepcopy(before), "after": deepcopy(after),
            "applied": {"created": [], "updates": [], "routes": [], "moves": [], **applied},
            "elapsed_ms": 2, "usage": {"input_tokens": 3, "output_tokens": 1}}


def fixture(steps, events=None, annotations=None, kind="synthetic"):
    events = events or [{"id": row["event_id"], "text": "Material arrived", "thread": "t", "time": "2026-10-01"} for row in steps]
    bundle = {"schema": 1, "project": "demo", "map": "Monthly delivery", "streams": [{"id": "s", "kind": kind, "events": events}], "provenance": {}}
    reference = {"schema": 1, "status": "draft", "streams": {"s": {"events": annotations or {}}}}
    result = {"schema": 1, "bundle_sha256": "frozen", "run": {"mode": "stub"}, "streams": [{"id": "s", "kind": kind, "steps": steps, "final": deepcopy(steps[-1]["after"])}]}
    return bundle, reference, result


def test_alternative_trees_and_actions_do_not_require_fixed_node_ids():
    # The scorer intentionally does not claim that these trees mean the same
    # business task. Both satisfy the reviewed coarse action constraints.
    for created, actions in [([task("month", "root", "2026-09")], ["create_period"]),
                             ([task("group", "root"), task("delivery", "group", "2026-09")], ["create_child"])]:
        annotations = {"e1": {"options": [{"require": ["create"], "periods": ["2026-09"]}], "note": "Several decompositions are acceptable."}}
        report = score(*fixture([step("e1", state(), state([task(), *created]), actions, ["2026-09"], created=created)], annotations=annotations))
        row = report["streams"][0]["steps"][0]
        assert row["reference"]["matched"] and row["mechanical_pass"]
        assert report["scope"]["semantic_accuracy"] is None
        assert report["needs_human_review"]


def test_alternative_creation_timing_and_unreferenced_branches_stay_explicit():
    pending = {"message_id": "e1", "quote": "Material arrived", "target": "root", "kind": "pending"}
    first = state(pending=[pending], links=[pending])
    child = task("delivery", "root", "2026-09", created_from="e2")
    second = state([task(), child], [pending], [pending])
    annotations = {"e1": {"options": [{"require": ["create"]}, {"require": ["pending"], "forbid": ["create", "attach"]}], "note": "Clarification can precede instantiation."}}
    report = score(*fixture([
        step("e1", state(), first, ["pending"], routes=[pending]),
        step("e2", first, second, ["create_child"], ["2026-09"], created=[child]),
    ], annotations=annotations))
    rows = report["streams"][0]["steps"]
    assert rows[0]["reference"]["option"] == 2
    assert rows[0]["reference"]["matched"]
    assert rows[1]["reference"] is None
    assert report["summary"]["synthetic"]["unreferenced"] == 1


def test_duplicate_delivery_does_not_recreate_or_mutate_state():
    child = task("delivery", "root", "2026-09")
    before, after = state(), state([task(), child])
    events = [{"id": "e1"}, {"id": "e2", "replay_of": "e1"}]
    bundle, reference, result = fixture([
        step("e1", before, after, ["create_period"], ["2026-09"], created=[child]),
        step("e2", after, after, ["duplicate"], status="duplicate"),
    ], events=events)
    report = score(bundle, reference, result)
    assert report["streams"][0]["steps"][1]["checks"]["duplicate"]["passed"]
    bad = task("duplicate-task", "root", "2026-09", created_from="e2")
    result["streams"][0]["steps"][1] = step("e2", after, state([task(), child, bad]), ["create_child"], ["2026-09"], status="duplicate", created=[bad])
    result["streams"][0]["final"] = result["streams"][0]["steps"][1]["after"]
    duplicate = score(bundle, reference, result)["streams"][0]["steps"][1]
    assert not duplicate["checks"]["duplicate"]["passed"]


def test_retry_of_failed_event_can_commit_and_errors_remain_in_report():
    events = [{"id": "e1"}, {"id": "e2", "replay_of": "e1"}]
    failed = step("e1", state(), state(), [], status="error")
    failed["error"] = {"kind": "model", "message": "offline failure"}
    created = task("delivery", "root", "2026-09", created_from="e2")
    retried = step("e2", state(), state([task(), created]), ["create_child"], ["2026-09"], created=[created])
    report = score(*fixture([failed, retried], events=events))
    assert report["summary"]["synthetic"]["errors"] == 1
    assert report["streams"][0]["steps"][0]["error"] == failed["error"]
    assert report["streams"][0]["steps"][1]["checks"]["duplicate"] is None


def test_rejected_operations_are_atomic_and_continuity_is_checked():
    changed = state([task(progress="unexpected write")])
    rows = [step("e1", state(), changed, [], status="rejected"), step("e2", state(), state(), ["ignore"])]
    report = score(*fixture(rows))
    assert not report["streams"][0]["steps"][0]["checks"]["atomic"]["passed"]
    assert not report["streams"][0]["steps"][1]["checks"]["continuity"]["passed"]


def test_migration_preserves_old_quote_and_resolves_pending():
    original = {"message_id": "e1", "quote": "Unassigned source material", "target": "root", "kind": "pending"}
    pending = state(pending=[original], links=[original])
    child = task("delivery", "root", "2026-09", created_from="e2")
    moved_link = {"message_id": "e1", "quote": original["quote"], "target": "delivery", "kind": "evidence"}
    resolved = state([task(), child], links=[moved_link])
    move = {"message_id": "e1", "target": "delivery", "source_quote": original["quote"], "quote": "Now confirmed as September", "why": "New clarification"}
    report = score(*fixture([
        step("e1", state(), pending, ["pending"], routes=[original]),
        step("e2", pending, resolved, ["create_child", "move"], ["2026-09"], created=[child], moves=[move]),
    ]))
    assert report["streams"][0]["steps"][1]["checks"]["moves"]["passed"]
    resolved["links"][-1]["quote"] = "Now confirmed as September"
    broken = score(*fixture([
        step("e1", state(), pending, ["pending"], routes=[original]),
        step("e2", pending, resolved, ["create_child", "move"], ["2026-09"], created=[child], moves=[move]),
    ]))
    assert not broken["streams"][0]["steps"][1]["checks"]["moves"]["passed"]


def test_bad_parent_links_and_creation_accounting_are_visible():
    broken = state([task(), task("child", "missing", "2026-09")], links=[{"message_id": "future", "quote": "x", "target": "unknown", "kind": "evidence"}])
    report = score(*fixture([step("e1", state(), broken, ["create_child"], ["2026-09"])]))
    row = report["streams"][0]["steps"][0]
    assert not row["checks"]["tree"]["passed"]
    assert not row["checks"]["links"]["passed"]
    assert not row["checks"]["created"]["passed"]
    assert report["summary"]["synthetic"]["mechanical_failures"] == 1


def test_period_constraints_and_action_constraints_use_one_complete_option():
    annotations = {"e1": {"options": [
        {"require": ["create"], "periods": ["2026-09"]},
        {"require": ["pending"], "periods": ["2026-10"]},
    ], "note": "Do not combine different acceptable branches."}}
    report = score(*fixture([step("e1", state(), state(), ["pending"], ["2026-09"])], annotations=annotations))
    assert not report["streams"][0]["steps"][0]["reference"]["matched"]
    annotations["e1"]["options"] = [{"require": ["pending"], "forbid": ["create"], "periods": ["2026-09", "2026-10"], "period_match": "any"}]
    for periods, expected in [(["2026-09"], True), (["2026-09", "2026-11"], False), ([], False)]:
        report = score(*fixture([step("e1", state(), state(), ["pending"], periods)], annotations=annotations))
        assert report["streams"][0]["steps"][0]["reference"]["matched"] is expected
    report = score(*fixture([step("e1", state(), state(), ["pending", "create_child"], ["2026-09"])], annotations=annotations))
    assert report["streams"][0]["steps"][0]["reference"]["forbidden_actions"] == ["create"]


def test_creation_record_can_precede_same_transaction_update_and_carry_evidence():
    created = task("delivery", "root", "2026-09", kind="work")
    recorded = {**created, "quote": "Material arrived", "why": "New outcome"}
    committed = {**created, "status": "done", "outcome": "Amended outcome"}
    report = score(*fixture([step("e1", state(), state([task(), committed]), ["create_child", "complete", "contract"], ["2026-09"], created=[recorded], updates=[{"target": "delivery", "action": "complete"}])]))
    assert report["streams"][0]["steps"][0]["checks"]["created"]["passed"]


def test_invalid_reference_and_result_identity_fail_closed():
    bundle, reference, result = fixture([step("e1", state(), state(), ["ignore"])])
    reference["streams"]["s"]["events"]["e1"] = {"options": [{"require": ["create_child"], "forbid": ["create"]}], "note": "Contradictory"}
    with pytest.raises(ValueError, match="both required and forbidden"):
        validate_reference(bundle, reference)
    reference["streams"]["s"]["events"] = {"unknown": {"options": [{"require": []}], "note": "Bad id"}}
    with pytest.raises(ValueError, match="unknown events"):
        score(bundle, reference, result)
    result["streams"][0]["steps"][0]["event_id"] = "unknown"
    with pytest.raises(ValueError, match="event set or ordering"):
        score(bundle, None, result)


def test_real_and_synthetic_counts_are_separate_and_final_mismatch_is_visible():
    bundle, reference, result = fixture([step("e1", state(), state(), ["ignore"])], kind="real")
    result["streams"][0]["final"]["tasks"][0]["status"] = "done"
    report = score(bundle, reference, result)
    assert report["summary"]["real"]["steps"] == 1
    assert report["summary"]["synthetic"]["steps"] == 0
    assert not report["streams"][0]["final_check"]["passed"]
    assert report["streams"][0]["steps"][0]["reference"] is None
