"""Compare model predictions with versioned, possibly alternative annotations."""

from __future__ import annotations

from collections import Counter


def target_actions(relations):
    return {r["target"] for r in relations}, {r["target"] for r in relations if r["wake"] is True}


def evaluate_case(case, annotation, prediction, month_by_id):
    if prediction.get("error"):
        return {"error": prediction["error"]}

    actual, waking = target_actions(prediction["relations"])
    wake_supported = prediction["engine"] == "multi"
    options = [target_actions(option) for option in annotation["options"]]
    option_index, (expected, expected_wake) = min(
        enumerate(options),
        key=lambda entry: (
            len(actual ^ entry[1][0]),
            len(waking ^ entry[1][1]) if wake_supported else 0,
            entry[0],
        ),
    )
    missing = expected - actual
    extra = actual - expected
    expected_months = {month_by_id[target] for target in expected} - {None}
    wrong_period = {
        target for target in extra
        if expected_months and month_by_id[target] not in expected_months | {None}
    }
    return {
        "option": option_index + 1,
        "expected": sorted(expected),
        "actual": sorted(actual),
        "exact_targets": not missing and not extra,
        "missing": sorted(missing),
        "extra": sorted(extra),
        "wrong_period": sorted(wrong_period),
        "missed_targets": len(missing) if len(expected) > 1 else 0,
        "matched_targets": len(expected & actual),
        "multi_expected": len(expected) > 1,
        "legacy_multi_limit": not wake_supported and all(len(targets) > 1 for targets, _ in options),
        "wake_supported": wake_supported,
        "wake_missed": sorted(expected_wake - waking) if wake_supported else None,
        "wake_extra": sorted(waking - expected_wake) if wake_supported else None,
        "root_expected": case["root"] in expected,
        "root_actual": case["root"] in actual,
    }


def summarize(rows):
    counts = Counter({
        "cases": 0, "errors": 0, "exact_targets": 0, "matched_targets": 0,
        "missing_targets": 0, "extra_targets": 0, "wrong_period": 0,
        "multi_expected": 0, "missed_targets": 0, "legacy_multi_limit": 0,
        "root_expected": 0, "root_actual": 0, "root_unexpected": 0,
        "wake_supported": 0, "wake_missed": 0, "wake_extra": 0,
    })
    errors = Counter()
    elapsed = 0
    tokens = Counter()
    for row in rows:
        counts["cases"] += 1
        prediction = row["prediction"]
        outcome = row["outcome"]
        elapsed += prediction.get("elapsed_ms", 0)
        for key, value in (prediction.get("usage") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                tokens[key] += value
        if "error" in outcome:
            counts["errors"] += 1
            errors[outcome["error"]["kind"]] += 1
            continue
        for key in (
            "exact_targets", "matched_targets", "multi_expected", "missed_targets",
            "legacy_multi_limit", "root_expected", "root_actual", "wake_supported",
        ):
            counts[key] += outcome[key]
        counts["root_unexpected"] += outcome["root_actual"] and not outcome["root_expected"]
        counts["missing_targets"] += len(outcome["missing"])
        counts["extra_targets"] += len(outcome["extra"])
        counts["wrong_period"] += bool(outcome["wrong_period"])
        counts["wake_missed"] += len(outcome["wake_missed"] or [])
        counts["wake_extra"] += len(outcome["wake_extra"] or [])
    evaluated = counts["cases"] - counts["errors"]
    tp = counts["matched_targets"]
    predicted = tp + counts["extra_targets"]
    expected = tp + counts["missing_targets"]
    return dict(counts) | {
        "evaluated": evaluated,
        "exact_rate": counts["exact_targets"] / evaluated if evaluated else None,
        "precision": tp / predicted if predicted else None,
        "recall": tp / expected if expected else None,
        "elapsed_ms": round(elapsed, 1), "usage": dict(tokens), "error_kinds": dict(errors),
    }


def build_report(bundle, labels, bundle_digest, labels_digest, predictions):
    if predictions.get("schema") != 1 or predictions.get("bundle_sha256") != bundle_digest:
        raise ValueError("predictions were created with a different bundle or schema")
    engines = ("legacy", "multi")
    known = {case["id"] for case in bundle["cases"]}
    expected_keys = {(case_id, engine) for case_id in known for engine in engines}
    rows = {}
    for prediction in predictions["predictions"]:
        key = prediction["case_id"], prediction["engine"]
        if key not in expected_keys or key in rows:
            raise ValueError(f"unknown or duplicate prediction {key}")
        rows[key] = prediction
    if set(rows) != expected_keys:
        raise ValueError("predictions do not cover both engines and all cases")
    month_by_id = {task["id"]: task["month"] for task in bundle["tasks"]}
    cases = []
    for case in bundle["cases"]:
        annotation = labels["cases"][case["id"]]
        scored = {}
        for engine in engines:
            prediction = rows[case["id"], engine]
            scored[engine] = {
                "prediction": prediction,
                "outcome": evaluate_case(case | {"root": bundle["root"]}, annotation, prediction, month_by_id),
            }
        cases.append({
            "case_id": case["id"], "group": case["group"], "kind": case["kind"],
            "tags": case["tags"], "engines": scored,
        })

    summary = {}
    for engine in engines:
        summary[engine] = {
            "real": {
                group: summarize(row["engines"][engine] for row in cases
                                 if row["kind"] == "real" and row["group"] == group)
                for group in ("dev", "holdout")
            },
            "synthetic": summarize(row["engines"][engine] for row in cases if row["kind"] == "synthetic"),
        }
    return {
        "schema": 1, "bundle_sha256": bundle_digest,
        "labels": {"version": labels["version"], "status": labels["status"], "sha256": labels_digest},
        "run": predictions["run"],
        "ingress": dict(Counter(case["gate"] for case in bundle["cases"] if case["kind"] == "real")),
        "summary": summary, "cases": cases,
    }


def compare_reports(before, after):
    for field in ("schema", "bundle_sha256"):
        if before[field] != after[field]:
            raise ValueError(f"reports use different {field}; cannot compare case-by-case")
    before_ids = [row["case_id"] for row in before["cases"]]
    after_ids = [row["case_id"] for row in after["cases"]]
    if before_ids != after_ids:
        raise ValueError("reports have different case sets or ordering")
    transitions = {}
    for left, right in zip(before["cases"], after["cases"]):
        for engine in ("legacy", "multi"):
            previous = left["engines"][engine]["outcome"]
            current = right["engines"][engine]["outcome"]
            if previous != current:
                transitions.setdefault(left["case_id"], {})[engine] = {
                    "before": previous, "after": current,
                }
    return {
        "bundle_sha256": before["bundle_sha256"],
        "before": {"run": before["run"], "labels": before["labels"], "summary": before["summary"]},
        "after": {"run": after["run"], "labels": after["labels"], "summary": after["summary"]},
        "changed_outcomes": transitions,
    }
