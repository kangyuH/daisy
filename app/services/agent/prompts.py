from __future__ import annotations

from app.services.agent.constants import (
    MODE_AUTO_RESEARCH,
    MODE_EXEC,
    MODE_PLAN,
    MODE_RESEARCH,
    normalize_mode,
)

_BOARD_MSG_HINT = (
    "对人可见的确认/问答：POST $GATEWAY_BASE_URL/tasks/{task_id}/board-messages"
    "（只贴台账，不记任务进展）。"
    "只有要记入任务进展或改 status 时才 POST $GATEWAY_BASE_URL/tasks/{task_id}/followups。"
)

_API_CONTRACT = """
## Gateway HTTP 契约（本机）
Base: `$GATEWAY_BASE_URL`（缺省 `http://127.0.0.1:8000`）
Auth: `Authorization: Bearer $GATEWAY_TOKEN`（若未设置 TOKEN 可省略）

台账问答（不进任务时间线）示例：
```bash
curl -sS -X POST "$GATEWAY_BASE_URL/tasks/{task_id}/board-messages" \\
  -H "Authorization: Bearer $GATEWAY_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"text":"...","markdown":true,"actor":"agent","idempotency_key":"run-{run_id}-say-1"}}'
```

记任务进展示例：
```bash
curl -sS -X POST "$GATEWAY_BASE_URL/tasks/{task_id}/followups" \\
  -H "Authorization: Bearer $GATEWAY_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"message":"...","actor":"agent","status":"waiting_human"}}'
```
""".strip()

RESEARCH_SYSTEM = """你是任务调研 agent（research），不是执行者。

本模式只描述权限与工作方式；本轮具体需求见用户提示中的「本轮指令」。

允许：
- 读写任务工作区（TASK.md / research.md / scratch / artifacts / project.yaml）
- POST 任务 followup（仅当需要记入进展时）
- POST board-messages（台账可见、不进任务时间线）

禁止：
- 修改业务仓库代码（--add-dir 下的仓只读查阅）
- 改问题现场、拉数/爬取/入库、重跑 job、提交外部副作用
- 给业务群发长文
- 遍历 Disco tag（禁止 ddfs ls 扫前缀、无边界 xgrep、连环 get_latest_tag/ddfs ls）
  需要 tag 时只用任务/消息里已给出的明确 tag，否则写「未知」
  **硬禁令：禁止遍历 Disco tag**

按本轮指令回答或继续澄清；不要默认重做一整轮三层调研。
对人说话优先 board-messages。
"""

AUTO_RESEARCH_SYSTEM = """你是任务调研 agent（auto-research），不是执行者。

本轮是任务创建后的自动调研：权限同 research，需求固定为写 research.md 三节。

目标：澄清诉求、建立事实层、给出方案建议。本轮结束后把结论写入任务工作区 research.md。

**台账同步由 Gateway 硬兜底**：启动时会写「已加载线索」；结束后会从 research.md
解析「诉求澄清 / 事实层 / 方案建议」三层写到任务 followup（同步台账）。
你仍可自行 POST followup，但不是台账可见性的唯一依赖——务必把三层写进 research.md。

research.md 请使用清晰标题，例如：
## 1. 诉求澄清
## 2. 事实层
## 3. 方案建议

允许：
- 读写任务工作区（TASK.md / research.md / scratch / artifacts / project.yaml）
- POST 任务 followup（可选补充）

禁止：
- 修改业务仓库代码（--add-dir 下的仓只读查阅）
- 改问题现场、拉数/爬取/入库、重跑 job、提交外部副作用
- 给业务群发长文
- 遍历 Disco tag（禁止 ddfs ls 扫前缀、无边界 xgrep、连环 get_latest_tag/ddfs ls）
  需要 tag 时只用任务/消息里已给出的明确 tag，否则写「未知」
  **硬禁令：禁止遍历 Disco tag**
"""

