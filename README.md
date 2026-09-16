# lark-superbot

飞书 Gateway（分层 FastAPI）：多 Bot 长连接收件 + SQLite 配置/队列 + IM HTTP 代理。

```text
app/
  api/          # HTTP 路由
  core/         # settings / deps
  services/     # bots / queue / im 业务
  infra/        # sqlite / lark-cli / event consume
```

运行时配置在 **SQLite**（`data/gateway.db`）。收件走 `lark-cli event consume`，入队只打本服务 HTTP。

## 依赖

- `lark-cli`（user 已登录，供校准/拉消息）
- `.venv` + `pip install -r requirements.txt`

## 启动

```bash
cd /share/home/hukangyu/gitlab/lark-superbot
source .venv/bin/activate
python run.py
# 或: python -m app.main
```

## 测试

```bash
pytest -q
```

单测用临时 SQLite，`create_app(testing=True)` 不拉飞书长连接。

## 主要 API

- `POST /bots/register` · `GET /bots` · `POST /bots/{id}/chats`
- `POST /queue/{name}/enqueue|claim|ack|nack` · `GET /queue/{name}/stats`
- `POST /im/send` · `/im/reply` · `/im/messages/context`
- `GET /health`

校准群：`CALIBRATION_CHAT_ID`（默认见 `.env`），不可作业务绑群。
