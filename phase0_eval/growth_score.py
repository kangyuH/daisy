"""Deterministic checks and explicit reference constraints for growing task trees.

Reference actions are review aids, not semantic ground truth. No title matching,
fixed-node alignment, or model judge is used. Alternative trees and decisions
can satisfy the same action constraints; their business meaning needs review.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
from typing import Any

ACTIONS = frozenset({
    "create", "create_period", "create_child", "attach", "evidence", "pending",
    "progress", "wait", "resume", "complete", "reopen", "contract", "move", "ignore", "duplicate",
})
STATUSES = frozenset({"applied", "duplicate", "rejected", "error"})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _index(rows: list[dict], key: str, label: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        value = row[key]
        if not isinstance(value, str) or not value or value in result:
            raise ValueError(f"invalid or duplicate {label}: {value!r}")
        result[value] = row
    return result


def _period(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 7:
        return False
    try:
        return date.fromisoformat(value + "-01").strftime("%Y-%m") == value
    except ValueError:
        return False


def _string_list(value: Any, label: str, allowed: set | frozenset | None = None) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    if allowed is not None and set(value) - allowed:
        raise ValueError(f"{label} contains unknown values: {sorted(set(value) - allowed)}")
    return value


def _expanded(actions: list[str]) -> set[str]:
    expanded = set(actions)
    if expanded & {"create_period", "create_child"}:
        expanded.add("create")
    if "evidence" in expanded:
        expanded.add("attach")
    return expanded


def validate_reference(bundle: dict, reference: dict | None) -> None:
    """Reject contradictory constraints; permit deliberately unreviewed branches."""
    if reference is None:
        return
    if reference.get("schema") != 1 or reference.get("status") not in {"draft", "reviewed", "accepted"}:
        raise ValueError("reference requires schema=1 and draft/reviewed/accepted status")
    streams = _index(bundle["streams"], "id", "bundle stream")
    entries = reference.get("streams")
    if not isinstance(entries, dict) or entries.keys() - streams.keys():
        raise ValueError("reference contains an unknown stream or invalid streams")
    for stream_id, entry in entries.items():
        events = _index(streams[stream_id]["events"], "id", "bundle event")
        annotations = entry.get("events")
        if not isinstance(annotations, dict) or annotations.keys() - events.keys():
            raise ValueError(f"{stream_id}: reference contains unknown events")
        for event_id, annotation in annotations.items():
            prefix = f"{stream_id}/{event_id}"
            if not isinstance(annotation.get("note"), str) or not annotation["note"].strip():
                raise ValueError(f"{prefix}: reference note is required")
            options = annotation.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError(f"{prefix}: at least one reference option is required")
            for option in options:
                if not isinstance(option, dict) or set(option) - {"require", "forbid", "periods", "period_match"}:
                    raise ValueError(f"{prefix}: unknown reference option fields")
                required = _string_list(option.get("require"), prefix + " require", ACTIONS)
                forbidden = _string_list(option.get("forbid", []), prefix + " forbid", ACTIONS)
                if _expanded(required) & set(forbidden):
                    raise ValueError(f"{prefix}: action is both required and forbidden")
                if "periods" in option:
                    periods = _string_list(option["periods"], prefix + " periods")
                    if any(not _period(value) for value in periods):
                        raise ValueError(f"{prefix}: periods must be YYYY-MM")
                if option.get("period_match", "all") not in {"all", "any"}:
                    raise ValueError(f"{prefix}: period_match must be all or any")
                if "period_match" in option and "periods" not in option:
                    raise ValueError(f"{prefix}: period_match requires periods")
                if option.get("period_match") == "any" and not option["periods"]:
                    raise ValueError(f"{prefix}: any requires a nonempty period set")


def _reference_outcome(annotation: dict | None, actions: list[str], periods: list[str]) -> dict | None:
    if annotation is None:
        return None
    actual = _expanded(actions)
    actual_periods = set(periods)
    options = []
    for number, option in enumerate(annotation["options"], 1):
        required, forbidden = set(option["require"]), set(option.get("forbid", []))
        missing_actions, forbidden_actions = sorted(required - actual), sorted(forbidden & actual)
        expected = set(option["periods"]) if "periods" in option else None
        period_match = None
        missing_periods, extra_periods = [], []
        if expected is not None:
            missing_periods, extra_periods = sorted(expected - actual_periods), sorted(actual_periods - expected)
            if option.get("period_match", "all") == "any":
                period_match = bool(expected & actual_periods) and not extra_periods
            else:
                period_match = actual_periods == expected
        action_match = not missing_actions and not forbidden_actions
        options.append({
            "matched": action_match and period_match is not False,
            "option": number, "action_match": action_match, "period_match": period_match,
            "missing_actions": missing_actions, "forbidden_actions": forbidden_actions,
            "expected_periods": sorted(expected) if expected is not None else None,
            "actual_periods": sorted(actual_periods), "missing_periods": missing_periods,
            "extra_periods": extra_periods, "required_actions": sorted(required),
            "forbidden_constraints": sorted(forbidden), "actual_actions": list(actions),
            "expanded_actions": sorted(actual),
        })
    # Select one complete option, never combine parts of different options.
    best = min(options, key=lambda row: (
        not row["matched"], len(row["missing_actions"]) + len(row["forbidden_actions"]),
        row["period_match"] is False, len(row["missing_periods"]) + len(row["extra_periods"]), row["option"],
    ))
    return best | {"note": annotation["note"]}


def _check(violations: list[str]) -> dict:
    return {"passed": not violations, "violations": violations}


def _tree(state: dict) -> dict:
    issues, tasks = [], {}
    for task in state.get("tasks", []):
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            issues.append("task has invalid id")
        elif task_id in tasks:
            issues.append(f"duplicate task id: {task_id}")
        else:
            tasks[task_id] = task
    roots = [task_id for task_id, task in tasks.items() if task.get("parent") is None]
    if len(roots) != 1:
        issues.append(f"expected one root, found {len(roots)}")
    root = roots[0] if len(roots) == 1 else None
    for task_id, task in tasks.items():
        if task.get("status") not in {"active", "waiting", "done"}:
            issues.append(f"invalid task status: {task_id}")
        if task.get("period") is not None and not _period(task["period"]):
            issues.append(f"invalid task period: {task_id}")
        seen, node = set(), task_id
        while node is not None:
            if node in seen:
                issues.append(f"cyclic parent chain: {task_id}")
                break
            seen.add(node)
            if node not in tasks:
                issues.append(f"missing parent in chain: {task_id} -> {node}")
                break
            node = tasks[node].get("parent")
        if root and root not in seen:
            issues.append(f"task does not reach root: {task_id}")
    return _check(issues)


def _links(state: dict, seen_messages: set[str]) -> dict:
    issues = []
    tasks = {task.get("id") for task in state.get("tasks", [])}
    seen = set()
    for link in state.get("links", []):
        if link.get("target") not in tasks:
            issues.append(f"link has unknown target: {link.get('target')}")
        if link.get("message_id") not in seen_messages:
            issues.append(f"link references unseen message: {link.get('message_id')}")
        if not isinstance(link.get("quote"), str) or not link["quote"].strip():
            issues.append("link has empty quote")
        key = (link.get("message_id"), link.get("quote"), link.get("target"), link.get("kind"))
        if key in seen:
            issues.append("duplicate message/quote/target/kind link")
        seen.add(key)
    pending_seen = set()
    for pending in state.get("open_pending", []):
        key = (pending.get("message_id"), pending.get("quote"), pending.get("target"), "pending")
        if key in pending_seen:
            issues.append("duplicate open pending item")
        pending_seen.add(key)
        if pending.get("target") not in tasks:
            issues.append("pending has unknown target")
        if pending.get("message_id") not in seen_messages:
            issues.append("pending references unseen message")
        if not isinstance(pending.get("quote"), str) or not pending["quote"].strip():
            issues.append("pending has empty quote")
        if key not in seen:
            issues.append("open pending item has no matching link")
    return _check(issues)


def _state_value(state: dict) -> dict:
    # Row order is not a business invariant; fields within each row are.
    return {key: sorted(_canonical(row) for row in state.get(key, [])) for key in ("tasks", "links", "open_pending")}


def _moves(before: dict, after: dict, moves: list[dict]) -> dict:
    issues = []
    after_tasks = {task.get("id") for task in after.get("tasks", [])}
    for move in moves:
        message_id, target = move.get("message_id"), move.get("target")
        prior = [item for item in before.get("open_pending", []) if item.get("message_id") == message_id
                 and (move.get("source_quote") is None or item.get("quote") == move["source_quote"])]
        resolved = [item for item in prior if not any(
            row.get("message_id") == message_id and row.get("quote") == item.get("quote")
            for row in after.get("open_pending", [])
        )]
        if target not in after_tasks:
            issues.append("move has unknown target")
        if len(prior) != 1:
            issues.append("move must identify one prior pending source")
        elif not resolved:
            issues.append("move did not resolve its prior pending source")
        elif not any(
            link.get("message_id") == message_id and link.get("quote") == item.get("quote") and link.get("target") == target
            for item in resolved for link in after.get("links", [])
        ):
            issues.append("move did not preserve the prior message quote at its new target")
    return _check(issues)


def _score_step(event: dict, step: dict, annotation: dict | None, prior: dict | None, seen_messages: set[str], successful: set[str]) -> dict:
    before, after = step["before"], step["after"]
    actions = _string_list(step.get("actions", []), "result actions")
    periods = _string_list(step.get("periods", []), "result periods")
    if any(not _period(value) for value in periods):
        raise ValueError("result periods must be YYYY-MM")
    applied = step.get("applied") or {}
    before_tasks = {task.get("id"): task for task in before.get("tasks", [])}
    after_tasks = {task.get("id"): task for task in after.get("tasks", [])}
    added, removed = set(after_tasks) - set(before_tasks), set(before_tasks) - set(after_tasks)
    changed = {key for key in before_tasks.keys() & after_tasks.keys() if before_tasks[key] != after_tasks[key]}
    declared_created = [task.get("id") for task in applied.get("created", [])]
    created_issues = []
    if len(declared_created) != len(set(declared_created)) or set(declared_created) != added:
        created_issues.append("applied.created does not equal actual new task IDs")
    if removed:
        created_issues.append("task deletion is outside growing-tree operations")
    for task in applied.get("created", []):
        committed = after_tasks.get(task.get("id"), {})
        if any(committed.get(key) != task[key] for key in ("id", "parent", "kind", "title", "period", "created_from") if key in task):
            created_issues.append("created task identity differs from committed task")
    changed_state = _state_value(before) != _state_value(after)
    effect_count = sum(len(applied.get(key, [])) for key in ("created", "updates", "routes", "moves"))
    expect_duplicate = event.get("replay_of") in successful
    duplicate = None
    if expect_duplicate or step["status"] == "duplicate":
        issues = []
        if expect_duplicate and step["status"] != "duplicate":
            issues.append("replay of committed event was not marked duplicate")
        if changed_state or effect_count or set(actions) - {"duplicate"}:
            issues.append("duplicate delivery changed state or emitted operations")
        duplicate = _check(issues)
    atomic = None
    if step["status"] in {"error", "rejected"}:
        atomic = _check(["unsuccessful event committed state or operations"] if changed_state or effect_count else [])
    checks = {
        "tree": _tree(after), "links": _links(after, seen_messages),
        "continuity": _check(["before state differs from preceding committed state"] if _state_value(before) != _state_value(prior) else []) if prior is not None else None,
        "created": _check(created_issues), "duplicate": duplicate, "atomic": atomic,
        "moves": _moves(before, after, applied.get("moves", [])),
        "actions": _check(["result contains an unknown action"] if set(actions) - ACTIONS else []),
    }
    return {
        "event_id": event["id"], "status": step["status"],
        "reference": _reference_outcome(annotation, actions, periods), "checks": checks,
        "mechanical_pass": all(check is None or check["passed"] for check in checks.values()),
        "metrics": {
            "created": len(added), "updated": len(applied.get("updates", [])),
            "routes": len(applied.get("routes", [])), "moves": len(applied.get("moves", [])),
            "tasks_before": len(before_tasks), "tasks_after": len(after_tasks),
            "pending_before": len(before.get("open_pending", [])), "pending_after": len(after.get("open_pending", [])),
            "task_ids_created": sorted(added), "task_ids_changed": sorted(changed),
            "state_changed": changed_state, "expected_duplicate": expect_duplicate,
        },
        "elapsed_ms": step.get("elapsed_ms", 0), "usage": step.get("usage") or {},
        "error": step.get("error"),
    }


def _summary(steps: list[dict]) -> dict:
    counts = Counter({key: 0 for key in (
        "steps", "applied", "duplicate", "rejected", "errors", "referenced", "reference_matches",
        "reference_differences", "unreferenced", "mechanical_failures", "created", "updated", "routes", "moves",
    )})
    usage, elapsed = Counter(), 0
    for step in steps:
        counts["steps"] += 1
        counts["errors" if step["status"] == "error" else step["status"]] += 1
        counts["mechanical_failures"] += not step["mechanical_pass"]
        reference = step["reference"]
        if reference is None:
            counts["unreferenced"] += 1
        else:
            counts["referenced"] += 1
            counts["reference_matches" if reference["matched"] else "reference_differences"] += 1
        for key in ("created", "updated", "routes", "moves"):
            counts[key] += step["metrics"][key]
        elapsed += step["elapsed_ms"] or 0
        for key, value in step["usage"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] += value
    return dict(counts) | {"elapsed_ms": round(elapsed, 3), "usage": dict(usage)}


def score(bundle: dict, reference: dict | None, result: dict) -> dict:
    """Score committed mechanics and action constraints, without judging semantics."""
    if bundle.get("schema") != 1 or result.get("schema") != 1:
        raise ValueError("growth bundle and result require schema=1")
    validate_reference(bundle, reference)
    streams = _index(bundle["streams"], "id", "bundle stream")
    results = _index(result["streams"], "id", "result stream")
    if streams.keys() != results.keys():
        raise ValueError("result stream IDs differ from bundle")
    scored_streams, needs_review = [], []
    for stream_id, stream in streams.items():
        replay = results[stream_id]
        if replay["kind"] != stream["kind"] or stream["kind"] not in {"real", "synthetic"}:
            raise ValueError(f"{stream_id}: stream kind differs or is invalid")
        events = _index(stream["events"], "id", "bundle event")
        steps = _index(replay["steps"], "event_id", "result event")
        if list(events) != list(steps):
            raise ValueError(f"{stream_id}: result event set or ordering differs")
        annotations = ((reference or {}).get("streams", {}).get(stream_id) or {}).get("events", {})
        prior, successful, visible_messages, scored = None, set(), set(), []
        for event_id, event in events.items():
            step = steps[event_id]
            if step.get("status") not in STATUSES:
                raise ValueError(f"{stream_id}/{event_id}: invalid result status")
            visible_messages.add(event_id)
            row = _score_step(event, step, annotations.get(event_id), prior, visible_messages, successful)
            scored.append(row)
            prior = step["after"]
            if step["status"] in {"applied", "duplicate"}:
                successful.add(event_id)
            if row["reference"] is None or row["reference"]["matched"] is False or row["metrics"]["created"] or row["metrics"]["moves"]:
                needs_review.append({"stream_id": stream_id, "event_id": event_id, "unreferenced": row["reference"] is None, "created": row["metrics"]["task_ids_created"], "moves": row["metrics"]["moves"], "reason": "Review task meaning, independent outcome and stopping point, placement/timing, and preservation of prior evidence."})
        final_check = _check(["final state differs from last committed step"] if prior is not None and _state_value(prior) != _state_value(replay["final"]) else [])
        summary = _summary(scored)
        summary.update(pending_final=len(replay["final"].get("open_pending", [])), tasks_final=len(replay["final"].get("tasks", [])), final_state_pass=final_check["passed"])
        scored_streams.append({"id": stream_id, "kind": stream["kind"], "summary": summary, "steps": scored, "final_check": final_check})
    return {
        "schema": 1, "bundle_sha256": result.get("bundle_sha256"), "run": result.get("run", {}),
        "reference": {"status": (reference or {}).get("status", "unprovided"), "sha256": hashlib.sha256(_canonical(reference).encode("utf-8")).hexdigest() if reference is not None else None},
        "scope": {"semantic_accuracy": None, "semantic_mapping": "human_review_required", "timing": "event alternatives only; no fixed target tree", "reference_match": "action and affected-period constraints only; not a semantic correctness score"},
        "summary": {**{kind: _summary([step for stream in scored_streams if stream["kind"] == kind for step in stream["steps"]]) for kind in ("real", "synthetic")}, "streams": {stream["id"]: stream["summary"] for stream in scored_streams}},
        "streams": scored_streams, "needs_human_review": needs_review,
    }