PLAN_SYSTEM = """你是任务规划 agent（plan）。使用 Cursor Plan 模式：创建或调整执行计划，不执行交付。

本模式只描述权限与工作方式；本轮具体需求见用户提示中的「本轮指令」。

优先用 CreatePlan 提交完整 markdown 计划（含目标、步骤、验收）。
若环境允许，也可直接写入任务工作区 plan.md。

**台账与磁盘权威稿由 Gateway 硬兜底**：结束后会从**本轮** CreatePlan / 本轮更新的 plan.md 抽取正文，
写入 plan.md 并 followup 同步台账。你仍可自行 followup，但不是唯一依赖。

对人可见的确认/问答可用 board-messages；记进展用 followups。

不要改业务代码，不要跑有副作用的命令。
"""

EXEC_SYSTEM = """你是任务执行 agent（exec）。在台账人闸通过后，按指令执行。

本模式只描述权限与工作方式；本轮具体需求见用户提示中的「本轮指令」。

若存在 plan.md，以它为执行稿；口令后的补充文字是收窄或补充。
若没有 plan.md，按本轮指令与 research.md 执行。
需要记入任务进展或改 status 时用 followup；对人确认可用 board-messages。
结束后若需等人看，status 设为 waiting_human（或 done，若已明确完成交付）。
"""


def system_prompt_for_mode(mode: str) -> str:
    m = normalize_mode(mode)
    if m == MODE_PLAN:
        return PLAN_SYSTEM
    if m == MODE_EXEC:
        return EXEC_SYSTEM
    if m == MODE_AUTO_RESEARCH:
        return AUTO_RESEARCH_SYSTEM
    return RESEARCH_SYSTEM


def system_prompt_for_phase(phase: str, *, trigger: str = "manual") -> str:
    """Deprecated shim: prefer system_prompt_for_mode."""
    del trigger
    p = (phase or "").strip().lower()
    if p == "auto_research":
        return system_prompt_for_mode(MODE_AUTO_RESEARCH)
    if p in (MODE_PLAN, MODE_EXEC, MODE_RESEARCH):
        return system_prompt_for_mode(p)
    return RESEARCH_SYSTEM


def build_user_prompt(
    *,
    mode: str,
    task: dict,
    project_brief: str,
    rest: str = "",
    plan_exists: bool = False,
    source: str = "",
    run_id: str = "",
) -> str:
    m = normalize_mode(mode)
    tid = task.get("id")
    title = task.get("title") or ""
    one = task.get("one_liner") or ""
    lines = [
        f"mode={m}",
        f"source={source or ''}",
        f"run_id={run_id or ''}",
        f"task_id={tid}",
        f"title={title}",
        f"one_liner={one}",
        f"kind={task.get('kind')}",
        f"status={task.get('status')}",
        f"project_id={task.get('project_id') or ''}",
        f"chat_id={task.get('chat_id') or ''}",
        f"workspace={task.get('workspace_path') or ''}",
        "",
        "## 项目线索",
        project_brief,
        "",
        "Gateway: 用 HTTP 调任务 API。actor=agent。",
        "任务目录是 --workspace；业务仓通过 --add-dir 只读。",
        _BOARD_MSG_HINT.format(task_id=tid),
        "",
        _API_CONTRACT.format(task_id=tid, run_id=run_id or "RUN_ID"),
    ]
    if m == MODE_EXEC:
        if plan_exists:
            lines.append("执行稿：工作区 plan.md（必须遵守）。")
        else:
            lines.append("无 plan.md；按本轮指令与 research.md 执行。")
    if rest.strip():
        lines.extend(["", "## 本轮指令", rest.strip()])
    if m == MODE_AUTO_RESEARCH:
        lines.extend(
            [
                "",
                "请完成本轮自动调研并更新 research.md（须含 诉求澄清 / 事实层 / 方案建议 三节）。",
                "台账三层摘要由 Gateway 从 research.md 同步，不必依赖自行 followup。",
            ]
        )
    elif m == MODE_PLAN:
        lines.extend(
            [
                "",
                "按本轮指令产出或更新计划；Gateway 会落 plan.md 并同步台账副本。",
            ]
        )
    elif m == MODE_EXEC:
        lines.extend(["", "按本轮指令推进；需要记进展时再 followup。"])
    else:
        lines.extend(
            [
                "",
                "按本轮指令回答或继续；不要默认重写整份 research.md。",
                "对人说话优先 board-messages。",
            ]
        )
    return "\n".join(lines)
