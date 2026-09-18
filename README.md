# lark-superbot

这是一个跑在你自己机器上的**飞书任务入口**：别人在业务群里 `@` 你的机器人（或 `@` 你），本服务会听进去，用大模型判断「这是新事项、还是在跟某条老任务、还是无关」，然后把结论记成任务，并同步到你指定的**台账群**（每条任务一条消息，跟进写在话题里，方便点开看）。

注意：默认只负责**分发和记账**，不会自动去改数据、跑任务、或在业务群里长篇回复。真要动手，可以你自己做，或再接别的 agent。

长期愿景（飞书闭环、决策分流、允许清单内自动执行）与分阶段路线见 [docs/blueprint.md](docs/blueprint.md)。当前实现仍以下文为准。

## 能做什么

| 能力 | 说明 |
|------|------|
| 多业务 Bot 长连接收件 | 每个业务应用各自 `event consume`；只处理已绑定业务群里的 `@bot` / `@登录用户` |
| 入队与串行消费 | 消息进 SQLite 队列；独立 worker 进程 claim → 处理 → ack/nack |
| 智能分发（默认） | Dispatcher 拉上下文、看开放任务，决定 create / followup / noop，并留痕 |
| 任务台账 | 创建/跟进后，用 **lark-cli 默认 app 的 bot** 在台账群发根消息并开话题帖 |
| HTTP API | 注册 bot、绑群、查/建任务、跟进、发消息、拉上下文等 |

典型一天：业务群有人 `@你的业务 bot` → Gateway 入队 → Dispatcher 建一条任务或跟进旧任务 → 台账群出现台账帖（并 `@` 你这个登录用户）。

## 架构（简图）

```text
飞书业务群 ──@bot/@你──► 业务 bot (lark-cli event consume)
                              │
                              ▼
                         Gateway :8000
                         (SQLite / 队列 / HTTP API)
                              │
                              ▼
                         Worker daemon
                         (默认 Dispatcher + DeepSeek)
                              │
                              ▼
台账群 ◄── 默认 app bot 发台账 ──┘
（同群兼：注册时 open_id 校准）
```

### 台账群是什么

这是一个**你自己建的专用飞书群**，主要干两件事：

1. **日常任务台账**（主用途）：每条任务一根消息 + 话题线程，跟进写进同一 thread  
2. **注册时的 open_id 校准**（一次性）：把业务 bot 拉进群，发 `@你 @bot calibrate`，记下 bot / 登录用户在事件里的 open_id  

**不要**把业务工作群当成台账群绑进 Gateway；台账群也**不能**再绑成业务收件群。

环境变量仍叫 `CALIBRATION_CHAT_ID`（历史命名），填的就是这个台账群的 `oc_...`。

两类飞书应用角色：

| 角色 | 用途 | 要不要 `POST /bots/register` |
|------|------|------------------------------|
| **默认 app bot** | 在台账群发任务帖（`--as bot`、无 `--profile`） | **不要**；在本机 `lark-cli config init` 配成默认即可 |
| **业务 bot** | 听业务群、被 @；注册时在台账群做 `@bot/@self` 校准 | **要**；register 后 secret 进 SQLite + named profile |

登录的 **飞书用户**（`lark-cli auth login`）用于：把 bot 拉进台账群、发注册校准消息、拉业务群消息上下文；台账开 thread 时也会 `@` 该用户。

## 提前准备

按顺序自检；缺一项后面会卡在注册或收不到消息。

### 1. 机器与依赖

