---
name: cursor-cli-headless
description: >-
  Headless Cursor CLI for lark-superbot Researcher/Executor: /research /plan
  /exec /stop, cursorcli, headless, resume session, 台账续跑. Gateway auto-research
  worker and ledger slash commands share the same argv / process-group conventions.
---

# Cursor CLI Headless（lark-superbot）

本 skill 描述 Gateway 如何用本机 `agent` CLI 推进任务。实现代码在仓库：

- `app/services/agent/cli_runner.py` — argv、spawn 进程组、stream-json 取 session
- `app/services/agent/runtime.py` — mode、workspace、claim、followup / board-message
- `app/services/agent/prompts.py` — 四模式提示词
- `app/services/agent/constants.py` — `auto_research` / `research` / `plan` / `exec`
- `app/api/agent.py` — `/agent/ledger`（当场 spawn）与 `/agent/run`（worker）
- `workers/agent/` — `WORKER_IMPL=agent` 串行消费 `queue=agent`（仅 auto_research）

## Only-session（Gateway）

每个 task 在**飞书台账交互**上只绑定一个 `agent_session_id`：自动调研与后续台账口令共用；有 session 则一律 `--resume`。

同时只允许一个 run：`TaskStore.try_claim_agent_run`（`agent_run_id`）原子占锁；spawn 失败 / finish / `/stop` 必须 `release_agent_run`。

IDE / 其它飞书会话与之平行；Gateway **不会**改用 IDE session。

## 四种模式（显式 `mode`，不是 trigger）

| mode | 含义 | 需求从哪来 | Gateway 硬同步 |
|------|------|------------|----------------|
| **auto_research** | research 权限 + 固定写三层 `research.md` | 创建任务后自动 | 启动 clues；结束抽三层 followup |
| **research** | 仅权限 | 口令后正文（必填） | **不**捞 `research.md`；生命周期走 board-message |
| **plan** | Plan 模式权限 | 口令后正文（必填） | 本轮 CreatePlan / 本轮更新的 `plan.md` → 磁盘 + 台账副本 |
| **exec** | 执行权限 | 口令后正文（必填） | **不**捞 md；生命周期走 board-message |

`source` / `trigger` 只作审计字段，不决定行为。`POST /agent/run` 必填 `mode`。

口令后无正文 → **拒绝**并 board-message 贴台账。未知口令同理。

对人确认/问答：`POST /tasks/{id}/board-messages`（必填稳定 `idempotency_key`）。记进展才用 followups。

每轮日志：`scratch/runs/{run_id}_{mode}.jsonl`（覆盖写，不追加）。

## 口令语义（CLI）

| 口令 | CLI | 允许 | 禁止 |
|------|-----|------|------|
| `/research <指令>` | 不要 `--mode plan`；有 session 则 `--resume` | 按指令调研/回答 | 改业务仓、副作用；**禁止遍历 Disco tag** |
| `/plan <指令>` | `--mode plan` | 按指令产出/调整计划 | 执行交付 |
| `/exec <指令>` | 需要时 `--force` | 按 `plan.md` + 指令执行 | 无 `plan.md` 且门禁开启时拒绝 |
| `/stop` | 杀 `agent_pgid` + release claim | — | 不要用 `agent persist` |

## 两条拉起路径

1. **auto_research**：入队 `queue=agent`；payload `mode=auto_research`；台账根帖 Typing（默认 CLI app）。
2. **台账口令**：`POST /agent/ledger` 当场 spawn；口令消息 Typing（默认 app，不带 `--profile`）。

## Typing

台账侧一律默认 CLI app（`--as bot`、省略 `--profile`）。业务群队列 Typing 仍可用业务 bot。
