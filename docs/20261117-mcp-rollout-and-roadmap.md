# 2026-11-17 MCP 上线记录

本页保留 11 月 17 日 Gateway 的上线结果，供维护五个 MCP 工具和排查旧实例时使用。后续开发以[协作记事本设计](blueprint.md)和[实施与评测路线图](20261119-task-coordination-roadmap.md)为准。

## 1. 本次已上线的改动

Gateway 已部署包含 MCP 的版本，参见下方的上线验证。

### 1.1 分层重构

把 IM 与任务的业务逻辑从 HTTP 层搬到独立 service 层，让 REST 与 MCP 共用同一套领域动作。

| 新文件 | 职责 |
|---|---|
| `app/services/im/gateway.py` | 入站项读取、任务发送校验、按入站回复、出站消息与任务关联、消息上下文 |
| `app/services/tasks/gateway.py` | 任务列表、任务详情、追加跟进（含台账同步补偿） |
| `app/api/mcp.py` | MCP Server 与 5 个工具、Bearer 鉴权、作用域校验 |

`app/api/im.py` 净删约 140 行，API 层只保留「解析 + 异常映射」：`ImGatewayError(status_code)` 映射为 4xx，`LarkCliError` 映射为 502。

### 1.2 MCP 工具面（5 个）

| 工具 | 对应 REST | 说明 |
|---|---|---|
| `list_tasks` | `GET /tasks` | 按业务群查近期任务 |
| `get_task` | `GET /tasks/{id}` | 任务详情、契约、近期事件 |
| `get_message_context` | `POST /im/messages/context` | 按入站项 ID 读取飞书上下文与直接引用目标 |
| `append_task_followup` | `POST /tasks/{id}/followups` | 追加跟进并同步台账，支持幂等重试 |
| `reply_to_inbound` | `POST /im/respond` | 按入站项 ID 回复原消息 |

安全设计：

- `GATEWAY_MCP_TOKEN` 是独立的静态 token。MCP 初始化成功且 token 未配置时，`/mcp` 返回 503。
- `bot_store.get_chat_binding` 校验业务群绑定，配置 `GATEWAY_MCP_CHAT_IDS` 可进一步限制允许访问的群。
- 超出授权群范围的任务与消息请求会被拒绝。
- 默认 Host 允许列表包含本机回环地址。远程访问通过 `GATEWAY_MCP_ALLOWED_HOSTS` 配置，客户端发出 Origin 时另外通过 `GATEWAY_MCP_ALLOWED_ORIGINS` 校验。
- `stateless_http=True` 采用无会话传输。断线后的写操作使用幂等键重试。

### 1.3 跟进幂等与台账补偿

- `task_events` 新增 `idempotency_key`、`board_sync_json`，并建 partial 唯一索引 `(task_id, idempotency_key) WHERE idempotency_key IS NOT NULL`
- 命中相同 key 时返回原事件；参数不一致抛 `TaskConflictError`
- 首次台账同步失败时，用相同 key 重试只补台账、不重复记账（`record_followup_board_sync`）

### 1.4 上线加固

| 加固项 | 原因 |
|---|---|
| MCP 可降级（`app/main.py`） | SDK 导入或 MCP 初始化失败时记录告警并保留 REST，`/mcp` 返回 404。设置 `GATEWAY_MCP_REQUIRED=true` 时，这些错误会中止启动 |
| `httpx2` 显式声明 | 测试直接 import，但此前只是 mcp 的传递依赖 |
| 建表定义补新列 | 新库不再依赖迁移补列 |
| 迁移建索引 | 索引必须在 `_migrate_schema` 里、补列之后创建；若写进 `_SCHEMA`，老库会因列不存在而启动失败 |

上线检查确认 `/live` 返回 200、`/health` 正常，未配置 token 的 `/mcp` 返回 503。五个 MCP 工具通过隔离环境联调验证。

## 2. 复用这次上线成果

**任务操作与进程调度。** REST 与 MCP 复用任务和消息的领域动作。现有内网 daemon 经 REST 领取、续租、确认或退回队列项，HTTP 409 表示租约冲突。新设计沿用这条调度边界，由 daemon 根据待办启动本地 agent，由 VPS 校验角色可用的工具、当前控制权与写操作。队列租约失效或人接管后，旧运行继续提交写入是需要覆盖的回归场景。

**消息关联与去重。** 在 11 月 17 日这版里，`dispatch_runs.inbound_id` 唯一，一条分发记录只有一个 `task_id`。现有 MCP 的 `append_task_followup` 会追加任务事件并同步台账。新设计中的 router follow 是把一段原文关联到已有任务；同一消息可以有多个目标。入站事件仍去重，每个目标的证据关联也要经得起重试。一次消息关联两项任务，并不要求发送两份回执或唤醒两个 worker。归属不清的内容进入项目根，由根的 task_owner 判断是否拆建子任务。

**扩展 MCP 的次序。** 首先补齐人和三种角色所需的动作，分别检查可写字段与作用域。现有 `list_tasks` 必传业务群，`get_task` 可读契约，`append_task_followup` 只支持追加备注；支持搜索项目、按版本改契约、领取者交付与人工接管还需要开发。任务读模型稳定后再开放 task resources，多端有分发需求时再接 Skills over MCP。旧 dispatcher 的 `create_task`、`finalize_dispatch` 不作为新 router 的工具设计依据。

**VPS 与权限。** 此次上线使用本地磁盘 SQLite/WAL、飞书 CLI 长连接和独立的 REST/MCP 静态 token。新方案的单实例 VPS 可沿用本地持久盘，迁移时需验证数据库恢复、CLI 身份、单 bot 收件端和对外发送归属。服务端的角色权限、版本和控制权校验是新工作。PostgreSQL、飞书 webhook 或多实例部署可等实际容量需求确定后再评估。

## 3. 代码索引

| 关注点 | 入口 |
|---|---|
| MCP 工具与可选装配 | [工具定义](../app/api/mcp.py)、[应用启动](../app/main.py) |
| IM、任务与台账补偿 | [IM 领域动作](../app/services/im/gateway.py)、[任务领域动作](../app/services/tasks/gateway.py)、[任务存储](../app/services/tasks/store.py) |
| 入站、持久化与租约 | [飞书接收器](../app/infra/events/consumer.py)、[数据库](../app/infra/db.py)、[队列实现](../app/services/queue/service.py) |
| 旧分发运行 | [daemon](../workers/daemon.py)、[dispatcher 工具](../workers/dispatcher/tools.py) |
