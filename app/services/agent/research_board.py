from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Heading patterns for the three research layers (order matters).
_LAYER_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "诉求澄清",
        (
            r"诉求",
            r"澄清",
            r"问题陈述",
            r"成功标准",
            r"范围",
        ),
    ),
    (
        "事实层",
        (
            r"事实",
            r"现状",
            r"已核对",
            r"证据",
        ),
    ),
    (
        "方案建议",
        (
            r"方案",
            r"建议",
            r"选项",
            r"下一步",
            r"结论",
            r"推荐",
            r"给人闸",
        ),
    ),
)

_H2_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def _score_heading(title: str, keywords: tuple[str, ...]) -> int:
    t = title.strip()
    score = 0
    for kw in keywords:
        if re.search(kw, t, re.I):
            score += 1
    return score


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading_title, body), ...] including preamble under ''."""
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return [("", text.strip())]
    out: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        pre = text[: matches[0].start()].strip()
        if pre:
            out.append(("", pre))
    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        out.append((title, body))
    return out


def _pick_layer(
    sections: list[tuple[str, str]], keywords: tuple[str, ...]
) -> str:
    best_score = 0
    best_body = ""
    for title, body in sections:
        if not title:
            continue
        score = _score_heading(title, keywords)
        if score > best_score and body:
            best_score = score
            best_body = body
    return best_body


def _truncate(text: str, limit: int) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 20].rstrip() + "\n\n…(truncated)"


def extract_research_layers(
    text: str,
    *,
    per_layer_limit: int = 1800,
) -> dict[str, str]:
    """
    Heuristically pull 诉求澄清 / 事实层 / 方案建议 from research.md.

    Falls back to preamble / first chunk / last chunk when headings are missing.
    """
    raw = (text or "").strip()
    empty = {"诉求澄清": "", "事实层": "", "方案建议": ""}
    if not raw:
        return empty

    sections = _split_sections(raw)
    layers: dict[str, str] = {}
    for name, kws in _LAYER_SPECS:
        layers[name] = _pick_layer(sections, kws)

    # Fallbacks when headings did not match.
    bodies = [b for _, b in sections if b]
    if not layers["诉求澄清"]:
        # preamble or first section
        if sections and sections[0][0] == "" and sections[0][1]:
            layers["诉求澄清"] = sections[0][1]
        elif bodies:
            layers["诉求澄清"] = bodies[0]
    if not layers["事实层"]:
        if len(bodies) >= 2:
            layers["事实层"] = bodies[1]
        elif bodies:
            layers["事实层"] = bodies[0]
    if not layers["方案建议"]:
        if len(bodies) >= 3:
            layers["方案建议"] = bodies[-1]
        elif bodies:
            layers["方案建议"] = bodies[-1]

    return {k: _truncate(v, per_layer_limit) for k, v in layers.items()}


def format_clues_followup(
    *,
    project_id: Optional[str],
    add_dirs: list[str],
    brief: str,
) -> str:
    pid = (project_id or "").strip() or "(未绑定)"
    lines = [
        "## 已加载项目/仓库线索",
        f"- project_id: `{pid}`",
    ]
    if add_dirs:
        lines.append("- repos (`--add-dir`):")
        for p in add_dirs:
            lines.append(f"  - `{p}`")
    else:
        lines.append("- repos: **无可用本地路径**（调研置信度应偏低）")
    brief_s = (brief or "").strip()
    if brief_s:
        lines.extend(["", "### 线索摘要", _truncate(brief_s, 1200)])
    return "\n".join(lines)


def format_research_summary_followup(
    *,
    research_path: Path,
    returncode: int,
    missing: bool = False,
) -> str:
    if missing:
        return (
            "## 调研成果摘要\n\n"
            f"本轮 **未产出** `research.md`（CLI returncode={returncode}）。\n"
            f"请查看工作区 `scratch/runs/`，或在台账发 `/research <指令>`。\n"
            "（Gateway 硬同步：不依赖模型自行 followup。）"
        )

    try:
        text = research_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return (
            "## 调研成果摘要\n\n"
            f"读取 `research.md` 失败：{exc}\n"
            f"path=`{research_path}` returncode={returncode}"
        )

    layers = extract_research_layers(text)
    lines = [
        "## 调研成果摘要（Gateway 从 research.md 同步）",
        "",
        "### 1. 诉求澄清",
        layers["诉求澄清"] or "_（未能从 research.md 解析出该层）_",
        "",
        "### 2. 事实层",
        layers["事实层"] or "_（未能从 research.md 解析出该层）_",
        "",
        "### 3. 方案建议",
        layers["方案建议"] or "_（未能从 research.md 解析出该层）_",
        "",
        f"全文：`{research_path}` · CLI returncode={returncode}",
    ]
    return "\n".join(lines)
