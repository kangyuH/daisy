"""Pure, file-driven review of the message-by-message task-growth experiment.

The renderers consume frozen input, run snapshots and optional draft references.
They never connect to Feishu, a Gateway, a task store or a model service.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


STATUS = {"applied": "已应用", "duplicate": "重复投递，未新增效果", "rejected": "校验拒绝，未应用",
          "error": "运行错误", "not_run": "尚未运行", "active": "进行中", "open": "进行中",
          "noted": "已记录", "waiting": "等待", "waiting_human": "等待人工",
          "waiting_external": "等待外部", "blocked": "阻塞", "done": "完成", "closed": "已关闭"}
ACTION = {"create": "创建任务", "route": "挂载消息", "move": "迁移关联", "progress": "记录进展",
          "wait": "设置等待", "resume": "恢复推进", "complete": "确认完成", "reopen": "重新打开", "contract": "修订承诺",
          "evidence": "仅留证", "maintain": "交给owner维护", "pending": "根待判断", "noop": "无任务动作"}
FIELD = {"status": "状态", "outcome": "交付结果", "done_when": "停止条件", "progress": "进展",
         "wait_for": "等待对象", "parent": "父任务", "title": "名称", "period": "业务期",
         "target": "目标", "quote": "原句", "why": "依据", "kind": "处理", "action": "动作",
         "op": "动作", "message_id": "源消息", "id": "编号", "created_from": "创建来源"}


def _s(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return "；".join(_s(item) for item in value) or "—"
    if isinstance(value, dict):
        return "；".join(FIELD.get(key, key) + "：" + _s(item) for key, item in value.items()) or "—"
    return str(value)


def _e(value: Any) -> str:
    return html.escape(_s(value), quote=True)


def _p(value: Any) -> str:
    return "<p>" + _e(value).replace("\n", "<br/>") + "</p>"


def _table(headers: tuple[str, ...], rows: list[list[Any]]) -> str:
    if not rows:
        return _p("无")
    head = "".join("<th>" + _e(item) + "</th>" for item in headers)
    body = "".join("<tr>" + "".join("<td>" + _p(item) + "</td>" for item in row) + "</tr>" for row in rows)
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>"


def _task_label(task: dict) -> str:
    period = task.get("period") or ("项目根" if task.get("parent") is None else "业务期未定")
    return f"{task['id']} · {task.get('title', '')} · {period} · {STATUS.get(task.get('status'), task.get('status', ''))}"


def tree_lines(snapshot: dict | None, *, contracts: bool = False) -> list[str]:
    """Render a snapshot's actual topology; never invent missing ancestors."""
    if snapshot is None:
        return ["未提供该时点快照"]
    tasks = snapshot.get("tasks", [])
    if not tasks:
        return ["空树"]
    nodes = {task["id"]: task for task in tasks}
    if len(nodes) != len(tasks):
        raise ValueError("Duplicate task ID in growth snapshot")
    children: dict[str | None, list[dict]] = {}
    for task in tasks:
        parent = task.get("parent")
        if parent is not None and parent not in nodes:
            raise ValueError("Snapshot parent does not exist: " + str(parent))
        children.setdefault(parent, []).append(task)
    lines: list[str] = []
    visited: set[str] = set()
    def walk(task: dict, depth: int) -> None:
        if task["id"] in visited:
            raise ValueError("Cycle in growth snapshot")
        visited.add(task["id"])
        prefix = "　" * depth
        lines.append(prefix + ("└ " if depth else "") + _task_label(task))
        if contracts:
            for key in ("outcome", "done_when", "progress", "wait_for"):
                if task.get(key):
                    lines.append(prefix + "　" + FIELD[key] + "：" + _s(task[key]))
        for child in children.get(task["id"], []):
            walk(child, depth + 1)
    for task in children.get(None, []):
        walk(task, 0)
    if len(visited) != len(tasks):
        raise ValueError("Cycle or rootless task in growth snapshot")
    return lines


