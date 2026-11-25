"""Acceptance tests for the file-only runner, without the app test fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from phase0_eval.dataset import Bundle, validate_labels
from phase0_eval.ledger import exercise_replay
from phase0_eval.scoring import evaluate_case


@pytest.fixture()
def tiny_dataset():
    def task(task_id, parent, month):
        return {
            "id": task_id, "parent": parent, "title": f"{month or 'Demo'} source tables",
            "month": month, "status": "noted", "objective": "Process source tables",
            "scope": "Only the selected delivery", "acceptance": "QA accepted", "stop": "QA approved",
        }

    bundle = {
        "version": 1, "project": "demo", "root": "ROOT", "provenance": {"source": "synthetic"},
        "tasks": [task("ROOT", None, None), task("AUG", "ROOT", "2026-08"), task("SEP", "ROOT", "2026-09")],
        "cases": [{
            "id": "S-001", "group": "dev", "kind": "synthetic", "thread": "SYNTH-1",
            "source": "Synthetic fixture", "text": "9月源表到齐。8月验收口径需要复核。",
            "context": [], "candidates": ["ROOT", "AUG", "SEP"], "tags": ["cross_period", "multi"],
            "mentions": [], "speaker": "业务方",
        }],
    }
    labels = {
        "version": "synthetic-v1", "status": "draft", "cases": {"S-001": {
            "options": [[
                {"quote": "9月源表到齐", "target": "SEP", "wake": False},
                {"quote": "8月验收口径需要复核", "target": "AUG", "wake": True},
            ]],
            "reason": "9月仅留存，8月重新确认口径",
        }},
    }
    return bundle, labels


def test_validation_and_separate_wake_scoring(tiny_dataset):
    bundle, labels = tiny_dataset
    bundle = Bundle.model_validate(bundle).model_dump()
    validate_labels(bundle, labels)
    case = bundle["cases"][0]
    annotation = labels["cases"][case["id"]]
    months = {row["id"]: row["month"] for row in bundle["tasks"]}
    relations = annotation["options"][0]
    predicted = {"relations": relations, "engine": "multi"}
    outcome = evaluate_case(case | {"root": "ROOT"}, annotation, predicted, months)
    assert outcome["exact_targets"] and not outcome["wake_extra"] and not outcome["wake_missed"]
    legacy = {"relations": [relations[0] | {"wake": None}], "engine": "legacy"}
    legacy_outcome = evaluate_case(case | {"root": "ROOT"}, annotation, legacy, months)
    assert legacy_outcome["missing"] == ["AUG"] and legacy_outcome["legacy_multi_limit"]
    assert legacy_outcome["wake_missed"] is None

    bundle["cases"].append(case | {"id": "S-002", "group": "holdout"})
    with pytest.raises(ValueError, match="thread crosses"):
        Bundle.model_validate(bundle)

    annotation["options"][0][0]["quote"] = "不存在的句子"
    with pytest.raises(ValueError, match="not a substring"):
        validate_labels(bundle | {"cases": [case]}, labels)


def test_replay_does_not_duplicate_links_wakeups_or_outbound(tiny_dataset):
    _, labels = tiny_dataset
    result = exercise_replay("S-001", labels["cases"]["S-001"]["options"][0])
    assert result["first"] == {"associations": 2, "wakeups": 1, "outbound": 0, "duplicate": False}
    assert result["duplicate"] == {"associations": 0, "wakeups": 0, "outbound": 0, "duplicate": True}
    assert result["retry"]["before_commit"] == result["first"]
    assert result["retry"]["after_commit"] == result["duplicate"]


def test_guard_fails_closed_in_a_separate_process(tmp_path):
    program = """
import os, socket, subprocess
from phase0_eval.isolation import install_runner_guard, IsolationError, ModelTransport
install_runner_guard()
r = socket.socket()
from app.infra import db
from app.infra.lark import cli
from workers.client import GatewayClient
import httpx
transport = ModelTransport("https://api.deepseek.com")
actions = [
    lambda: r.connect(("127.0.0.1", 8000)),
    lambda: socket.getaddrinfo("example.com", 443),
    lambda: socket.gethostbyname("example.com"),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 8000)),
    lambda: subprocess.run(["lark-cli", "--help"]),
    lambda: GatewayClient("http://127.0.0.1:8000").list_tasks(),
    lambda: db.connect_sync("should-not-exist.db"),
    lambda: db.sqlite3.connect("should-not-exist.db"),
    lambda: cli.run_cli(["im", "+messages-send"]),
    lambda: open(".env").close(),
    lambda: transport.handle_request(httpx.Request("POST", "https://open.feishu.cn/open-apis")),
]
for action in actions:
    try:
        action()
    except IsolationError:
        pass
    else:
        raise AssertionError("disallowed operation was not blocked")
"""
    subprocess.run([sys.executable, "-c", program], check=True, timeout=25)
    assert not Path("should-not-exist.db").exists()


def cli(*args):
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "GATEWAY_BASE_URL": "http://127.0.0.1:1",
        "GATEWAY_DB_PATH": "/nonexistent/test.db",
        "PHASE0_MODEL_API_KEY": "",
    }
    return subprocess.run(
        [sys.executable, "-m", "phase0_eval", *map(str, args)],
        env=env, check=True, capture_output=True, text=True, timeout=30,
    )


def test_cli_stub_and_rescore_use_the_same_saved_predictions(tiny_dataset, tmp_path):
    bundle, labels = tiny_dataset
    bundle_file, labels_file = tmp_path / "bundle.json", tmp_path / "labels.json"
    bundle_file.write_text(json.dumps(bundle), encoding="utf-8")
    labels_file.write_text(json.dumps(labels), encoding="utf-8")
    original = bundle_file.read_bytes(), labels_file.read_bytes()
    initial = tmp_path / "first"
    common = ["--bundle", bundle_file, "--labels", labels_file]
    cli("validate", *common)
    cli("run", *common, "--out", initial)
    assert (initial / "review.html").is_file() and (initial / "review.xml").is_file()
    assert original == (bundle_file.read_bytes(), labels_file.read_bytes())
    updated = tmp_path / "labels-v2.json"
    updated.write_text(json.dumps(labels | {"version": "v2", "status": "accepted"}), encoding="utf-8")
    second = tmp_path / "second"
    cli("score", "--bundle", bundle_file, "--labels", updated,
        "--predictions", initial / "predictions.json", "--out", second)
    comparison = tmp_path / "comparison.json"
    cli("compare", initial / "report.json", second / "report.json", "--out", comparison)
    diff = json.loads(comparison.read_text(encoding="utf-8"))
    assert diff["after"]["labels"]["status"] == "accepted"
    assert diff["changed_outcomes"] == {}
    with pytest.raises(subprocess.CalledProcessError) as error:
        cli("run", "--bundle", tmp_path / "missing.json", "--labels", updated, "--out", tmp_path / "retry")
    assert error.value.returncode == 1
    assert "missing input file" in error.value.stderr
    assert not (tmp_path / "retry").exists()
