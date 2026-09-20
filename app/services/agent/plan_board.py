from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def extract_plan_markdown_from_log(log_path: Path) -> Optional[str]:
    """
    Pull plan markdown from Cursor CLI stream-json (CreatePlan tool args).

    Headless `--mode plan` often never writes plan.md (CreatePlan is UI-oriented);
    the authoritative text still appears in createPlanToolCall.args.plan.
    """
    if not log_path.is_file():
        return None
    last: Optional[str] = None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        plan = _plan_from_obj(obj)
        if plan:
            last = plan
    return last


def _plan_from_obj(obj: dict) -> Optional[str]:
    tc = obj.get("tool_call")
    if isinstance(tc, dict):
        for key in ("createPlanToolCall", "create_plan_tool_call"):
            block = tc.get(key)
            if isinstance(block, dict):
                args = block.get("args")
                if isinstance(args, dict):
                    plan = args.get("plan") or args.get("content") or args.get("body")
                    if isinstance(plan, str) and plan.strip():
                        return plan.strip()
    # interaction_query createPlanRequestQuery
    if obj.get("type") == "interaction_query":
        query = obj.get("query")
        if isinstance(query, dict):
            cpr = query.get("createPlanRequestQuery")
            if isinstance(cpr, dict):
                args = cpr.get("args")
                if isinstance(args, dict):
                    plan = args.get("plan")
                    if isinstance(plan, str) and plan.strip():
                        return plan.strip()
    return None


def persist_plan_markdown(task_dir: Path, markdown: str) -> Path:
    dest = task_dir / "plan.md"
    dest.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    return dest


def format_plan_copy_followup(*, plan_text: str, plan_path: Path) -> str:
    excerpt = (plan_text or "").strip()
    if len(excerpt) > 6000:
        excerpt = excerpt[:6000] + "\n\n…(truncated)"
    return (
        "## 执行计划（Gateway 从 Plan 模式同步）\n\n"
        f"权威稿：`{plan_path}`（飞书手改不回流）\n\n"
        + (excerpt or "_(空 plan)_")
    )


def format_plan_missing_followup(*, log_path: Path, returncode: int) -> str:
    return (
        "## 执行计划同步失败\n\n"
        f"本轮 `--mode plan` 结束（returncode={returncode}），但未找到 "
        "`plan.md`，也未从 `CreatePlan` stream-json 抽到正文。\n"
        f"请查看 `{log_path}`，或再发 `/plan`。\n"
        "（Gateway 硬同步：不依赖模型自觉写盘 / followup。）"
    )
