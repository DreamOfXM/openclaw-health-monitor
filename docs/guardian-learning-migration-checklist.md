# Guardian 学习职责迁移清单

## 1. 目的

本清单用于把 `guardian` 当前承担的 learning / reflection 主责任，逐步迁移为纯监督职责。

目标不是立刻删除所有相关代码，而是：

- 停止把 `guardian` 当作学习引擎
- 保留它作为监督、审计、展示、验收层
- 为 OpenClaw 内部学习循环接管主路径提供迁移顺序

对应总方案：

- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)

执行拆解：

- [learning-execution-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-execution-work-packages.md)

## 2. 当前 Guardian 中仍承担主责任的点

基于当前代码，以下逻辑仍属于“过渡期的主责任”：

### 2.1 learning capture

当前入口：

- `guardian.py` 中 `capture_control_plane_learnings(...)`

当前行为：

- 从 blocked / follow-up outcome 推导 learning
- 直接写入 `State Store.learnings`
- 由外层命名 learning key、title、detail、evidence

问题：

- 这是外层在替 OpenClaw做学习提炼
- learning 来源于控制面观察，而不是 OpenClaw 内部认知产物

### 2.2 reflection run

当前入口：

- `guardian.py` 中 `run_reflection_cycle(...)`

当前行为：

- 直接读取 `STORE.list_learnings(...)`
- 根据 occurrences 和 threshold 做 `pending -> reviewed -> promoted`
- 写 `reflection_runs`

问题：

- promote 决策现在发生在 guardian
- 这和“OpenClaw 自己 reflect / promote”冲突

### 2.3 durable memory 文件写入

当前入口：

- `guardian.write_task_registry_snapshot()`

当前行为：

- 外层生成 `.learnings/*.md`
- 外层生成 `memory/YYYY-MM-DD.md`
- 外层生成 `MEMORY.md`

问题：

- 这使得 durable memory 看起来像 guardian 产物
- 容易把监督层误当成学习主引擎

## 3. 迁移后的 Guardian 目标职责

迁移完成后，guardian 只保留以下职责：

- 读取 OpenClaw 产出的 learning artifacts
- 记录 learning freshness / reflection freshness
- 审计 promoted items 是否存在注入位置
- 展示 backlog / reflection history / reuse evidence
- 继续维护 Task Watcher、DLQ、completed != delivered

换句话说：

- guardian 不再“决定学什么”
- guardian 只负责“检查有没有真的学”

## 4. 迁移清单

### Phase A：明确过渡边界

- [ ] 在代码注释和文档中把 `capture_control_plane_learnings(...)` 标记为 transitional
- [ ] 在代码注释和文档中把 `run_reflection_cycle(...)` 标记为 transitional
- [ ] 在 Dashboard 文案中避免把 guardian 描述成 reflection owner

完成标准：

- 文档和 UI 不再把 guardian 当作学习主责任方

### Phase B：把 capture 降级为 observation export

- [ ] 将 `capture_control_plane_learnings(...)` 从“生成 canonical learning”改成“生成 observation candidates”
- [ ] 为候选项增加字段，明确 `source = guardian_observation`
- [ ] 禁止 guardian 为 OpenClaw 内部经验直接产出最终 promote 结论
- [ ] 保留控制面异常到 watcher/guardrail 规则候选的外层路径

完成标准：

- guardian 输出的是候选观察事实，而不是最终学习判断

### Phase C：停用 guardian promote 决策

- [ ] 把 `run_reflection_cycle(...)` 从默认主路径移除
- [ ] 默认不再由 guardian 定时推动 `pending -> promoted`
- [ ] 允许 guardian 仅记录来自 OpenClaw 的 reflection run 结果
- [ ] `REFLECTION_INTERVAL_SECONDS` / `LEARNING_PROMOTION_THRESHOLD` 从“主配置”降级为兼容项

完成标准：

- reflection / promote 主执行只来自 OpenClaw

### Phase D：停用 guardian 生成 durable memory

- [ ] 停止由 guardian 生成 canonical `.learnings/*.md`
- [ ] 停止由 guardian 生成 canonical `memory/YYYY-MM-DD.md`
- [ ] 停止由 guardian 生成 canonical `MEMORY.md`
- [ ] 保留只读镜像导出或审计快照能力，但必须标记为 derived/exported

完成标准：

- durable memory 的权威文件来自 OpenClaw workspace
- guardian 只读并展示 freshness / diff / export

### Phase E：补强监督指标

- [ ] 增加 `learning_freshness`
- [ ] 增加 `reflection_freshness`
- [ ] 增加 `memory_freshness`
- [ ] 增加 `promoted_items_count`
- [ ] 增加 `reuse_evidence_count`
- [ ] 增加 `repeat_error_trend`

完成标准：

- Dashboard / shared-state 能证明学习是否真的发生

## 5. 具体代码迁移目标

### 5.1 guardian.py

需要处理的函数：

- `capture_control_plane_learnings(...)`
- `run_reflection_cycle(...)`
- `write_task_registry_snapshot()` 中 `.learnings / memory / MEMORY.md` 写入部分

迁移方向：

- 保留 watcher / audit / shared-state / task facts
- 剥离 learning ownership

### 5.2 state_store.py

当前能力：

- `upsert_learning(...)`
- `list_learnings(...)`
- `summarize_learnings(...)`
- `record_reflection_run(...)`
- `list_reflection_runs(...)`

迁移方向：

- 保留为监督存储层
- 为 learning source / origin / injection target / reuse evidence 预留结构
- 不把 store 当成认知判断器

## 6. 风险与注意事项

### 6.1 不能一次性粗暴删除

原因：

- Dashboard 现在依赖 learning summary / reflection history
- shared-state 现在依赖 backlog 输出
- 测试和观测还依赖现有表结构

所以正确方式是：

- 先改 ownership 口径
- 再改默认路径
- 最后清理兼容代码

### 6.2 控制面 learning 仍可保留为外层规则来源

注意区分两类内容：

- `OpenClaw learning`
  - 属于认知学习
- `guardian/watcher rule`
  - 属于外层治理规则

外层仍然可以根据运行异常形成：

- watcher guardrails
- delivery guardrails
- protocol guardrails

但这些不应再被混称为 OpenClaw 的“自我学习”。

## 7. 完成定义

以下条件全部满足，才算迁移完成：

- guardian 不再承担 reflection / promote 主执行
- guardian 不再生成权威的 `MEMORY.md` / `.learnings/*`
- OpenClaw 成为 learning artifacts 的权威生产者
- health-monitor 仍能展示 backlog / reflection / promoted / reuse evidence
- watcher / audit / DLQ / completed != delivered 不受影响
