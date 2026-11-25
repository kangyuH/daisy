"""Validate, replay and score local snapshots. See docs/phase0-eval.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from phase0_eval.dataset import Input, fingerprint, load_dataset
from phase0_eval.isolation import IsolationError, ModelTransport, install_runner_guard
from phase0_eval.ledger import exercise_replay
from phase0_eval.scoring import build_report, compare_reports

ENGINES = ("legacy", "multi")
ROOT = Path(__file__).resolve().parents[1]


def dump_file(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def new_output(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)


def source_hashes() -> dict:
    paths = [
        *Path(__file__).resolve().parent.glob("*.py"),
        *(ROOT / "workers" / "dispatcher" / name for name in
          ("agent.py", "prompts.py", "state.py", "tools.py")),
    ]
    return {path.relative_to(ROOT).as_posix(): fingerprint(path.read_bytes()) for path in sorted(paths)}


def classify_error(exc: Exception) -> dict:
    # API failures can contain headers or provider payloads. Only keep a status
    # or exception class for those. The router may provide a sanitized trace.
    cause = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    name = type(cause).__name__
    kind = "timeout" if "timeout" in name.lower() else "model_error"
    if isinstance(cause, (ValueError, json.JSONDecodeError)):
        kind = "invalid_output"
    status = getattr(cause, "status_code", None)
    return {"kind": kind, "type": name, "status": status}


def get_prediction(engine, bundle, case, llm, max_iterations):
    from phase0_eval.routing import route

    start = time.monotonic()
    try:
        result = route(engine, bundle, case, llm, max_iterations=max_iterations)
        if engine == "multi":
            result["replay"] = exercise_replay(case["id"], result["relations"])
        return result
    except Exception as exc:
        partial = getattr(exc, "partial_result", None) or {}
        return partial | {
            "engine": engine, "case_id": case["id"], "error": classify_error(exc),
            "relations": [], "candidates": case["candidates"],
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
        }


def run_models(bundle, *, llm, args):
    start = time.monotonic()
    cases = bundle["cases"]
    jobs = [(engine, case) for case in cases for engine in ENGINES]
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(get_prediction, engine, bundle, case, llm, args.max_iterations)
            for engine, case in jobs
        ]
        for index, future in enumerate(futures, 1):
            result = future.result()
            results.append(result)
            if result.get("error"):
                print(f"{result['case_id']} {result['engine']}: {result['error']}", file=sys.stderr)
            if index % 10 == 0 or index == len(futures):
                print(f"processed {index}/{len(futures)} predictions", flush=True)
    return results, round(time.monotonic() - start, 2)


def record_predictions(bundle: Input, args):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    config = {
        "mode": args.mode, "started_at": timestamp, "max_iterations": args.max_iterations,
        "source_sha256": source_hashes(), "jobs": args.jobs,
    }
    if args.mode == "stub":
        results, duration = run_models(bundle.data, llm=None, args=args)
    else:
        from langchain_openai import ChatOpenAI
        from openai import OpenAI

        class ModelClient(OpenAI):
            def platform_headers(self):
                # SDK diagnostics can invoke uname via a subprocess.
                # The model request does not require those headers.
                return {}

        key = os.environ.get("PHASE0_MODEL_API_KEY", "").strip()
        if not key:
            raise ValueError("PHASE0_MODEL_API_KEY is required in live mode")
        config.update({
            "model": args.model, "base_url": args.base_url, "timeout": args.timeout,
            "temperature": 0.2, "max_tokens": 2048, "thinking": "disabled",
        })
        with httpx.Client(transport=ModelTransport(args.base_url), trust_env=False,
                          timeout=args.timeout, follow_redirects=False) as client:
            model_client = ModelClient(
                api_key=key, base_url=args.base_url, max_retries=0, http_client=client,
            )
            llm = ChatOpenAI(
                model=args.model, base_url=args.base_url, api_key=key, temperature=0.2,
                max_tokens=2048, max_retries=0, timeout=args.timeout, disable_streaming=True,
                extra_body={"thinking": {"type": "disabled"}}, http_client=client,
                root_client=model_client, client=model_client.chat.completions,
            )
            results, duration = run_models(bundle.data, llm=llm, args=args)
    config["elapsed_seconds"] = duration
    return {"schema": 1, "bundle_sha256": bundle.digest, "run": config, "predictions": results}


def write_report(out: Path, bundle: Input, labels: Input, predictions: dict, *, save_predictions: bool):
    from phase0_eval.review import render_html, render_xml

    report = build_report(bundle.data, labels.data, bundle.digest, labels.digest, predictions)
    html = render_html(bundle.data, labels.data, report)
    xml = render_xml(bundle.data, labels.data, report)
    bundle.check_unchanged()
    labels.check_unchanged()
    new_output(out)
    if save_predictions:
        dump_file(out / "predictions.json", predictions)
    dump_file(out / "report.json", report)
    (out / "review.html").write_text(html, encoding="utf-8")
    (out / "review.xml").write_text(xml, encoding="utf-8")
    print(f"report: {out / 'report.json'}")
    return any(
        result["prediction"].get("error")
        for case in report["cases"] for result in case["engines"].values()
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate bundle and labels without a model")
    run = commands.add_parser("run", help="run both routers and write a new report directory")
    score = commands.add_parser("score", help="score saved predictions against a new label version")
    for command in (validate, run, score):
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--labels", type=Path, required=True)
    for command in (run, score):
        command.add_argument("--out", type=Path, required=True)
    run.add_argument("--mode", choices=("stub", "live"), default="stub")
    run.add_argument("--model", default="deepseek-flash")
    run.add_argument("--base-url", default="https://api.deepseek.com")
    run.add_argument("--timeout", type=float, default=60)
    run.add_argument("--max-iterations", type=int, default=6)
    run.add_argument("--jobs", type=int, default=4)
    score.add_argument("--predictions", type=Path, required=True)
    compare = commands.add_parser("compare", help="compare evaluations of the same bundle")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        install_runner_guard()
        if args.command == "compare":
            before, after = Input.read(args.before), Input.read(args.after)
            result = compare_reports(before.data, after.data)
            before.check_unchanged()
            after.check_unchanged()
            dump_file(args.out, result)
            return 0

        bundle, labels = load_dataset(args.bundle, args.labels)
        if args.command == "validate":
            print(f"{len(bundle.data['cases'])} cases, {len(bundle.data['tasks'])} tasks")
            print(f"bundle sha256={bundle.digest}\nlabels sha256={labels.digest}")
            return 0
        if args.out.exists():
            raise ValueError(f"output directory already exists: {args.out}")

        if args.command == "run":
            if not (1 <= args.jobs <= 16 and 1 <= args.max_iterations <= 12 and 0 < args.timeout <= 300):
                raise ValueError("expected 1–16 jobs, 1–12 iterations and timeout at most 300 seconds")
            predictions = record_predictions(bundle, args)
        else:
            saved = Input.read(args.predictions)
            predictions = saved.data
            saved.check_unchanged()
        has_errors = write_report(
            args.out, bundle, labels, predictions, save_predictions=args.command == "run",
        )
        return 2 if has_errors else 0
    except IsolationError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