def _attempt_rows(step: dict) -> list[list[Any]]:
    attempts = step.get("attempts", [])
    if len(attempts) < 2:
        return []
    return [[item.get("number"), STATUS.get(item.get("status"), item.get("status")), item.get("error") or "—"]
            for item in attempts]


def _comparison_rows(result: dict) -> list[list[Any]]:
    baseline = result.get("baseline")
    if not baseline:
        return []
    rows = []
    for label, run in (("首跑", baseline), ("本轮", result)):
        for stream in run.get("streams", []):
            statuses = [step.get("status") for step in stream.get("steps", [])]
            final = stream.get("final") or {}
            tasks = final.get("tasks", [])
            created = sum(task.get("parent") is not None for task in tasks)
            repaired = sum(len(step.get("attempts", [])) > 1 for step in stream.get("steps", []))
            rows.append([label, stream["id"], statuses.count("applied"), statuses.count("rejected"),
                         statuses.count("error"), created, len(final.get("open_pending", [])), repaired])
    return rows


def _pending_rows(snapshot: dict | None) -> list[list[Any]]:
    return [[item.get("message_id"), item.get("quote"), item.get("target"), item.get("why", "")]
            for item in (snapshot or {}).get("open_pending", [])]


def _diff_rows(step: dict) -> list[list[Any]]:
    before = {task["id"]: task for task in (step.get("before") or {}).get("tasks", [])}
    after = {task["id"]: task for task in (step.get("after") or {}).get("tasks", [])}
    rows = []
    for task_id, task in after.items():
        if task_id not in before:
            rows.append([task_id, "新增任务", "—", task.get("title")])
            continue
        for field in ("parent", "title", "period", "outcome", "done_when", "status", "progress", "wait_for"):
            old, new = before[task_id].get(field), task.get(field)
            if old != new:
                rows.append([task_id, FIELD[field], STATUS.get(old, old) if field == "status" else old,
                             STATUS.get(new, new) if field == "status" else new])
    for task_id in before.keys() - after.keys():
        rows.append([task_id, "节点消失，需复核", before[task_id].get("title"), "—"])
    return rows


def _score_index(result: dict) -> dict[tuple[str, str], dict]:
    score = result.get("score") or {}
    return {(stream["id"], step["event_id"]): step
            for stream in score.get("streams", []) for step in stream.get("steps", [])}


def _reference(reference: dict | None, stream_id: str, event_id: str) -> dict | None:
    return (reference or {}).get("streams", {}).get(stream_id, {}).get("events", {}).get(event_id)


def _match_text(score_step: dict | None) -> str:
    if not score_step or not score_step.get("reference"):
        return "未做参考动作比较；任务边界和拆分仍需人工审阅。"
    reference = score_step["reference"]
    matched = reference.get("matched", reference.get("action_match", reference.get("reference_match")))
    if matched is True:
        return "与参考草稿的动作和期次约束一致；这不表示任务树的业务语义已经通过人工验收。"
    if matched is False:
        return "与参考草稿的动作和期次约束有差异，请同时审阅草稿与实际动作。"
    return "参考动作比较未给出明确判定。"


def _check_rows(score_step: dict | None) -> list[list[Any]]:
    names = {"tree": "树结构", "links": "消息关联", "continuity": "状态连续性", "created": "创建依据",
             "duplicate": "重复投递", "atomic": "拒绝后无变化", "moves": "迁移依据"}
    rows = []
    for key, check in (score_step or {}).get("checks", {}).items():
        if not isinstance(check, dict):
            continue
        passed = check.get("passed")
        rows.append([names.get(key, key), "通过" if passed is True else "有差异" if passed is False else "未判定",
                     check.get("violations") or "—"])
    return rows


