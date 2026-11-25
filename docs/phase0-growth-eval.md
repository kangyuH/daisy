# 阶段 0 按消息长树回放

使用一页项目地图，回放从只有项目根开始。地图提供术语、常见环节和辨识业务期次的线索。每条消息看到同一话题的有限前文、当前任务树和根上待判断的证据。任务依据当前沟通形成，正在处理的事项可以补证据、更新进展或更正，后续消息也能把根上的待判断证据归位。

当前用单个 dispatcher 给出两类决定：`routes` 表示证据该挂到哪里，`changes` 表示创建、更新或归位。运行器校验引文、引用和状态变更，并一起写入本次内存账本。无效动作整条回滚。重复投递已提交的消息会直接跳过。未提交的消息可以重试。任务树的范围仅限回放；上轮固定候选的 `phase0_eval` 回放仍可作为归属检查。

## 文件运行

真实回放输入和参考草稿位于 Git 忽略的 `data/phase0/jijia-growth/`。事件按源消息创建时间排列，使用脱敏缓存的现存正文，可能包含发送者后来的修改。抽样上下文有限。每条真实或合成流程各自从同一个项目根开始。

```bash
source .venv/bin/activate
bundle=data/phase0/jijia-growth/bundle.json
reference=data/phase0/jijia-growth/reference.json
python -m phase0_eval.growth validate --bundle "$bundle" --reference "$reference"
python -m phase0_eval.growth run --bundle "$bundle" --reference "$reference" \
  --mode stub --out data/phase0/jijia-growth/stub-02

# 在评估进程环境中只导出模型密钥 PHASE0_MODEL_API_KEY。
python -m phase0_eval.growth run --bundle "$bundle" --reference "$reference" \
  --mode live --out data/phase0/jijia-growth/live-02
```

默认模型 `deepseek-flash`。评估进程沿用原隔离保护，模型请求只发到配置的聊天补全接口。Stub 只验证文件流程，全部暂存根。加 `--repair-invalid` 后，每条无效提案最多向模型反馈一次校验错误并重试。结果保留每次提案及错误。运行会保存每条消息的模型结果、动作、证据和前后状态，以及独立的 HTML/XML 审阅文件。飞书文档发布是单独操作。

## 审阅和重新评分

参考草稿按事件写可接受的动作组合和业务期次约束，另外注明需要人判断的任务边界、证据力度和可接受的树形。动作命中率只表示与草稿约束的一致情况，不能证明生成的任务语义正确。合成回归单列。可以改参考文件并从保存结果重新评分：

```bash
python -m phase0_eval.growth score --bundle "$bundle" --reference "$reference" \
  --result data/phase0/jijia-growth/live-01/result.json \
  --out data/phase0/jijia-growth/rescore-01
```

如果调整地图、事件或提示词，重新运行生成新结果。项目地图的自动更新、人审和生产接入是后续独立工作。测试入口为 `python -m pytest -q phase0_tests`。
