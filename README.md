# lark-superbot

飞书 Gateway（分层 FastAPI）：多 Bot 长连接收件 + SQLite 配置/队列 + IM HTTP 代理；独立 worker daemon 串行消费队列。

```text
app/            # Gateway：api / core / services / infra
workers/        # Daemon（调度）+ DispatcherWorker / SimpleWorker
run.py          # 启动 Gateway
run_worker.py   # 启动 daemon
```

运行时配置在 **SQLite**（`data/gateway.db`）。收件走 `lark-cli event consume`，入队只打本服务 HTTP。

## 依赖

- `lark-cli`（user 已登录，供校准/拉消息）
- `.venv` + `pip install -r requirements.txt`（建议清华源：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`）

## 启动

一键（Gateway + worker daemon，日志/pid 在 `tmp/`）：

```bash
cd /share/home/hukangyu/gitlab/lark-superbot
./start.sh
./stop.sh
```

或分进程：

```bash
source .venv/bin/activate
python run.py          # Gateway :8000
python run_worker.py   # 另开终端；串行 claim → worker
```

环境变量（worker）：

| 变量 | 含义 | 默认 |
|------|------|------|
| `GATEWAY_BASE_URL` | Gateway 地址 | `http://127.0.0.1:8000` |
| `GATEWAY_TOKEN` | 可选 Bearer | 空 |
| `WORKER_ID` | claim 标识 | `daemon-1` |
| `WORKER_IDLE_SLEEP` | 空闲等待秒 | `1` |
| `WORKER_IMPL` | `dispatcher` 或 `simple` | `dispatcher` |
| `DISPATCHER_LLM_PROVIDER` | LLM 厂商 | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek key | 必填（dispatcher） |
| `DISPATCHER_LLM_MODEL` | 模型名 | `deepseek-v4-pro` |
| `DISPATCHER_LLM_BASE_URL` | API base | `https://api.deepseek.com` |
| `DISPATCHER_AGENT_MAX_ITERATIONS` | Agent 最大工具轮次 | `6` |

## Daemon vs Worker

| 角色 | 职责 |
|------|------|
| **Daemon** | 只 `claim` → 调用 worker → 按结果 `ack/nack`；不做 @bot/@self 判断 |
| **DispatcherWorker**（默认） | LangChain tool-calling Agent：拉上下文、列开放任务、create/followup/noop；**不**回复飞书、不执行业务；强制 `finalize_dispatch` 写入 `dispatch_runs` |
| **SimpleWorker** | 调试回退：含 `bot_open_id` 才 `POST /im/respond`「出来干活」；仅 @self 则 skip |

Dispatcher 白名单 tools：`fetch_message_context`、`get_chat_project`、`list_open_tasks`、`get_task`、`create_task`、`followup_task`、`finalize_dispatch`。无 `respond`/`send`/`reply`。

## 测试

```bash
pytest -q
```

单测用临时 SQLite，`create_app(testing=True)` 不拉飞书长连接。

## 主要 API

- `POST /bots/register` · `GET /bots` · `POST /bots/{id}/chats`  
  - register 只需 `app_id`/`app_secret`；`id` 省略则数字自增（`1,2,3…`，同 app_id 复用）；`name` 省略则从校准群成员列表自动取 bot 显示名
- `POST /queue/{name}/enqueue|claim|ack|nack` · `GET /queue/{name}/stats`（返回 `item`/`items`，表名 `queue_items`；`inbound`=消息队列）
- `POST /tasks` · `GET /tasks` · `GET /tasks/{id}` · `POST /tasks/{id}/followups`（任务元信息在 SQLite，工作区在 `data/task_workspace/<id>/`；跟进 append-only）
- `POST /dispatcher/runs` · `GET /dispatcher/runs/{inbound_id}`（分发员留痕，按 inbound 幂等）
- `PUT|GET|DELETE /chat-projects/{chat_id}` · `GET /chat-projects`（群聊绑定 `knowledge/projects` 的 `project_id`；建任务未显式传 project 时继承）
- `POST /im/send` · `/im/reply` · `/im/respond` · `/im/messages/context`
- `GET /health`

### 任务约定

释义以代码为准：`app/services/tasks/models.py` 中的 `STATUS_DESCRIPTIONS` / `KIND_DESCRIPTIONS`。

**kind**（可扩展，TEXT；本期白名单）：

| kind | 含义 |
|------|------|
| `readonly` | 不涉及业务侧写操作：信息收集、查口径、查根因等；可写 scratch/`/tmp` |
| `operational` | 需要执行操作（改数、跑 job、提 PR、动配置等） |

**status**：

| status | 含义 |
|--------|------|
| `noted` | 已记下，尚未开始处理（新建默认） |
| `waiting_human` | 等人工处理或确认（含尚未动手，或 agent 已完成一轮后等人确认/决策） |
| `waiting_external` | 正在等协助方提供关键资源 |
| `agent_running` | agent 正在处理 |
| `blocked` | 阻塞（依赖未满足或条件不成立，暂无法推进） |
| `done` | 完成且已反馈 |
| `cancelled` | 任务取消 |

终态 `done`/`cancelled` 后仍可追加 note，不可再改 status（409）。  
`bot_id` 可选：显式传入优先；未传但有 `chat_id` 且该群在 `bot_chats` 有绑定则继承。  
**台账同步**：创建/跟进后用 lark-cli **默认 app 的 bot 身份**（本机即 Daisy，`--as bot` 且不带 `--profile`）在校准群发根消息并开 thread；正文走 `--markdown`。开 thread 的那条消息会 `@` lark-cli 登录用户（`auth status` 的 user open_id），根消息不 @。字段为 `board_message_id` / `board_thread_id`。飞书失败不阻断落库，记 `board_sync_error`，后续 followup 会尝试补建。与任务业务 `bot_id` 无关，无需把 Daisy 注册进 Gateway `bots` 表。
环境变量：`GATEWAY_TASK_WORKSPACE`（默认 `data/task_workspace`）。

`/im/respond`：`{"inbound_id", "text", "mention_open_ids?"}` — 按入库 payload 的 `thread_id` 自动话题/引用回复；`sender_open_id` 置顶去重。底层 `/im/reply` 仍保留供排障。

校准群：`CALIBRATION_CHAT_ID`（默认见 `.env`），不可作业务绑群。