def _reference_differences(score_step: dict | None) -> list[str]:
    reference = (score_step or {}).get("reference") or {}
    notes = []
    if reference.get("missing_actions"):
        notes.append("草稿要求但未匹配的动作：" + "；".join(_constraint_text(item) for item in reference["missing_actions"]))
    if reference.get("forbidden_actions"):
        notes.append("匹配到草稿不允许的动作：" + "；".join(_constraint_text(item) for item in reference["forbidden_actions"]))
    if reference.get("period_match") is False:
        notes.append("业务期约束有差异；草稿：" + _s(reference.get("expected_periods")) + "；实际：" + _s(reference.get("actual_periods")))
    return notes


def _constraint_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ACTION.get(item, _s(item)) if isinstance(item, str) else _s(item)
    parts = []
    for key, value in item.items():
        text = ACTION.get(value, value) if isinstance(value, str) and key in ("op", "action", "kind", "type") else value
        parts.append(FIELD.get(key, key) + "：" + _s(text))
    return "；".join(parts)


def _reference_rows(annotation: dict | None) -> list[list[Any]]:
    if not annotation:
        return []
    rows = []
    for number, option in enumerate(annotation.get("options", []), 1):
        require = "\n".join(_constraint_text(item) for item in option.get("require", [])) or "无额外必做动作"
        forbid = "\n".join(_constraint_text(item) for item in option.get("forbid", [])) or "未另列禁止动作"
        period = _s(option.get("periods", []))
        rows.append([number, require, forbid, period])
    return rows


def _stream_views(bundle: dict, result: dict, reference: dict | None) -> list[dict]:
    result_streams = {stream["id"]: stream for stream in result.get("streams", [])}
    bundle_streams = {stream["id"]: stream for stream in bundle.get("streams", [])}
    if result_streams.keys() - bundle_streams.keys():
        raise ValueError("Run contains an unknown input stream")
    scoring = _score_index(result)
    views = []
    for stream in bundle.get("streams", []):
        stream_id = stream["id"]
        run_stream = result_streams.get(stream_id, {})
        events = {event["id"]: event for event in stream["events"]}
        steps = {step["event_id"]: step for step in run_stream.get("steps", [])}
        if len(steps) != len(run_stream.get("steps", [])) or steps.keys() - events.keys():
            raise ValueError("Run contains duplicate or unknown event IDs")
        ordered = [step["event_id"] for step in run_stream.get("steps", [])]
        ordered.extend(event_id for event_id in events if event_id not in steps)
        viewed_steps = []
        for event_id in ordered:
            step = steps.get(event_id, {"event_id": event_id, "status": "not_run"})
            score_step = scoring.get((stream_id, event_id))
            annotation = _reference(reference, stream_id, event_id)
            match_reference = (score_step or {}).get("reference") or {}
            mismatch = match_reference.get("matched") is False
            mechanically_failed = any(isinstance(check, dict) and check.get("passed") is False
                                      for check in (score_step or {}).get("checks", {}).values())
            issue = step["status"] in ("rejected", "error", "not_run") or mismatch or mechanically_failed or len(step.get("attempts", [])) > 1
            viewed_steps.append({"event": events[event_id], "step": step, "score": score_step,
                                 "reference": annotation, "issue": issue})
        views.append({"stream": stream, "result": run_stream, "steps": viewed_steps})
    return views


def _overview_rows(views: list[dict]) -> list[list[Any]]:
    rows = []
    for view in views:
        steps = view["steps"]
        statuses = [entry["step"]["status"] for entry in steps]
        scored = [(entry.get("score") or {}).get("reference") for entry in steps]
        rated = [item for item in scored if item and isinstance(item.get("matched"), bool)]
        matched = sum(item["matched"] for item in rated)
        rows.append([view["stream"]["id"], "真实消息" if view["stream"]["kind"] == "real" else "合成补充",
                     len(steps), statuses.count("applied"), statuses.count("duplicate"),
                     statuses.count("rejected"), statuses.count("error"),
                     f"{matched}/{len(rated)}" if rated else "未评"])
    return rows


