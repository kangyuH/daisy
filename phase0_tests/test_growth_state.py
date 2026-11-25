from copy import deepcopy

import pytest

from phase0_eval.growth import run_stream
from phase0_eval.growth_state import GrowthBundle, GrowthState, Proposal


def make_event(key, day, text, **fields):
    return {"id": key, "time": f"2026-10-{day:02d}T09:00:00+08:00",
            "thread": "T1", "text": text, **fields}


def make_bundle(events):
    return GrowthBundle.model_validate({
        "schema": 1, "project": "demo", "map": "RMS → 交付。以明确业务月份为准。",
        "streams": [{"id": "demo-flow", "kind": "synthetic", "events": events}],
        "provenance": {},
    }).model_dump()


def make_create(alias, text, *, kind="period", parent="root", month="2026-09"):
    return {"op": "create", "ref": alias, "parent": parent, "kind": kind,
            "title": alias, "period": month, "outcome": "确认数据", "done_when": "复核通过",
            "quote": text, "why": "消息提出了需要确认的结果"}


def route(target, text, kind="maintain"):
    return {"quote": text, "target": target, "kind": kind}


def test_state_grows_from_accepted_evidence_and_moves_old_quote_once():
    texts = ["这批重量有问题", "是9月，要单独复核，先等业务确认", "业务已经确认，可以继续"]
    events = [make_event(f"E{k}", k, text) for k, text in enumerate(texts, 1)]
    events.append(make_event("E4", 4, texts[1], replay_of="E2"))
    bundle = make_bundle(events)
    waiting = {"op": "update", "target": "new:weight", "action": "wait",
               "quote": texts[1], "why": "等确认", "wait_for": "业务确认"}
    move = {"op": "move", "target": "new:weight", "message_id": "E1", "quote": texts[1],
            "why": "已明确期次和问题"}
    proposals = [
        {"routes": [route("root", texts[0], "pending")], "changes": [], "reason": "缺期次"},
        {"routes": [route("new:weight", texts[1])], "changes": [
            make_create("new:period", texts[1]),
            make_create("new:weight", texts[1], kind="work", parent="new:period"),
            waiting, move,
        ], "reason": "同一事项已知期次，并须单独等待"},
        {"routes": [route("t002", texts[2])], "changes": [
            {"op": "update", "target": "t002", "action": "resume", "quote": texts[2], "why": "恢复"},
        ], "reason": "等待条件已解除"},
    ]
    seen = []

    def scripted(visible):
        assert texts[2] not in str(visible) or visible["message"]["id"] == "E3"
        seen.append(deepcopy(visible))
        return proposals[len(seen) - 1], {}

    result = run_stream(bundle, bundle["streams"][0], scripted)
    a, b, c, d = result["steps"]
    assert [t["id"] for t in seen[0]["tasks"]] == ["root"]
    assert a["after"]["open_pending"][0]["message_id"] == "E1"
    assert seen[1]["same_thread_before"][0]["text"] == texts[0]
    assert "move" in b["actions"] and b["periods"] == ["2026-09"]
    assert b["applied"]["moves"][0]["source_quote"] == texts[0]
    assert b["after"]["open_pending"] == []
    assert b["after"]["tasks"][-1]["status"] == "waiting"
    assert c["after"]["tasks"][-1]["status"] == "active"
    assert d["actions"] == ["duplicate"] and d["before"] == d["after"]
    assert len(seen) == 3


def test_bad_alias_or_unsupported_quote_rolls_back_whole_message():
    ledger = GrowthState("demo")
    before = ledger.snapshot()
    text = "本期交付可以开始"
    for quote, target in ((text, "invented-id"), ("伪造的引文", "new:month")):
        proposal = Proposal.model_validate({
            "routes": [route(target, quote)], "changes": [make_create("new:month", text)],
            "reason": "尝试建期次",
        })
        with pytest.raises(ValueError):
            ledger.apply("E1", text, proposal)
        assert ledger.snapshot() == before
        assert ledger.processed == set()
    good = Proposal.model_validate({
        "routes": [route("new:month", text)], "changes": [make_create("new:month", text)],
        "reason": "确认期次",
    })
    ledger.apply("E1", text, good)
    after = ledger.snapshot()
    with pytest.raises(ValueError, match="already exists"):
        ledger.apply("E2", text, good)
    assert ledger.snapshot() == after and ledger.processed == {"E1"}


def test_bundle_rejects_future_order_and_mismatched_replay():
    with pytest.raises(ValueError, match="chronological"):
        make_bundle([make_event("E1", 2, "先"), make_event("E2", 1, "后")])
    with pytest.raises(ValueError, match="replay source"):
        make_bundle([make_event("E1", 1, "原句"), make_event("E2", 2, "改写", replay_of="E1")])


def test_one_repair_sees_the_same_state_and_never_applies_invalid_work():
    text = "9月源表到了"
    bundle = make_bundle([make_event("E1", 1, text)])
    seen = []

    def scripted(visible):
        seen.append(visible)
        fix = "validation_feedback" in visible
        return {
            "routes": [route("new:sep" if fix else "nonexistent", text)],
            "changes": [make_create("new:sep", text)], "reason": "9月材料到达",
        }, {"total_tokens": 15}

    result = run_stream(bundle, bundle["streams"][0], scripted, repair_invalid=True)
    step = result["steps"][0]
    assert len(seen) == 2 and seen[0]["tasks"] == seen[1]["tasks"]
    assert [attempt["status"] for attempt in step["attempts"]] == ["rejected", "applied"]
    assert step["usage"]["total_tokens"] == 30
    assert len(step["after"]["tasks"]) == 2 and not step.get("error")
