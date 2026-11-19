# lark-superbot

lark-superbot 的产品方向是一本供人和 agent 共用的任务记事本。目标架构把任务树、契约、进度和飞书收发放在 VPS，由内网 daemon 拉起 agent。router 给消息找归属，task_owner 维护任务，worker 按契约办理。第一版计划由人主动挑选任务执行。

[产品蓝图](docs/blueprint.md)说明角色分工、任务生命周期和人工接管。[实施路线图](docs/20261119-task-coordination-roadmap.md)给出各阶段的交付顺序和评测方法。

## 当前版本能做什么

下文是本分支的运行说明。现有 Dispatcher 把一条入站判为新建、跟进一条已有任务或略过。建任务和跟进会同步飞书台账；业务群的自动文字回执已关闭。当前版本仍提供自动 research 和台账执行命令。

| 能力 | 说明 |
|------|------|
| 多业务 Bot 长连接收件 | 每个业务应用各自 `event consume`；只处理已绑定业务群里的 `@bot` / `@登录用户` |
| 入队与串行消费 | 消息进 SQLite 队列；独立 worker 进程 claim → 处理 → ack/nack |
| 智能分发（默认） | Dispatcher 拉上下文、看开放任务，记录 create / followup / noop 及判断依据 |
| 任务台账 | 用 **lark-cli 默认 app 的 bot** 建台账根消息，后续进展追加到同一话题 |
| 自动 research | create 后入 `queue=agent`，agent worker 串行调 Gateway 的 `/agent/run`，Gateway 在本机启动 Cursor CLI 做调研并记录 followup |
| 台账口令 | 登录用户在任务 thread 发 `/research` `/plan` `/exec` `/stop`（当场 spawn，不入队） |
| HTTP API | 注册 bot、绑群、查/建任务、跟进、发消息、拉上下文等 |

业务群有人 `@业务 bot` 后，Gateway 将消息入队。Dispatcher 新建任务时，台账群会出现一条任务帖；如果是跟进，则写进已有任务的话题。

## 当前运行架构

```text
飞书业务群 ──@bot/@你──► 业务 bot (lark-cli event consume)
                              │
                              ▼
                         Gateway :8000
                         (SQLite / 队列 / HTTP API)
                         ├─ inbound → Dispatcher worker
                         ├─ agent   → Agent worker → /agent/run → Gateway 执行 Cursor CLI
                         └─ 台账口令 → 当场 spawn Cursor CLI
                              │
                              ▼
台账群 ◄── 默认 app bot 发台账 ──┘
（同群兼：注册时 open_id 校准；thread 内 /research|/plan|/exec|/stop）
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
| `AGENT_AUTO_RESEARCH` | 是否在建任务后自动安排 research。设为 `false` 时，台账里的手动命令仍可用 | `true` |
| `WORKER_ID` | claim 时的 worker 标识 | `daemon-1` |
| `WORKER_IDLE_SLEEP` | 队列空时休眠秒数 | `3` |
| `DISPATCHER_LLM_PROVIDER` | LLM 厂商（目前仅 `deepseek`） | `deepseek` |
| `DISPATCHER_LLM_MODEL` | 模型名 | `deepseek-v4-pro` |
| `DISPATCHER_LLM_BASE_URL` | API base | `https://api.deepseek.com` |
| `DISPATCHER_LLM_API_KEY` | 通用 LLM key；可代替 `DEEPSEEK_API_KEY` | 空 |
| `DISPATCHER_AGENT_MAX_ITERATIONS` | 分发 Agent 最大工具轮次 | `6` |
| `LARK_USER_PROFILE` | user 身份用的 lark-cli profile；空则用 CLI 默认 | 空 |
| `DEFAULT_BOT_ID` | 没有任何已注册 bot 时，IM API 的 fallback profile | 空 |
| `GATEWAY_DB_PATH` | SQLite 路径（**必须本地盘，禁止 NFS**） | `data/gateway.db` |
| `GATEWAY_TASK_WORKSPACE` | 任务工作区目录（可 NFS） | `data/task_workspace` |

`GATEWAY_DB_PATH` 若落在 NFS（如 `/share/...`），空闲 claim 写锁会让 `/health` 与任务 API 间歇卡死（约 5s）。本机推荐例如 `/data/lark-superbot/gateway.db`；工作区可继续放仓库 `data/task_workspace`。

`.env`、`data/`、`knowledge/` 默认不进 git；每人实例各自一份。

## 启动