def _action_sections(step: dict) -> list[tuple[str, tuple[str, ...], list[list[Any]]]]:
    applied = step.get("applied") or {}
    return [
        ("创建", ("任务ID", "父任务", "业务期", "名称", "独立交付结果", "停止条件"),
         [[item.get(key) for key in ("id", "parent", "period", "title", "outcome", "done_when")]
          for item in applied.get("created", [])]),
        ("更新", ("目标", "动作", "消息依据", "理由", "更新值"),
         [[item.get("target"), ACTION.get(item.get("action"), item.get("action")), item.get("quote"), item.get("why"),
           {FIELD[key]: item[key] for key in ("status", "progress", "outcome", "done_when", "wait_for") if key in item}]
          for item in applied.get("updates", [])]),
        ("挂载", ("源消息", "原句", "目标", "处理"),
         [[item.get("message_id"), item.get("quote"), item.get("target"), ACTION.get(item.get("kind"), item.get("kind"))]
          for item in applied.get("routes", [])]),
        ("迁移", ("源消息", "先前待判断原句", "本条迁移依据", "迁入目标", "理由"),
         [[item.get("message_id"), item.get("source_quote"), item.get("quote"), item.get("target"), item.get("why")]
          for item in applied.get("moves", [])]),
    ]


def _step_xml(stream_id: str, entry: dict) -> str:
    event, step = entry["event"], entry["step"]
    out = ["<h2>" + _e(event["id"] + " · " + _s(event.get("time"))) + "</h2>",
           _p("处理结果：" + STATUS.get(step["status"], step["status"])),
           _p("当时已有树：\n" + "\n".join(tree_lines(step.get("before")))),
           _p("输入原文：" + event["text"])]
    before_pending = _pending_rows(step.get("before"))
    if before_pending:
        out.insert(3, _table(("当时待判断源消息", "原句", "目标", "理由"), before_pending))
    if event.get("source"):
        out.append(_p("来源：" + _s(event["source"])))
    for previous in (step.get("input") or {}).get("same_thread_before", []):
        out.append(_p("同话题此前 " + _s(previous.get("id")) + "：" + _s(previous.get("text"))))
    for context in event.get("context", []):
        out.append(_p("当时背景：" + _s(context.get("quote")) + ("；来源：" + _s(context["source"]) if context.get("source") else "")))
    if event.get("replay_of"):
        out.append(_p("重复投递来源：" + _s(event["replay_of"])))
    if step.get("reason") or step.get("error"):
        out.append(_p("本条依据或错误：" + _s(step.get("reason") or step.get("error"))))
    attempts = _attempt_rows(step)
    if attempts:
        out.extend([_p("本条触发一次自动反馈修正；首次被拒绝的提议没有改变当时的树。"),
                    _table(("提议次数", "校验或应用结果", "首次错误与反馈"), attempts)])
    has_effect = False
    for title, headers, rows in _action_sections(step):
        if rows:
            has_effect = True
            out.extend([_p("实际" + title), _table(headers, rows)])
            if title == "创建":
                for task in (step.get("applied") or {}).get("created", []):
                    out.append(_p("创建 " + _s(task.get("id")) + " 的原句依据：" + _s(task.get("quote"))
                                  + "；理由：" + _s(task.get("why"))))
    if not has_effect:
        out.append(_p("本条没有新增已应用动作；重复、拒绝或错误不能据提议推定已经改树。"))
    differences = _diff_rows(step)
    out.append(_p("状态与承诺差异"))
    out.append(_table(("任务", "字段", "之前", "之后"), differences) if differences else _p("任务节点字段无变化。"))
    pending_before, pending_after = _pending_rows(step.get("before")), _pending_rows(step.get("after"))
    if pending_before != pending_after:
        out.extend([_p("处理后的根待判断"), _table(("源消息", "原句", "目标", "理由"), pending_after)])
    checks = _check_rows(entry["score"])
    if checks:
        out.extend([_p("回放约束检查"), _table(("项目", "结果", "差异"), checks)])
    annotation = entry["reference"]
    out.extend([_p("参考草稿说明：" + _s(annotation.get("note")) if annotation else "本条尚无参考草稿标注。"),
                _p(_match_text(entry["score"]))])
    for difference in _reference_differences(entry["score"]):
        out.append(_p(difference))
    if annotation:
        out.append(_table(("可接受方案", "必做动作", "不应做的动作", "业务期约束"), _reference_rows(annotation)))
    out.append(_table(("审阅项", "你的意见（可直接填写）"), [["草稿、拆分或状态是否合理", "待复核"], ["修改建议", ""]]))
    return "\n".join(out)


