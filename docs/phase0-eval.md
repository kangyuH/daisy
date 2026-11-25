# 阶段 0 路由回放

本工具比较当前 LangChain dispatcher 与一个仅服务于评估的多目标 router。两版从“消息已进入路由”开始。现有收件门槛另报，无法从缓存确认的情况记为 unknown。多目标 router 获得文件中完整的候选集合。现有 dispatcher 通过原工具获取候选，关闭状态、候选可见性、单目标输出等旧行为保留。报告不衡量生产检索效果。

## 运行方法

在远程仓库根目录用现有的 Python 环境执行。吉家真实数据和报告位于 Git 忽略的 `data/phase0/jijia/`。数据是两期沟通记录构成的冻结场景，包含发送者编辑后的缓存正文。人工设计的任务树和草稿标注独立于现有任务系统。每次运行使用新输出目录。

```bash
source .venv/bin/activate
bundle=data/phase0/jijia/bundle.json
labels=data/phase0/jijia/labels.json
python -m phase0_eval validate --bundle "$bundle" --labels "$labels"
python -m phase0_eval run --bundle "$bundle" --labels "$labels" \
  --mode stub --out data/phase0/jijia/smoke-02
# 从可用的凭据来源将模型密钥导出为 PHASE0_MODEL_API_KEY。
python -m phase0_eval run --bundle "$bundle" --labels "$labels" \
  --mode live --out data/phase0/jijia/live-02
```

默认模型为 `deepseek-flash`。用 `--model` 和 `--base-url` 选择兼容的 HTTPS 模型服务。仅允许向该服务的 `chat/completions` 发 POST，关闭环境代理、重定向及 SDK 自动重试。程序不读取仓库 `.env`。只把已经脱敏的场景交给模型。遇到损坏输入直接退出，发生模型错误则在保存完整报告后返回状态 2。Stub 只检查回放流程，分数不纳入模型比较。

运行输出：

- `predictions.json` 保存两版原始判断、候选与工具记录、用量、耗时、程序和输入指纹。
- `report.json` 按开发、留出、合成样例统计与标注的差异。旧 dispatcher 的 `create` 仅在评分时投影为项目根，唤醒结果保持不可表达。目标集合按最接近的完整可接受方案评分。
- `review.html` 可离线审阅并导出修订。 `review.xml` 供单独发布成飞书文档。评估过程不会发布文档或读取飞书。

路由以单条消息为单位初始化。多目标输出会在内存账本中模拟挂载和唤醒，登记重复投递、提交前和提交后重试的效果。这些检查验证回放器的行为。

## 回收标注与重跑

在飞书审阅文档中直接编辑标注表。用单独的飞书文档读取流程导出全量 XML 响应并归档为 JSON，然后在远程仓库从文件导入。离线审阅页的 JSON 导出也可作为版本化标注文件。

```bash
python -m phase0_eval.review import-xml --bundle "$bundle" --labels "$labels" \
  --response data/phase0/review/revision.json --out data/phase0/jijia/labels-v2.json
python -m phase0_eval score --bundle "$bundle" --labels data/phase0/jijia/labels-v2.json \
  --predictions data/phase0/jijia/live-01/predictions.json --out data/phase0/jijia/review-v2
python -m phase0_eval compare data/phase0/jijia/live-01/report.json \
  data/phase0/jijia/review-v2/report.json --out data/phase0/jijia/comparison-v2.json
```

只改答案时重新评分即可。任务设定、上下文、模型或提示词变化时，用新的版本文件与输出目录重新运行。标注采纳由审阅者决定，原始草稿与首轮预测分开保留。标注表之外的自由文字建议由维护者整理成新场景或新标注，按文件校验后再评估。

隔离测试使用 `python -m pytest -q phase0_tests`。该目录独立于服务测试夹具，可在无生产配置的环境中运行。
