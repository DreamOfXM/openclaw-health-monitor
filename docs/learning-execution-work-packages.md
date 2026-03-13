# 学习闭环执行工作包

## 1. 目的

本文件把学习反思重构方案拆成可连续推进的实施工作包。

关联文档：

- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)
- [guardian-learning-migration-checklist.md](/Users/hangzhou/openclaw-health-monitor/docs/guardian-learning-migration-checklist.md)
- [openclaw-learning-implementation-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-implementation-design.md)
- [openclaw-learning-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-artifact-schema.md)
- [openclaw-learning-cron-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-cron-runtime-spec.md)
- [health-monitor-learning-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-learning-supervision-spec.md)

## 2. 工作包总览

建议按 6 个工作包推进：

1. `WP-1` Ownership 收口
2. `WP-2` OpenClaw artifact 协议
3. `WP-3` OpenClaw cron 落地
4. `WP-4` Promote / inject 路径落地
5. `WP-5` 外层监督验收面落地
6. `WP-6` 兼容层收缩与旧逻辑下线

## 3. 工作包定义

### WP-1 Ownership 收口

目标：

- 把所有文档、代码注释、UI 口径统一为：
  - OpenClaw owns learn / reflect / promote / reuse
  - health-monitor owns visibility / audit / verification

涉及：

- `guardian.py`
- Dashboard 文案
- docs/*

交付：

- guardian 相关函数标记为 transitional
- 文档链完整
- 后续实施不再有角色混乱

完成判定：

- 新文档和代码注释不再把 guardian 写成 learning owner

### WP-2 OpenClaw artifact 协议

目标：

- 让 OpenClaw 有一套机器可读的 learning / reflection / promote 记录格式

涉及：

- `.learnings/pending.jsonl`
- `.learnings/promoted.jsonl`
- `.learnings/discarded.jsonl`
- reflection run record
- promoted target record

交付：

- 字段 schema
- 示例文件
- 与 health-monitor 的读取约定

完成判定：

- health-monitor 无需猜测即可读取和展示 learning 状态

### WP-3 OpenClaw cron 落地

目标：

- 把反思与记忆维护正式迁到 OpenClaw 自己的调度体系

涉及 cron：

- `daily-reflection`
- `memory-maintenance`
- `team-rollup`

交付：

- cron 触发条件
- 输入输出定义
- 失败重试与日志位置

完成判定：

- reflection 主执行来自 OpenClaw，而不是 guardian

### WP-4 Promote / inject 路径落地

目标：

- promote 决策必须能进入实际运行载体

允许目标：

- `MEMORY.md`
- `AGENTS.md`
- `Skills/`
- watcher / guardrail rules

交付：

- 注入决策格式
- 目标文件写入策略
- 冲突处理规则
- 回滚与审计记录

完成判定：

- 每个 promoted item 都能指向明确注入位置

### WP-5 外层监督验收面落地

目标：

- health-monitor 不学，但要能证明 OpenClaw 在学

指标：

- `learning_freshness`
- `reflection_freshness`
- `memory_freshness`
- `promoted_items_count`
- `reuse_evidence_count`
- `repeat_error_trend`

交付：

- Dashboard 卡片
- shared-state 字段
- 验收接口字段

完成判定：

- 用户可以直接判断“有没有学”和“学了是否有用”

### WP-6 兼容层收缩与旧逻辑下线

目标：

- 把 guardian 中过渡性 learning/reflection 主逻辑降到最小

涉及：

- `capture_control_plane_learnings(...)`
- `run_reflection_cycle(...)`
- guardian 对 `.learnings / memory / MEMORY.md` 的权威写入

交付：

- 默认关闭 guardian promote 主路径
- 旧逻辑只保留兼容或回放用途
- 文档标记为 legacy bridge

完成判定：

- guardian 不再承担 canonical learn / reflect / promote

## 4. 推荐实施顺序

建议按下面顺序连续推进：

### 第一段

- `WP-1`
- `WP-2`

### 第二段

- `WP-3`
- `WP-4`

### 第三段

- `WP-5`
- `WP-6`

## 5. 关键依赖

### 对 OpenClaw 的依赖

- 需要有可执行的 cron 入口
- 需要有稳定写文件能力
- 需要有技能/记忆注入机制

### 对 health-monitor 的依赖

- 需要能读取 OpenClaw workspace artifacts
- 需要把 freshness / reuse / promoted target 展示出来
- 需要保留 watcher / audit / DLQ 不受影响

## 6. 风险

### 风险 1：只改文档不改运行路径

结果：

- 口径对了，但行为没变

### 风险 2：过快删除 guardian 兼容层

结果：

- 学习展示面中断
- dashboard/shared-state 回归

### 风险 3：OpenClaw 只写记录，不做 reuse

结果：

- 仍然是“有反思、没效果”

## 7. 最终完成定义

全部工作包完成后，应达到：

- OpenClaw 是权威学习引擎
- health-monitor 是权威学习监督面
- watcher / audit / completed != delivered 继续稳定工作
- 学习是否发生、是否 promote、是否 reuse，都能被验证