```bash
./start.sh          # Gateway + worker；日志/pid 在 tmp/
curl -fsS http://127.0.0.1:8000/live    # 探活（不碰 SQLite）
curl -fsS http://127.0.0.1:8000/health  # 诊断（含 db_path / 队列）
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

建任务时会继承业务群绑定的 `project_id`，也可在请求中显式指定。当前 Dispatcher 经 Gateway 读取群与项目的绑定；Gateway 启动的调研 agent 使用本地 `knowledge/projects/` 中的项目配置。

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

## 当前任务模型

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

当前 API 允许给 `done` / `cancelled` 追加 note，变更终态会返回 HTTP 409。新蓝图中可再次唤起的稳定态属于后续实现。

台账飞书失败不阻断落库，会记 `board_sync_error`，后续 followup 会尝试补建。

### 语义契约（contract）

结果承诺写在任务工作区 `contract.yaml`，字段以 [`docs/daisy-task-contract.template.yaml`](docs/daisy-task-contract.template.yaml) 为准（`schema_version: "0.1"`）。Gateway 在 `POST /tasks` 时写入初稿，并用 `POST /tasks/{id}/contract` 整份替换。请求体里的 `revision` 会被拒绝。

保存次数在 `tasks.contract_revision`：0 表示尚未通过 API 保存，创建成功后为 1，每次替换加 1。YAML 里不写这个数字。

保存时从契约投影 `title`（契约标题）和 `one_liner`（`objective.statement`，陈述为空则不改旧值）。`feedback.destination` 只有在 `status: confirmed` 且 `kind: feishu_thread` 时才覆盖 `thread_id`。`kind`、进度、运行锁、台账和 `chat_id` 不从契约投影。

`GET /tasks/{id}` 附带 `contract`。`contract_revision = 0` 且没有文件时，按旧列合成同一形状，`contract_persisted` 为 false，不落盘。列表只带 `contract_revision`，不嵌整份 YAML。

### 任务线索（clues）

线索是任务相关材料的索引（Cursor 会话、飞书消息、仓库等），**不是** followup，**不同步台账**。同一任务可挂多条；同一线索也可挂到多个任务。

| 字段 | 说明 |
|------|------|
| `kind` | `cursor_session` / `lark_thread` / `lark_message` / `git_repo` |
| `ref_key` | 该类型下的稳定主键（如 `CURSOR_CONVERSATION_ID`） |
| `one_liner` | 这线索在本任务里干了什么（可更新） |
| `relevance` | `primary` > `related` > `weak`；新建默认 `related`；upsert 默认只升不降（除非 `demote=true`） |

## 当前入站 worker 模式

| 模式 | 职责 |
|------|------|
| **dispatcher**（默认） | 拉上下文、读契约短稿，决定 create / followup / noop 并记录判断依据；契约由调用方通过 API 维护 |
| **simple** | 调试：被 @bot 时回一句「出来干活」；仅 @self 则 skip |

Dispatcher 可用工具：`fetch_message_context`、`get_chat_project`、`list_open_tasks`、`get_task`、`create_task`、`followup_task`、`finalize_dispatch`。认任务时读取工具返回的 `contract_brief`。当前契约版本见[契约设计说明](docs/task-contract-design.md)。

## HTTP API 一览

| 路径 | 用途 |
|------|------|
| `GET /live` | 轻量探活（进程 + 内存 bot 状态，不查 SQLite） |
| `GET /health` | 诊断探活：db_path、bot 长连接、队列概览 |
| `POST /bots/register` · `GET /bots` · `GET /bots/{id}` | 注册 / 列表 / 详情 |
| `POST /bots/{id}/chats` | 绑定业务群 |
| `POST /tasks` · `GET /tasks` · `GET /tasks/{id}` · `POST /tasks/{id}/contract` · `POST /tasks/{id}/followups` · `POST /tasks/{id}/board-messages` | 任务与语义契约（contract 不记进展、不贴台账；board-messages 只贴台账） |
| `POST /tasks/{id}/clues` · `GET /tasks/{id}/clues` · `GET /tasks/{id}/clues/{clue_id}` · `GET /clues?kind=&ref_key=` | 任务线索索引（读写） |
| `PUT\|GET\|DELETE /chat-projects/{chat_id}` · `GET /chat-projects` | 群 ↔ 项目 |
| `POST /dispatcher/runs` · `GET /dispatcher/runs/{inbound_id}` | 分发留痕。当前业务群文字回执关闭，`reply_sync` 会报告跳过 |
| `POST /im/send` · `/im/reply` · `/im/respond` · `/im/messages/context` | 发消息 / 按 inbound 回复 / 拉上下文 |
| `POST /queue/{name}/enqueue\|claim\|ack\|nack` · `GET /queue/{name}/stats` | 队列（`inbound` = 消息队列） |

### 业务群发消息与回复

`POST /im/send` 按 `chat_id` 发新消息。`POST /im/reply` 按 `message_id` 引用原消息，设 `reply_in_thread: true` 可在话题内回复。两者都可用 `bot_id` 选择业务 bot。`POST /im/respond` 按 `inbound_id` 取原消息和业务 bot，自动选择话题或引用回复。

JSON 请求中的纯文本使用顶层 `text`。发送其他格式时，使用一个 `message` 对象代替 `text`。

| 格式 | `message` 示例 |
|------|----------------|
| Markdown | `{"type":"markdown","text":"## 进展\n- 已完成"}` |
| 图片 | `{"type":"image","image_key":"img_xxx"}` |
| 文件 | `{"type":"file","file_key":"file_xxx"}` |
| 音频 | `{"type":"audio","file_key":"file_xxx"}` |
| 视频 | `{"type":"video","file_key":"file_xxx","image_key":"img_xxx"}`，`image_key` 可省略 |
| 卡片 | `{"type":"card","content":{…}}`，`content` 是[飞书卡片 JSON 对象](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json) |

纯文本和 Markdown 支持 `mention_open_ids`，`/im/respond` 也会在这两种格式中自动 `@` 原发送人。图片、文件、音视频和卡片消息可使用对应的飞书资源或卡片内容。资源 key 须可供发送消息的 bot 使用。

```bash
curl -fsS http://127.0.0.1:8000/im/send \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","bot_id":"1","message":{"type":"markdown","text":"## 进展\n- 已完成"}}'
```

图片和文件可直接用 `multipart/form-data` 上传并发送。三个接口使用相同的表单字段。

| 字段 | 内容 |
|------|------|
| `payload` | JSON 字符串，包含目标和通用选项，如 `message_id`、`bot_id`、`reply_in_thread`、`task_id`、`idempotency_key` |
| `type` | `image` 或 `file` |
| `file` | 一个文件的二进制内容，图片上限 10 MiB，文件上限 30 MiB |

直接上传要求业务 bot 开通飞书的图片或文件资源上传权限，例如 `im:resource`。音频、视频使用已上传的飞书资源 key。

```bash
curl -fsS http://127.0.0.1:8000/im/reply \
  --form-string 'payload={"message_id":"om_xxx","bot_id":"1","reply_in_thread":true,"idempotency_key":"deliver-report-1"}' \
  --form-string 'type=file' \
  -F 'file=@./report.pdf;type=application/pdf'
