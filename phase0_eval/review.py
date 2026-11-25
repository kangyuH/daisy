"""Offline review documents and strict import of an explicitly fetched Docx XML.

This module has no Feishu, Gateway, database, subprocess or HTTP dependencies.
Publishing and fetching are deliberate, separate CLI steps outside the runner.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


HEADERS = ("样例ID", "可接受方案", "原句", "目标任务ID", "唤醒owner", "理由")
NONE = "无关"


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _text(node: ET.Element) -> str:
    """Flatten native Docx XML cells, preserving explicit line breaks."""
    parts: list[str] = []
    def walk(item: ET.Element) -> None:
        if item.text:
            parts.append(item.text)
        for child in item:
            if child.tag.rsplit("}", 1)[-1] == "br":
                parts.append("\n")
            else:
                walk(child)
            if child.tail:
                parts.append(child.tail)
    walk(node)
    return "".join(parts).strip()


def _tasks(bundle: dict) -> dict[str, dict]:
    tasks = {str(task["id"]): task for task in bundle["tasks"]}
    root = bundle.get("root")
    if isinstance(root, dict):
        tasks.setdefault(str(root["id"]), root)
    elif isinstance(root, str):
        tasks.setdefault(root, {"id": root, "title": "项目根"})
    return tasks


def _rows(case: dict, annotation: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for number, option in enumerate(annotation["options"], 1):
        if not option:
            rows.append([case["id"], str(number), "", NONE, "否", ""])
        for relation in option:
            rows.append([case["id"], str(number), relation["quote"],
                         relation["target"], "是" if relation["wake"] else "否", ""])
    if rows:
        rows[0][-1] = str(annotation.get("reason", ""))
    return rows


def _table(rows: list[list[str]], headers: tuple[str, ...] = HEADERS) -> str:
    header = "".join("<th>" + _escape(value) + "</th>" for value in headers)
    body = "".join("<tr>" + "".join("<td><p>" + _escape(value).replace("\n", "<br/>")
                         + "</p></td>" for value in row) + "</tr>" for row in rows)
    return "<table><thead><tr>" + header + "</tr></thead><tbody>" + body + "</tbody></table>"


def _report_cases(report: dict | None) -> dict[str, dict]:
    return {item["case_id"]: item for item in (report or {}).get("cases", [])}


def _engine_result(engine: str, result: dict, tasks: dict[str, dict]) -> str:
    prediction = result.get("prediction", {})
    outcome = result.get("outcome", {})
    relations = prediction.get("relations", [])
    relations_text = "；".join(
        str(item.get("target", "?")) + "（" + str(tasks.get(str(item.get("target")), {}).get("title", ""))
        + ("，不表示唤醒决策" if engine == "legacy" or item.get("wake") is None
           else "，唤醒" if item.get("wake") else "，留证") + "）" for item in relations
    ) or "无关联"
    exact = outcome.get("exact_targets")
    score = "与草稿目标集合一致" if exact is True else "与草稿目标集合有差异" if exact is False else "未判定"
    error = prediction.get("error") or outcome.get("error")
    if error:
        score = "运行错误：" + str(error)
    projection = "新建提议在评测中映射项目根，不表示已经新建任务；" if engine == "legacy" and prediction.get("decision") == "create" else ""
    return f"{engine}：{projection}{relations_text}；{score}。{prediction.get('reason', '')}"


def _summary_rows(report: dict) -> list[list[str]]:
    rows = []
    for engine, summary in report.get("summary", {}).items():
        real = summary.get("real", {})
        dev, holdout = real.get("dev", {}), real.get("holdout", {})
        total = lambda key: int(dev.get(key, 0)) + int(holdout.get(key, 0))
        wake = "不支持，未评分" if engine == "legacy" else f"多唤醒 {total('wake_extra')}；漏唤醒 {total('wake_missed')}"
        rows.append([engine, f"{total('exact_targets')}/{total('cases')}",
                     f"{holdout.get('exact_targets', 0)}/{holdout.get('cases', 0)}",
                     str(total("missed_targets")), str(total("extra_targets")), wake])
    return rows


def render_xml(bundle: dict, labels: dict, report: dict | None = None) -> str:
    """Render editable native tables. Never emits mentions or task resource blocks."""
    tasks = _tasks(bundle)
    results = _report_cases(report)
    out = ["<title>阶段0｜吉家双期离线路由评估</title>",
           "<h1>审阅说明</h1>",
           "<p>这是依据真实沟通整理的参考标注草稿，首轮分数只表示模型与草稿的一致程度。请先审阅任务边界和标注，再解释模型结果。</p>",
           "<p>修改各样例表中的目标任务ID、唤醒owner、原句及理由即可。一个方案可有多行，表示同时挂到多个任务；另一种也可接受的处理使用另一个方案编号。完全无关联时保留一行目标为“无关”，原句留空、唤醒填“否”。</p>",
           "<p>请保留样例ID、表头及方案编号。添加目标可复制一行。原文、背景和任务快照用于回放，请勿改动。若需改任务树或上下文，请在理由中说明，由后续修订数据包处理。</p>",
           "<p>审阅文档的读取与回收是单独步骤；评估只读取导出的文件，不连接飞书或现有任务系统。</p>",
           "<h1>任务树与稳定编号</h1>"]
    out.append(_table([[key, str(task.get("parent", "")), str(task.get("title", "")),
                        str(task.get("month", "")), str(task.get("objective", ""))]
                       for key, task in tasks.items()], ("任务ID", "父任务ID", "名称", "业务月", "目标")))
    for key, task in tasks.items():
        out.append("<p><b>" + _escape(key) + "</b> 范围：" + _escape(task.get("scope", "未提供"))
                   + "；验收：" + _escape(task.get("acceptance", "未提供"))
                   + "；停止条件：" + _escape(task.get("stop", "未提供")) + "</p>")
    if report:
        out.extend(["<h1>首轮结果</h1>",
                    "<p>下表只统计真实消息，与当前参考标注草稿比较；合成补充样例单列在离线指标中。多目标路由减少了多目标漏挂，但仍有额外挂载与不必要唤醒，暂不能据此采纳自动分发。</p>",
                    _table(_summary_rows(report), ("路由", "真实样例目标集合一致", "留出集一致", "多目标样例漏挂目标数", "额外目标数", "唤醒差异")),
                    "<p>legacy 保留现有 create / followup / noop 语义，不支持 owner 唤醒决策；新建提议映射项目根仅用于比较，未真正创建任务。multi 为最小多目标路由。所有结果仅落离线文件。</p>"])
    out.append("<h1>逐条标注与结果</h1>")
    for case in bundle["cases"]:
        case_id = case["id"]
        annotation = labels["cases"][case_id]
        out.append("<h2>" + _escape(case_id + " · " + str(case.get("kind", ""))) + "</h2>")
        out.append("<p>分组：" + _escape(case.get("group", "")) + "；标签：" + _escape(", ".join(case.get("tags", []))) + "</p>")
        out.append("<p><b>入站原文：</b>" + _escape(case["text"]).replace("\n", "<br/>") + "</p>")
        for context in case.get("context", []):
            out.append("<p>背景 " + _escape(context.get("id", "")) + "：" + _escape(context.get("text", "")).replace("\n", "<br/>") + "</p>")
        out.append(_table(_rows(case, annotation)))
        for engine, result in results.get(case_id, {}).get("engines", {}).items():
            out.append("<p>" + _escape(_engine_result(engine, result, tasks)) + "</p>")
    return "\n".join(out)


def render_html(bundle: dict, labels: dict, report: dict | None = None) -> str:
    """Self-contained file: readable report, editable labels, JSON export; zero network."""
    tasks = _tasks(bundle)
    results = _report_cases(report)
    task_options = '<option value="">无关</option>' + "".join(
        '<option value="' + _escape(key) + '">' + _escape(key + " · " + str(task.get("title", ""))) + "</option>"
        for key, task in tasks.items())
    sections: list[str] = []
    for case in bundle["cases"]:
        case_id = case["id"]
        annotation = labels["cases"][case_id]
        rows = []
        for row in _rows(case, annotation):
            _, number, quote, target, wake, _ = row
            options = task_options.replace('value="' + _escape(target if target != NONE else "") + '"',
                                           'value="' + _escape(target if target != NONE else "") + '" selected', 1)
            rows.append('<tr><td><input class="option" type="number" min="1" value="' + number + '"></td><td><textarea class="quote">' + _escape(quote) + '</textarea></td><td><select class="target">' + options + '</select></td><td><select class="wake"><option value="false"' + (" selected" if wake == "否" else "") + '>留证</option><option value="true"' + (" selected" if wake == "是" else "") + '>唤醒</option></select></td><td><button class="remove" type="button">删行</button></td></tr>')
        result_html = "".join('<p class="result">' + _escape(_engine_result(engine, result, tasks)) + '</p>'
                              for engine, result in results.get(case_id, {}).get("engines", {}).items())
        context_html = "".join('<p><small>' + _escape(context.get("id", "")) + '</small> ' + _escape(context.get("text", "")) + '</p>' for context in case.get("context", []))
        sections.append('<section class="case" data-case="' + _escape(case_id) + '"><h2>' + _escape(case_id) + '</h2><p class="meta">' + _escape(str(case.get("kind", "")) + " · " + str(case.get("group", "")) + " · " + ", ".join(case.get("tags", []))) + '</p><blockquote>' + _escape(case["text"]) + '</blockquote><details><summary>当时可见背景</summary>' + context_html + '</details><div class="scroll"><table><thead><tr><th>方案</th><th>原句</th><th>目标任务</th><th>动作</th><th></th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div><button class="add" type="button">增加目标或替代方案</button><p>标注理由</p><textarea class="reason">' + _escape(annotation.get("reason", "")) + '</textarea>' + result_html + '</section>')
    tree_rows = "".join('<tr><td>' + _escape(key) + '</td><td>' + _escape(task.get("parent", ""))
                        + '</td><td><details><summary>' + _escape(task.get("title", ""))
                        + '</summary><p>目标：' + _escape(task.get("objective", "未提供"))
                        + '</p><p>范围：' + _escape(task.get("scope", "未提供"))
                        + '</p><p>验收：' + _escape(task.get("acceptance", "未提供"))
                        + '</p><p>停止：' + _escape(task.get("stop", "未提供"))
                        + '</p></details></td><td>' + _escape(task.get("month", "")) + '</td></tr>' for key, task in tasks.items())
    embedded = json.dumps({"bundle": bundle, "labels": labels}, ensure_ascii=False).replace("<", "\\u003c")
    summary = (_table(_summary_rows(report), ("路由", "真实样例目标集合一致", "留出集一致", "多目标样例漏挂目标数", "额外目标数", "唤醒差异"))
               + '<p>以上只统计真实消息，并与当前草稿比较；合成补充样例单列。legacy 的新建提议仅映射项目根比较，不表示实际建任务或做出唤醒决策。</p><details><summary>完整分组指标</summary><pre>'
               + _escape(json.dumps(report.get("summary", {}), ensure_ascii=False, indent=2)) + '</pre></details>') if report else '<p>尚未运行模型。</p>'
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src data:; base-uri 'none'; form-action 'none'"><title>阶段0 · 离线路由评估</title><style>
body{font:16px/1.65 system-ui,sans-serif;margin:0;background:#f3f5f8;color:#243144}main{max-width:1160px;margin:auto;padding:28px}h1{font-size:30px}h2{font-size:21px;margin:0}section,.intro{background:white;border:1px solid #dce2e9;border-radius:12px;padding:24px;margin:20px 0}.meta,small{color:#69788b}blockquote{margin:18px 0;background:#eef5ff;border-left:4px solid #497de2;padding:14px;white-space:pre-wrap}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #dce2e9;padding:8px;text-align:left;vertical-align:top}th{background:#eff3f8}textarea,input,select{font:inherit;box-sizing:border-box;max-width:100%}textarea{width:100%;min-height:68px;padding:8px;border:1px solid #c8d1dc;border-radius:5px}.quote{min-width:180px}.option{width:60px}.target{min-width:170px}button{cursor:pointer;background:#245fc5;color:white;border:0;border-radius:6px;padding:8px 13px;margin:8px 3px 8px 0}.remove{background:#6c7888}.reason{min-height:80px}.scroll{overflow:auto}pre{white-space:pre-wrap;font-size:13px}.result{padding:12px;background:#f5f7fb}#search{width:100%;padding:11px;border:1px solid #c8d1dc;border-radius:6px}.toolbar{position:sticky;top:0;background:#f3f5f8;padding:10px 0;z-index:2}.notice{color:#85521a}
</style><main><h1>阶段0 · 离线路由评估</h1><div class="intro"><p class="notice">参考标注草稿：首轮分数表示模型与草稿的一致程度，尚不是人工金标准确率。</p><p>先审阅任务树，再逐条修改目标、原句和动作。同一方案的多行同时生效；另一种可接受的处理使用另一方案编号。完全无关联时保留一行“无关”，原句留空、动作选“留证”。</p><p>本文件可离线使用。修改只存在当前页面中；完成后点击“导出修订标签”保存JSON文件，交给后续文件回放。原文与上下文不会因标签导出而修改。</p><details><summary>任务树与稳定编号</summary><div class="scroll"><table><tr><th>ID</th><th>父任务</th><th>名称</th><th>业务月</th></tr>''' + tree_rows + '</table></div></details>' + summary + '</div><div class="toolbar"><input id="search" placeholder="按样例、原文或标签筛选"><button id="export" type="button">导出修订标签</button><span id="message"></span></div>' + "".join(sections) + '</main><script id="dataset" type="application/json">' + embedded + '''</script><script>
const data=JSON.parse(document.getElementById('dataset').textContent);
document.getElementById('search').addEventListener('input',e=>{let q=e.target.value.toLowerCase();document.querySelectorAll('.case').forEach(s=>s.hidden=!s.textContent.toLowerCase().includes(q));});
document.querySelectorAll('.case').forEach(section=>{section.addEventListener('click',e=>{if(e.target.classList.contains('remove')){if(section.querySelectorAll('tbody tr').length>1)e.target.closest('tr').remove();}if(e.target.classList.contains('add')){let row=section.querySelector('tbody tr').cloneNode(true);section.querySelector('tbody').appendChild(row);}});});
document.getElementById('export').addEventListener('click',()=>{try{let revised=structuredClone(data.labels);revised.version=String(revised.version||'draft-v1')+'.reviewed';revised.status='reviewed';revised.provenance={...(revised.provenance||{}),review:{source:'offline-html-export',base_version:data.labels.version}};document.querySelectorAll('.case').forEach(s=>{let groups=new Map();let current=data.bundle.cases.find(c=>c.id===s.dataset.case);s.querySelectorAll('tbody tr').forEach(r=>{let number=Number(r.querySelector('.option').value);if(!Number.isInteger(number)||number<1)throw Error(current.id+'：方案编号须为正整数');let target=r.querySelector('.target').value;let quote=r.querySelector('.quote').value.trim();let wake=r.querySelector('.wake').value==='true';if(!target&&(quote||wake))throw Error(current.id+'：无关行必须清空原句并选择留证');if(target&&(!quote||!current.text.includes(quote)))throw Error(current.id+'：原句必须直接摘自入站原文');let rows=groups.get(number)||[];if(!target&&rows.length)throw Error(current.id+'：无关不能与目标混在同一方案');if(target)rows.push({quote,target,wake});groups.set(number,rows);});let options=[...groups].sort((a,b)=>a[0]-b[0]).map(p=>p[1]);if(!options.length)throw Error(current.id+'：缺少方案');revised.cases[current.id]={...revised.cases[current.id],options,reason:s.querySelector('.reason').value.trim()};});let blob=new Blob([JSON.stringify(revised,null,2)+'\n'],{type:'application/json'});let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='labels.reviewed.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);document.getElementById('message').textContent='已导出修订标签，请保存文件。';}catch(e){document.getElementById('message').textContent=e.message;}});
</script></html>'''


def import_review(bundle: dict, original_labels: dict, raw_response: dict | str) -> dict:
    """Import native review tables, failing on ambiguity or unrelated source edits."""
    raw = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
    if raw.get("ok") is not True:
        raise ValueError("Feishu fetch did not return ok=true")
    document = raw.get("data", {}).get("document", {})
    content = document.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Missing document XML content")
    try:
        root = ET.fromstring("<review>" + content + "</review>")
    except ET.ParseError as exc:
        raise ValueError("Invalid XML export; fetch using --doc-format xml") from exc
    cases = {case["id"]: case for case in bundle["cases"]}
    tasks = _tasks(bundle)
    # Source text and task snapshots are immutable replay inputs. A reviewer can
    # request changes in the rationale, but editing them here must not silently
    # change what the next model run receives.
    expected_task_rows = [[key, str(task.get("parent", "")), str(task.get("title", "")),
                           str(task.get("month", "")), str(task.get("objective", ""))]
                          for key, task in tasks.items()]
    task_headers = ("任务ID", "父任务ID", "名称", "业务月", "目标")
    task_tables = []
    for table in root.iter("table"):
        rows = [[_text(cell) for cell in row if cell.tag.rsplit("}", 1)[-1] in ("th", "td")]
                for row in table.iter("tr")]
        if rows and tuple(rows[0]) == task_headers:
            task_tables.append(rows[1:])
    if task_tables != [expected_task_rows]:
        raise ValueError("Task snapshot edited or missing; revise the bundle explicitly")
    originals = [_text(node)[len("入站原文："):]
                 for node in root.iter("p") if _text(node).startswith("入站原文：")]
    expected_originals = [case["text"].strip() for case in bundle["cases"]]
    if originals != expected_originals:
        raise ValueError("Source text edited or missing; revise the bundle explicitly")
    contexts = [_text(node) for node in root.iter("p") if _text(node).startswith("背景 ")]
    expected_contexts = ["背景 " + str(item.get("id", "")) + "：" + str(item.get("text", ""))
                         for case in bundle["cases"] for item in case.get("context", [])]
    if contexts != expected_contexts:
        raise ValueError("Context snapshot edited or missing; revise the bundle explicitly")
    groups: dict[str, dict[int, list[dict]]] = {}
    reasons: dict[str, list[str]] = {}
    empty_options: set[tuple[str, int]] = set()
    found_tables = 0
    for table in root.iter("table"):
        rows = [[_text(cell) for cell in row if cell.tag.rsplit("}", 1)[-1] in ("th", "td")]
                for row in table.iter("tr")]
        if not rows or tuple(rows[0]) != HEADERS:
            continue
        found_tables += 1
        for row in rows[1:]:
            if len(row) != len(HEADERS):
                raise ValueError("Review row has wrong column count")
            case_id, number_text, quote, target, wake_text, reason = row
            if case_id not in cases:
                raise ValueError("Unknown case ID: " + case_id)
            try:
                number = int(number_text)
            except ValueError as exc:
                raise ValueError(case_id + ": invalid option number") from exc
            if number < 1 or str(number) != number_text:
                raise ValueError(case_id + ": option number must be a positive integer")
            if wake_text not in ("是", "否"):
                raise ValueError(case_id + ": wake must be 是 or 否")
            option = groups.setdefault(case_id, {}).setdefault(number, [])
            key = (case_id, number)
            if target == NONE:
                if quote or wake_text != "否" or option or key in empty_options:
                    raise ValueError(case_id + ": invalid or duplicate unrelated row")
                empty_options.add(key)
            else:
                if key in empty_options:
                    raise ValueError(case_id + ": unrelated row cannot share an option with targets")
                if target not in tasks:
                    raise ValueError(case_id + ": unknown task ID: " + target)
                if target not in cases[case_id].get("candidates", list(tasks)):
                    raise ValueError(case_id + ": task is outside the frozen candidate set: " + target)
                if not quote or quote not in cases[case_id]["text"]:
                    raise ValueError(case_id + ": quote must be an exact substring of inbound text")
                relation = {"quote": quote, "target": target, "wake": wake_text == "是"}
                if relation in option:
                    raise ValueError(case_id + ": duplicate relation")
                option.append(relation)
            if reason and reason not in reasons.setdefault(case_id, []):
                reasons[case_id].append(reason)
    missing = set(cases) - set(groups)
    if not found_tables or missing:
        raise ValueError("Missing review cases: " + ", ".join(sorted(missing)))
    revised = copy.deepcopy(original_labels)
    revised["version"] = str(original_labels.get("version", "draft-v1")) + ".reviewed-r" + str(document.get("revision_id", "unknown"))
    revised["status"] = "reviewed"
    revised["provenance"] = {**(original_labels.get("provenance") or {}), "review": {
        "source": "feishu-xml-import", "base_version": original_labels.get("version"),
        "document_id": document.get("document_id"), "revision_id": document.get("revision_id"),
        "xml_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}}
    for case_id, options in groups.items():
        if not reasons.get(case_id):
            raise ValueError(case_id + ": a nonempty rationale is required")
        revised["cases"][case_id] = {**revised["cases"][case_id],
                                     "options": [option for _, option in sorted(options.items())],
                                     "reason": "；".join(reasons.get(case_id, []))}
    return revised


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--bundle", required=True)
    render.add_argument("--labels", required=True)
    render.add_argument("--report")
    render.add_argument("--format", choices=("html", "xml"), required=True)
    render.add_argument("--out", required=True)
    importer = subparsers.add_parser("import-xml")
    importer.add_argument("--bundle", required=True)
    importer.add_argument("--labels", required=True)
    importer.add_argument("--response", required=True, help="Saved docs +fetch JSON response")
    importer.add_argument("--out", required=True)
    args = parser.parse_args()
    def read(path: str) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    bundle, labels = read(args.bundle), read(args.labels)
    output = Path(args.out)
    if output.exists():
        parser.error("Output already exists; choose a new versioned filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "render":
        report = read(args.report) if args.report else None
        rendered = render_html(bundle, labels, report) if args.format == "html" else render_xml(bundle, labels, report)
        output.write_text(rendered, encoding="utf-8")
    else:
        raw = read(args.response)
        imported = import_review(bundle, labels, raw)
        output.write_text(json.dumps(imported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(output))


if __name__ == "__main__":
    main()