- 一台能长期在线的机器（笔记本可以，但休眠/断网会丢长连接）
- Python 3.10+、能建 venv
- 已安装且在 `PATH` 里的 [`lark-cli`](https://github.com/larksuite/cli)
- DeepSeek API Key（默认 dispatcher 需要）

不要和别人共用同一个 Linux 用户 / `$HOME`（`~/.lark-cli` 会互相抢）。

### 2. 飞书：默认 app（台账 bot）

创建 / 管理应用可走网页端 [飞书开放平台 · 开发者后台](https://open.feishu.cn/app)（「创建企业自建应用」等），或 CLI：

```bash
lark-cli config init          # 已有 app_id/secret 写入默认 profile；或 --new 走浏览器新建
```

- 在开发者后台给该应用开通 **bot 发消息** 等必要权限
- 把这个 bot **拉进台账群**
- 本机用 `lark-cli config init --app-id …`（或交互式 init）把该应用配成 **CLI 默认 app**
- **不要**再对它调用本服务的 `/bots/register`

### 3. 飞书：业务 bot（可多个）

- 同样可在 [开发者后台](https://open.feishu.cn/app) 新建企业自建应用，或：

```bash
lark-cli config init --new --name my-biz-bot
```

- 事件用 **长连接**，订阅 `im.message.receive_v1`（网页端应用详情里配置）
- 开通收/发消息、表情、拉群成员等 bot 权限（缺权限时接口错误里常有 `console_url`，也可直接回开发者后台勾选）
- 记下 `app_id` / `app_secret`（启动后 `POST /bots/register` 时用）

权限与事件订阅以开发者后台为准；CLI 不能替 bot 自动开通 scope。

### 4. 飞书：用户登录

```bash
lark-cli auth login --domain all   # 或按需 --scope / --domain
lark-cli auth status --json --verify
```

### 5. 台账群（自己建）

见上文「台账群是什么」。群里至少要有：

1. 上面登录的用户（且能拉 bot 进群）
2. 默认 app bot（发台账）
3. 注册成功后会自动再拉进业务 bot

记下群的 `chat_id`（`oc_...`）→ 写入 `.env` 的 `CALIBRATION_CHAT_ID`。

### 6. 业务群

每个要接入的群：

- 拉进对应 **业务 bot**
- **登录用户也必须在群里**（拉上下文走 user）
- 记下 `chat_id`，服务起来后 `POST /bots/{id}/chats` 绑定

## 安装与配置

```bash
git clone <your-repo-url> lark-superbot
cd lark-superbot
python3 -m venv .venv
source .venv/bin/activate
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

cp .env.example .env
# 编辑 .env：至少填 CALIBRATION_CHAT_ID（台账群）、DEEPSEEK_API_KEY
```

### 配置项说明

复制自 [`.env.example`](.env.example)。业务 bot 的密钥**不必**写进 `.env`，走 HTTP register。

#### 必填

| 变量 | 做什么用 |
|------|----------|
| `CALIBRATION_CHAT_ID` | **台账群** ID（`oc_...`）。日常发任务帖；注册业务 bot 时也在此群做 open_id 校准。Gateway 启动必填；不可再绑成业务收件群。 |
| `DEEPSEEK_API_KEY` | Dispatcher 调 DeepSeek。`WORKER_IMPL=dispatcher`（默认）时 worker 启动即检查。 |

#### 常用可选

| 变量 | 做什么用 | 默认 |
|------|----------|------|
| `HOST` / `PORT` | Gateway 监听 | `0.0.0.0` / `8000` |
| `GATEWAY_BASE_URL` | Worker 访问 Gateway 的地址 | `http://127.0.0.1:8000` |
| `GATEWAY_TOKEN` | 若设置，所有管理 API 需 `Authorization: Bearer …` | 空（不校验） |
| `WORKER_IMPL` | `dispatcher`（智能分发）或 `simple`（调试回「出来干活」） | `dispatcher` |
| `WORKER_ID` | claim 时的 worker 标识 | `daemon-1` |
| `WORKER_IDLE_SLEEP` | 队列空时休眠秒数 | `1` |
| `DISPATCHER_LLM_PROVIDER` | LLM 厂商（目前仅 `deepseek`） | `deepseek` |
| `DISPATCHER_LLM_MODEL` | 模型名 | `deepseek-v4-pro` |
| `DISPATCHER_LLM_BASE_URL` | API base | `https://api.deepseek.com` |
| `DISPATCHER_LLM_API_KEY` | 通用 LLM key；可代替 `DEEPSEEK_API_KEY` | 空 |
| `DISPATCHER_AGENT_MAX_ITERATIONS` | 分发 Agent 最大工具轮次 | `6` |
| `LARK_USER_PROFILE` | user 身份用的 lark-cli profile；空则用 CLI 默认 | 空 |
| `DEFAULT_BOT_ID` | 没有任何已注册 bot 时，IM API 的 fallback profile | 空 |
| `GATEWAY_DB_PATH` | SQLite 路径 | `data/gateway.db` |
| `GATEWAY_TASK_WORKSPACE` | 任务工作区目录 | `data/task_workspace` |

`.env`、`data/`、`knowledge/` 默认不进 git；每人实例各自一份。

## 启动

```bash
./start.sh          # Gateway + worker；日志/pid 在 tmp/
curl -fsS http://127.0.0.1:8000/health
./stop.sh
```

或分进程：

```bash
source .venv/bin/activate
python run.py          # Gateway
python run_worker.py   # 另开终端
```

## 首次接入（空库必做）

假设 Gateway 已在 `http://127.0.0.1:8000`。

### 1. 注册业务 bot

```bash
curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/bots/register \
  -d '{"app_id":"cli_xxx","app_secret":"***"}'
```

可选字段：`id`（省略则 `1,2,3…`，同 `app_id` 复用）、`name`（省略则从台账群成员取显示名）。

成功时会：把 bot 拉进台账群、长连接就绪、在台账群发 `@你 @bot calibrate`（open_id 校准），并写入 `open_id` / `self_open_id`。

### 2. 绑定业务群

```bash
curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/bots/1/chats \
  -d '{"chat_id":"oc_biz_xxx"}'
```

未绑定的群里的 @ 会被丢弃。台账群不能绑成业务群。若群已绑别的 bot，可加 `"force": true`。

### 3.（可选）群绑定项目

```bash
curl -sS -H 'Content-Type: application/json' \
  -X PUT http://127.0.0.1:8000/chat-projects/oc_biz_xxx \
  -d '{"project_id":"my-project","note":"可选说明"}'
```

建任务未显式传 `project_id` 时会继承。项目知识可放在本地 `knowledge/projects/`（该目录默认 gitignore）。

### 4. 验证

- 台账群：注册时有校准消息；之后建任务应出现台账帖
- 业务群：`@业务 bot` 或 `@登录用户` → 队列有消费；台账群有新帖或 followup

## 日常怎么用

1. **在业务群 @**：只处理已绑定群里的 `@业务 bot` 或 `@登录用户`（注册校准时记下的 self）
2. **看台账群**：每条任务一根消息 + 话题线程；跟进会追加到同一 thread
3. **HTTP 查任务 / 跟进**（Agent 或脚本）：

```bash
curl -sS 'http://127.0.0.1:8000/tasks?status=noted&limit=20'
curl -sS http://127.0.0.1:8000/tasks/12
curl -sS -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/tasks/12/followups \
  -d '{"message":"已查完，等人确认","event_type":"note","status":"waiting_human","actor":"agent"}'
```

若设置了 `GATEWAY_TOKEN`，以上请求都要加：`-H "Authorization: Bearer $GATEWAY_TOKEN"`。

## 任务约定

释义以代码为准：`app/services/tasks/models.py`。

**kind**

| 值 | 含义 |
|----|------|
| `readonly` | 信息收集 / 查口径 / 查根因等；不改业务侧 |
| `operational` | 需要执行操作（改数、跑 job、提 PR、动配置等） |

**status**

| 值 | 含义 |
|----|------|
| `noted` | 已记下，尚未开始（新建默认） |
| `waiting_human` | 等人工处理或确认 |
| `waiting_external` | 等外部资源 |
| `agent_running` | agent 正在处理 |
| `blocked` | 阻塞 |
| `done` | 完成且已反馈 |
| `cancelled` | 取消 |

终态 `done` / `cancelled` 后仍可追加 note，不可再改 status（HTTP 409）。  
台账飞书失败不阻断落库，会记 `board_sync_error`，后续 followup 会尝试补建。

### 任务线索（clues）

线索是任务相关材料的索引（Cursor 会话、飞书消息、仓库等），**不是** followup，**不同步台账**。同一任务可挂多条；同一线索也可挂到多个任务。

| 字段 | 说明 |
|------|------|
| `kind` | `cursor_session` / `lark_thread` / `lark_message` / `git_repo` |
| `ref_key` | 该类型下的稳定主键（如 `CURSOR_CONVERSATION_ID`） |
| `one_liner` | 这线索在本任务里干了什么（可更新） |
| `relevance` | `primary` > `related` > `weak`；新建默认 `related`；upsert 默认只升不降（除非 `demote=true`） |

## Worker 模式

| 模式 | 职责 |
|------|------|
| **dispatcher**（默认） | 拉上下文 → 建任务 / 跟进 / noop；**不**回业务群、不执行业务 |
| **simple** | 调试：被 @bot 时回一句「出来干活」；仅 @self 则 skip |

Dispatcher 可用工具：`fetch_message_context`、`get_chat_project`、`list_open_tasks`、`get_task`、`create_task`、`followup_task`、`finalize_dispatch`。

## HTTP API 一览

| 路径 | 用途 |
|------|------|
| `GET /health` | 探活、bot 长连接状态、队列概览 |
| `POST /bots/register` · `GET /bots` · `GET /bots/{id}` | 注册 / 列表 / 详情 |
| `POST /bots/{id}/chats` | 绑定业务群 |
| `POST /tasks` · `GET /tasks` · `GET /tasks/{id}` · `POST /tasks/{id}/followups` | 任务 |
| `POST /tasks/{id}/clues` · `GET /tasks/{id}/clues` · `GET /tasks/{id}/clues/{clue_id}` · `GET /clues?kind=&ref_key=` | 任务线索索引（读写） |
| `PUT\|GET\|DELETE /chat-projects/{chat_id}` · `GET /chat-projects` | 群 ↔ 项目 |
| `POST /dispatcher/runs` · `GET /dispatcher/runs/{inbound_id}` | 分发留痕 |
| `POST /im/send` · `/im/reply` · `/im/respond` · `/im/messages/context` | 发消息 / 按 inbound 回复 / 拉上下文 |
| `POST /queue/{name}/enqueue\|claim\|ack\|nack` · `GET /queue/{name}/stats` | 队列（`inbound` = 消息队列） |

`/im/respond`：`{"inbound_id","text","mention_open_ids?"}` — 按入库 payload 的 `thread_id` 自动话题/引用回复。

## 开发自测

```bash
pytest -q
```

单测用临时 SQLite，`create_app(testing=True)` 不拉飞书长连接。

## 目录结构（开发向）

```text
app/            # Gateway：api / core / services / infra
workers/        # Daemon + DispatcherWorker / SimpleWorker
docs/           # 愿景与路线等设计文档
run.py          # 启动 Gateway
run_worker.py   # 启动 worker
data/           # 运行时 SQLite 与任务工作区（gitignore）
knowledge/      # 可选本地项目知识（gitignore）
tmp/            # 日志与 pid（gitignore）
```

## 常见卡点

| 现象 | 常查 |
|------|------|
| Gateway 起不来 | `.env` 是否有 `CALIBRATION_CHAT_ID`（台账群） |
| Worker 起不来（dispatcher） | 是否有 `DEEPSEEK_API_KEY` |
| register 超时 / 校准失败 | user 是否登录；台账群是否有默认 bot + 用户；业务 bot 长连接与事件订阅 |
| 业务群 @ 没反应 | 是否 `POST .../chats`；bot 与登录用户是否都在群里 |
| 有任务无台账帖 | 默认 app bot 是否在台账群、是否有发消息权限；看任务的 `board_sync_error` |
| 两人互相抢连 | 是否共用同一 `app_id` 或同一 `$HOME` 下的 `~/.lark-cli` |