```

若实例配置了 `GATEWAY_TOKEN`，请求还需带 `Authorization: Bearer …`。

三个接口都可传 `task_id`，将新发飞书消息关联到任务。`/im/send` 和 `/im/reply` 的任务消息需要传稳定的 `idempotency_key`（最多 50 字符）。`/im/respond` 默认使用 `inbound:{id}`，省略 `task_id` 时可沿用已有的分发归属。任务消息的结果包含 `task_link`，若状态为 `unrecorded`，可按原因处理，写入失败时用同一个幂等键重试。重试直接上传时，同一个幂等键用于飞书消息去重，附件仍可能重新上传。

### 消息上下文

`/im/messages/context` 返回每条消息的 `task_refs`，可传 `reply_to_message_id` 获取同群的直接引用目标 `reply_target`。Dispatcher 优先核对引用消息对应的任务契约。

### MCP

在 `.env` 设置独立的 `GATEWAY_MCP_TOKEN` 并启动 Gateway。客户端用 Streamable HTTP 连接
`http://127.0.0.1:8000/mcp`，发送 `Authorization: Bearer <GATEWAY_MCP_TOKEN>`。
MCP 初始化成功后，未配置 token 时 `/mcp` 返回 503。SDK 导入或 MCP 初始化失败时，
Gateway 默认记录告警，以 REST 模式启动，此时 `/mcp` 返回 404。部署要求 MCP 正常初始化时，
设置 `GATEWAY_MCP_REQUIRED=true` 让这些错误中止启动。

远程连接时，把客户端访问的主机和端口加入 `GATEWAY_MCP_ALLOWED_HOSTS`，多个值用逗号分隔。
浏览器客户端发送的 Origin 可用 `GATEWAY_MCP_ALLOWED_ORIGINS` 指定。
工具默认可访问已绑定的业务群。设置 `GATEWAY_MCP_CHAT_IDS` 可进一步限制群范围。
消息上下文里的任务引用和回复的自动任务关联只对同群任务生效。

| 工具 | 用途 |
|------|------|
| `list_tasks` | 按业务群查询近期任务，可按状态筛选 |
| `get_task` | 用任务 ID 查详情、契约和近期事件 |
| `get_message_context` | 用入站队列条目 ID（`inbound_id`）读取飞书上下文及直接引用目标 |
| `append_task_followup` | 追加任务跟进并同步台账。重试传相同 `idempotency_key` |
| `reply_to_inbound` | 用 `inbound_id` 回复原消息，支持文本和已有飞书媒体 key 等 JSON 消息格式。默认按 `inbound_id` 去重，同一消息的不同回复可传各自稳定的 `idempotency_key` |

从飞书消息建出的任务可以用任务详情的 `created_from_inbound_id` 找回首条入站消息。
Dispatcher 和 Worker 通过内部 REST 领取、续租和确认队列项。

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