def _disputes(views: list[dict]) -> list[list[Any]]:
    rows = []
    for view in views:
        stream_id = view["stream"]["id"]
        for entry in view["steps"]:
            if entry["issue"]:
                step = entry["step"]
                reference = (entry.get("score") or {}).get("reference") or {}
                checks = (entry.get("score") or {}).get("checks", {})
                check_failures = {key: item.get("violations") for key, item in checks.items()
                                  if isinstance(item, dict) and item.get("passed") is False}
                if step.get("error"):
                    label, note = STATUS.get(step["status"], step["status"]), step["error"]
                elif reference.get("matched") is False:
                    label, note = "参考动作不匹配", _reference_differences(entry["score"]) or _match_text(entry["score"])
                elif check_failures:
                    label, note = "回放约束有差异", check_failures
                elif len(step.get("attempts", [])) > 1:
                    label, note = "自动反馈修正", step["attempts"][0].get("error") or "查看首次提议与最终状态。"
                else:
                    label, note = STATUS.get(step["status"], step["status"]), step.get("reason") or "本条尚未运行。"
                rows.append([stream_id, entry["event"]["id"], label, note])
        pending = (view["result"].get("final") or {}).get("open_pending", [])
        if pending:
            rows.append([stream_id, "最终树", "仍有根待判断", f"{len(pending)} 条原句尚待明确归属；请判断是否合理保留或应已迁出。"])
    return rows


def render_xml(bundle: dict, result: dict, reference: dict | None = None) -> str:
    views = _stream_views(bundle, result, reference)
    out = ["<title>阶段0｜按消息顺序长树回放（参考草稿）</title>", "<h1>如何审阅</h1>",
           _p("本轮只从项目根与一页项目地图开始，任务随消息顺序逐步出现。下面展示每条消息到达时已有的树、实际动作及前后变化；最终树只出现在后面的结果区，不会反向作为早期输入。"),
           _p("参考标注仍是草稿。动作约束一致不等于任务划分正确；请重点判断每个子任务是否有独立交付结果和停止条件、等待与完成是否有消息依据、根待判断是否应迁移。"),
           _p("真实消息与合成补充各自回放、分别统计。所有任务变化仅存在于离线文件；没有修改当前飞书任务或发送消息。"),
           "<h1>起步项目地图</h1>", _p(bundle.get("map", "未提供")),
           "<h1>运行概览</h1>", _table(("消息线", "类型", "消息数", "已应用", "重复", "拒绝", "错误", "动作与期次一致"), _overview_rows(views))]
    notes = result.get("review_notes", [])
    if notes:
        out[1:1] = ["<h1>本轮结论与重点复核</h1>"] + [_p(note) for note in notes]
    comparison = _comparison_rows(result)
    if comparison:
        out.extend(["<h1>首跑与本轮的简短对照</h1>",
                    _table(("运行", "消息线", "已应用", "拒绝", "错误", "新增节点", "根待判断", "反馈修正事件"), comparison),
                    _p("本轮在单条提议被校验拒绝后，最多允许模型依据反馈修正一次；首次拒绝与最终状态均保留。两次模型运行存在波动，本表用于解释本次流程差异，不推断总体质量提升。")])
    for view in views:
        stream = view["stream"]
        out.append("<h1>" + _e(stream["id"] + (" · 真实消息顺序" if stream["kind"] == "real" else " · 合成补充场景")) + "</h1>")
        for entry in view["steps"]:
            out.append(_step_xml(stream["id"], entry))
    out.append("<h1>各消息线的最终树</h1>")
    for view in views:
        final = view["result"].get("final")
        out.extend(["<h2>" + _e(view["stream"]["id"]) + "</h2>", _p("\n".join(tree_lines(final, contracts=True))),
                    _p("仍待判断的消息"), _table(("源消息", "原句", "目标", "理由"), _pending_rows(final))])
    out.extend(["<h1>争点与复核清单</h1>",
                _table(("消息线", "事件", "复核入口", "说明"), _disputes(views)),
                _p("拒绝或重复可能正是故障样例的预期结果；本表提供复核入口，不直接认定业务错误。即使动作约束完全匹配，任务树的业务语义仍需人工核对。")])
    for note in (result.get("score") or {}).get("needs_human_review", []):
        out.append(_p(note))
    return "\n".join(out)


