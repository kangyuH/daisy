# lark-superbot

飞书 Gateway（分层 FastAPI）：多 Bot 长连接收件 + SQLite 配置/队列 + IM HTTP 代理；独立 worker daemon 串行消费队列。

```text
app/            # Gateway：api / core / services / infra
workers/        # Daemon（调度）+ SimpleWorker（业务）
run.py          # 启动 Gateway
run_worker.py   # 启动 daemon
```

运行时配置在 **SQLite**（`data/gateway.db`）。收件走 `lark-cli event consume`，入队只打本服务 HTTP。

## 依赖

- `lark-cli`（user 已登录，供校准/拉消息）
- `.venv` + `pip install -r requirements.txt`

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
python run_worker.py   # 另开终端；串行 claim → SimpleWorker
```

环境变量（worker）：`GATEWAY_BASE_URL`（默认 `http://127.0.0.1:8000`）、`GATEWAY_TOKEN`、`WORKER_ID`、`WORKER_IDLE_SLEEP`。

## Daemon vs Worker

| 角色 | 职责 |
|------|------|
| **Daemon** | 只 `claim` → 调用 worker → 按结果 `ack/nack`；不做 @bot/@self 判断 |
| **SimpleWorker** | 读 payload：含 `bot_open_id` 才 `POST /im/respond`；仅 @self 则 skip |

本期 SimpleWorker：正文 `出来干活`；`mention_open_ids=[self_open_id]`。`POST /im/respond` 只收 `inbound_id` + text（+ 可选额外 @）；Gateway 查 `queue_items` 自行取 `message_id` / `thread_id` / `sender_open_id` 封装回复。

## 测试

```bash
pytest -q
```

单测用临时 SQLite，`create_app(testing=True)` 不拉飞书长连接。

## 主要 API

- `POST /bots/register` · `GET /bots` · `POST /bots/{id}/chats`  
  - register 只需 `app_id`/`app_secret`；`id` 省略则数字自增（`1,2,3…`，同 app_id 复用）；`name` 省略则从校准群成员列表自动取 bot 显示名
- `POST /queue/{name}/enqueue|claim|ack|nack` · `GET /queue/{name}/stats`（返回 `item`/`items`，表名 `queue_items`；`inbound`=消息队列，任务列表另议）
- `POST /im/send` · `/im/reply` · `/im/respond` · `/im/messages/context`
- `GET /health`

`/im/respond`：`{"inbound_id", "text", "mention_open_ids?"}` — 按入库 payload 的 `thread_id` 自动话题/引用回复；`sender_open_id` 置顶去重。底层 `/im/reply` 仍保留供排障。

校准群：`CALIBRATION_CHAT_ID`（默认见 `.env`），不可作业务绑群。
