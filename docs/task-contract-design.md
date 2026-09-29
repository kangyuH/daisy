# Task 语义契约：本轮思路

本文记录已经落地的契约存储和分发边界，以及暂不开发的异步维护路径。

## 已落地

一个 Task 是一份有边界、可以判断是否完成的结果承诺。承诺正文在任务工作区的 `contract.yaml`，形状以 `docs/daisy-task-contract.template.yaml` 为准（`schema_version: "0.1"`）。

SQLite 的 `tasks` 行继续负责任务身份、进度、路由和 Agent 运行锁。和承诺重合的 `title`、`one_liner` 在保存契约时由 Gateway 投影出来，不再单独当作承诺原文。`kind`、`status`、群、thread、台账和运行锁不从契约投影。反馈位置只有在 `confirmed` 且是 `feishu_thread` 时才覆盖 `thread_id`。

保存次数在 `tasks.contract_revision`，不写进 YAML。创建成功后为 1，每次 `POST /tasks/{id}/contract` 加 1。请求体里带 `revision` 会被拒绝。这样代次由接口维护，不靠调用方自己记得加一。新正文先写到 `contract.yaml.tmp`，代次和投影提交成功后再替换 `contract.yaml`。更新失败则删掉临时文件，上一版契约和代次保持不动。

`GET /tasks/{id}` 附带整份契约。`contract_revision = 0` 且没有文件时，按旧列合成同一形状，`contract_persisted` 为 false，不落盘。列表只带 `contract_revision`。

已有任务已按当时的标题、一句话和写明的拆分做过一轮转录。目标陈述多为 `provisional`，验收和相关方多为 `open`。

## Dispatcher 的边界

Dispatcher 只做两件事：判断这条消息属于哪个任务，然后把原始需求附上去。决定仍是 `create`、`followup`、`noop`。

认任务时读工具返回的 `contract_brief`：业务对象（类型、引用、这一次发生范围、状态）、目标陈述与状态、范围是否列全、纳入与排除、验收是否列全、缺口问题、父任务引用。标题、群和 thread 只用于过滤和路由。`provisional` 不是已经确认的承诺。某条任务带 `contract_brief_error` 时，短稿没有取到，不能按标题跟进。

跟进把入站原文记到该任务上，不改目标、范围或验收。另一份可以单独验收的承诺才新建；新建仍走 `POST /tasks`，由 Gateway 按标题和 `one_liner` 生成契约初稿。对不上，或跟进会悄悄扩大原任务责任时，选择 `noop` 并写明原因。

Dispatcher 没有改契约的工具。范围是否变化、父子关系如何写入契约，不在分发里完成。

## 暂不开发：异步维护契约

维护契约的是后续 agent，不是 Dispatcher。本轮不改自动调研、规划和执行。

设想中的路径是：

- 自动调研仍写 `research.md` 的诉求澄清、事实层和方案建议。
- 本轮对契约的修改建议另写工作区里的 `contract.proposal.yaml`，不直接改 `contract.yaml`。
- 调研结束时由 Gateway 校验提案。通过才调用 `save_contract`，`contract_revision` 加 1。校验失败只记一条错误，不替换现有契约。
- 提出者原文标 `confirmed`，有依据的推断标 `provisional`，看不出保持 `open`。空列表不表示已经确认没有。不得扩大承诺。业务上的取舍留在 `gaps`。
- 规划与执行只读当前契约：不超出 `scope`；缺口还没解开时，只做 `gaps` 里允许做的事，不承诺那里写明不能承诺的结果。

这条路径等维护契约的 agent 单独开发时再接。在那之前，契约只在创建时生成初稿，或由调用方显式 `POST /tasks/{id}/contract`。