def render_html(bundle: dict, result: dict, reference: dict | None = None) -> str:
    views = _stream_views(bundle, result, reference)
    cards = []
    for view in views:
        stream_id = view["stream"]["id"]
        cards.append('<h2 class="stream">' + _e(stream_id) + (" · 真实消息顺序" if view["stream"]["kind"] == "real" else " · 合成补充场景") + '</h2>')
        for entry in view["steps"]:
            event, step = entry["event"], entry["step"]
            before = _e("\n".join(tree_lines(step.get("before"))))
            after = _e("\n".join(tree_lines(step.get("after"))))
            before_pending = _table(("当时待判断源消息", "原句", "目标", "理由"), _pending_rows(step.get("before"))) if _pending_rows(step.get("before")) else ""
            context = "".join('<p>同话题此前 ' + _e(item.get("id")) + '：' + _e(item.get("text")) + '</p>'
                              for item in (step.get("input") or {}).get("same_thread_before", []))
            context += "".join('<p>' + _e(item.get("quote")) + '</p>' for item in event.get("context", []))
            actions = []
            for title, headers, rows in _action_sections(step):
                if rows:
                    actions.append('<h3>实际' + title + '</h3><div class="scroll">' + _table(headers, rows) + '</div>')
                    if title == "创建":
                        for task in (step.get("applied") or {}).get("created", []):
                            actions.append(_p("创建 " + _s(task.get("id")) + " 的原句依据：" + _s(task.get("quote")) + "；理由：" + _s(task.get("why"))))
            if not actions:
                actions.append('<p class="muted">没有新增已应用动作。</p>')
            annotation = entry["reference"]
            note = _s(annotation.get("note")) if annotation else "尚无参考草稿标注。"
            constraints = '<details><summary>参考动作约束</summary><div class="scroll">' + _table(("方案", "必做", "不应做", "业务期"), _reference_rows(annotation)) + '</div></details>' if annotation else ''
            diff = _table(("任务", "字段", "之前", "之后"), _diff_rows(step)) if _diff_rows(step) else '<p class="muted">节点字段无变化。</p>'
            score_text = _match_text(entry["score"])
            comparison_notes = "".join(_p(item) for item in _reference_differences(entry["score"]))
            check_rows = _check_rows(entry["score"])
            checks_html = '<details><summary>回放约束检查</summary><div class="scroll">' + _table(("项目", "结果", "差异"), check_rows) + '</div></details>' if check_rows else ''
            error = '<p class="error">' + _e(step.get("error")) + '</p>' if step.get("error") else ''
            reason = '<p>' + _e(step["reason"]) + '</p>' if step.get("reason") else ''
            attempts = _attempt_rows(step)
            if attempts:
                reason += '<p>本条触发一次自动反馈修正；首次被拒绝的提议没有改变树。</p><div class="scroll">' + _table(("提议次数", "结果", "错误与反馈"), attempts) + '</div>'
            pending = _table(("源消息", "原句", "目标", "理由"), _pending_rows(step.get("after")))
            cards.append('<article class="step" data-issue="' + ('true' if entry["issue"] else 'false') + '" data-stream="' + _e(stream_id) + '" data-event="' + _e(event["id"]) + '"><div class="step-top"><h2>' + _e(event["id"]) + '</h2><span class="status ' + ('issue' if entry["issue"] else '') + '">' + _e(STATUS.get(step["status"], step["status"])) + '</span></div><p class="meta">' + _e(event.get("time")) + ' · 来源 ' + _e(event.get("source", "文件内消息")) + '</p><details class="before"><summary>① 消息到达时已有树</summary><pre>' + before + '</pre>' + before_pending + '</details><div class="inbound"><div class="label">② 输入</div><blockquote>' + _e(event["text"]) + '</blockquote>' + ('<details><summary>当时可见背景</summary>' + context + '</details>' if context else '') + '</div><div class="actions"><div class="label">③ 实际处理</div>' + reason + error + ''.join(actions) + '</div><div class="changes"><div class="label">④ 状态与承诺差异</div><div class="scroll">' + diff + '</div><details><summary>处理后的树与根待判断</summary><pre>' + after + '</pre><div class="scroll">' + pending + '</div></details></div><div class="reference"><div class="label">参考草稿与审阅</div><p>' + _e(note) + '</p><p class="match">' + _e(score_text) + '</p>' + comparison_notes + checks_html + constraints + '<textarea class="review-note" placeholder="你的意见：草稿是否合理、应如何拆分或改状态……"></textarea></div></article>')
    final_sections = []
    for view in views:
        final = view["result"].get("final")
        final_sections.append('<section class="final-tree"><h3>' + _e(view["stream"]["id"]) + '</h3><pre>' + _e("\n".join(tree_lines(final, contracts=True))) + '</pre><div class="scroll">' + _table(("源消息", "原句", "目标", "理由"), _pending_rows(final)) + '</div></section>')
    css = '''body{margin:0;background:#f2f5f8;color:#233345;font:16px/1.65 system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:30px}h1{font-size:30px;margin:0 0 14px}h2{font-size:22px}h3{font-size:16px;margin:16px 0 6px}p{margin:8px 0}.intro,.step,.final-tree,.disputes{background:#fff;border:1px solid #dae2ec;border-radius:12px;padding:22px;margin:20px 0}.step-top{display:flex;align-items:center;gap:14px}.step-top h2{margin:0}.status{padding:3px 10px;border-radius:15px;background:#e7f1ec;font-size:13px}.status.issue{background:#fff0da;color:#875214}.meta,.muted{color:#68788b;font-size:14px}.label{font-weight:700;color:#315476;margin:16px 0 8px}blockquote{margin:0;border-left:4px solid #407cc3;padding:12px 16px;background:#eef5ff;white-space:pre-wrap}.reference{background:#fffaf0;border-radius:8px;padding:4px 15px 14px;margin-top:18px}.reference .label{color:#86612a}.error{color:#9d392c;background:#fff0ed;padding:10px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:8px;border:1px solid #dce3ec;vertical-align:top;text-align:left}td p{margin:0;white-space:pre-wrap}th{background:#eff3f7}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 ui-monospace,monospace;background:#f6f8fb;padding:14px;border-radius:8px}details{margin:12px 0}summary{cursor:pointer;color:#356b9c}.toolbar{position:sticky;top:0;background:#f2f5f8;padding:9px 0;z-index:5;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar input[type=search]{flex:1;min-width:200px;padding:10px;border:1px solid #bac7d7;border-radius:6px;font:inherit}.toolbar button{border:0;background:#2461b2;color:white;border-radius:6px;padding:11px;font:inherit;cursor:pointer}.review-note{width:100%;box-sizing:border-box;font:inherit;min-height:80px;padding:9px;border:1px solid #d8c9aa;border-radius:6px}.stream{padding-top:12px}.nav a{margin-right:18px;color:#285b99}#message{font-size:13px;color:#51677f}'''
    js = '''const search=document.getElementById('search'),only=document.getElementById('issues');function filter(){const q=search.value.toLowerCase();document.querySelectorAll('.step').forEach(s=>{s.hidden=(!s.textContent.toLowerCase().includes(q))||(only.checked&&s.dataset.issue!=='true');});}search.addEventListener('input',filter);only.addEventListener('change',filter);document.getElementById('save').addEventListener('click',()=>{let notes=[...document.querySelectorAll('.step')].map(s=>({stream_id:s.dataset.stream,event_id:s.dataset.event,note:s.querySelector('.review-note').value.trim()})).filter(x=>x.note);let payload={schema:1,status:'human-review-notes',notes};let blob=new Blob([JSON.stringify(payload,null,2)+String.fromCharCode(10)],{type:'application/json'});let link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='growth-review-notes.json';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);document.getElementById('message').textContent='已导出 '+notes.length+' 条意见；请保存文件。';});'''
    summary = _table(("消息线", "类型", "消息数", "已应用", "重复", "拒绝", "错误", "动作与期次一致"), _overview_rows(views))
    comparison = _comparison_rows(result)
    if comparison:
        summary += '<h3>首跑与本轮的简短对照</h3>' + _table(("运行", "消息线", "已应用", "拒绝", "错误", "新增节点", "根待判断", "反馈修正事件"), comparison)
        summary += '<p>本轮对被拒绝提议最多自动反馈修正一次，首次错误仍可查看。两次模型运行存在波动；此表不证明总体质量提升。</p>'
    conclusion = ('<h2>本轮结论与重点复核</h2>' + "".join(_p(note) for note in result.get("review_notes", []))) if result.get("review_notes") else ''
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; connect-src \'none\'; base-uri \'none\'; form-action \'none\'"><title>按消息顺序长树回放</title><style>' + css + '</style><main><h1>按消息顺序长树回放</h1><div class="intro">' + conclusion + '<p>起点只有项目根与一页地图。逐条看“当时已有树 → 输入 → 实际处理 → 状态差异”，再判断树应如何长出来。</p><p>参考标注是草稿；动作约束匹配不能代替业务判断。真实消息与合成补充独立统计。所有变化仅发生于离线文件。</p><p class="nav"><a href="#events">顺序回放</a><a href="#final">最终树</a><a href="#disputes">争点</a></p><details><summary>起步的一页项目地图</summary>' + _p(bundle.get("map", "未提供")) + '</details><div class="scroll">' + summary + '</div><p class="muted">可在每条下方填写意见，点击导出保存。页面没有联网请求；关闭前请先保存意见。</p></div><div class="toolbar"><input type="search" id="search" placeholder="搜索消息、动作、节点或参考说明"><label><input type="checkbox" id="issues">仅看复核入口</label><button type="button" id="save">导出审阅意见</button><span id="message"></span></div><div id="events">' + ''.join(cards) + '</div><h2 id="final">最终树</h2>' + ''.join(final_sections) + '<section class="disputes" id="disputes"><h2>争点与复核入口</h2><div class="scroll">' + _table(("消息线", "事件", "入口", "说明"), _disputes(views)) + '</div><p>拒绝或重复可能是故障样例的预期结果。还需人工判断：子任务能否独立交付与停止；等待和完成是否有依据；待判断消息是否已在信息充分后迁移。</p></section></main><script>' + js + '</script></html>'


render_growth_html = render_html
render_growth_xml = render_xml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--reference")
    parser.add_argument("--score", help="Optional scoring report; attached for rendering only")
    parser.add_argument("--baseline", help="Optional first run, used only for a short review comparison")
    parser.add_argument("--format", choices=("html", "xml"), required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    read = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    bundle, result = read(args.bundle), read(args.result)
    reference = read(args.reference) if args.reference else None
    if args.score:
        result = {**result, "score": read(args.score)}
    if args.baseline:
        result = {**result, "baseline": read(args.baseline)}
    path = Path(args.out)
    if path.exists():
        parser.error("Output exists; choose a new versioned filename")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((render_html if args.format == "html" else render_xml)(bundle, result, reference), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
