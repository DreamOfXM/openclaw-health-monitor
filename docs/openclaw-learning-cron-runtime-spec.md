# OpenClaw 学习 Cron 运行规范

## 1. 目的

本文件把 OpenClaw 侧三类学习 cron 的运行方式定义为可实施规范。

关联文档：

- [openclaw-learning-implementation-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-implementation-design.md)
- [openclaw-learning-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-artifact-schema.md)
- [learning-execution-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-execution-work-packages.md)

## 2. 三类 cron

- `daily-reflection`
- `memory-maintenance`
- `team-rollup`

## 3. daily-reflection

### 3.1 目标

- 审阅 pending learnings
- 聚合重复模式
- 做 keep / promote / discard 决策
- 输出 reflection run 记录

### 3.2 输入

- `.learnings/pending.jsonl`
- 最近任务结果
- 最近错误日志
- 最近 watcher / delivery 异常样本

### 3.3 输出

- `.learnings/promoted.jsonl`
- `.learnings/discarded.jsonl`
- `.learnings/reflection-runs.jsonl`
- patch plan 或直接注入记录

### 3.4 成功条件

- 至少生成一条 reflection run 记录
- 对扫描范围内条目都给出 keep / promote / discard 之一
- 不产生 schema 不合法记录

### 3.5 失败处理

- 如果输入文件损坏：
  - 记录 `status=failed`
  - 写入 error summary
  - 不修改 promoted/discarded 文件
- 如果部分条目异常：
  - 记录 `status=partial`
  - 标记 failed item count

### 3.6 审计要求

- 每次运行必须记录：
  - `run_id`
  - `started_at`
  - `finished_at`
  - `status`
  - `pending_count`
  - `promoted_count`
  - `discarded_count`
  - `error_count`

## 4. memory-maintenance

### 4.1 目标

- 压缩 `MEMORY.md`
- 将当日细节归档到 `memory/YYYY-MM-DD.md`
- 清理冗余和重复原则

### 4.2 输入

- `MEMORY.md`
- `.learnings/promoted.jsonl`
- 最近 reflection runs
- 当前 `Skills/` / `AGENTS.md` 变更结果

### 4.3 输出

- 更新后的 `MEMORY.md`
- 新的 `memory/YYYY-MM-DD.md`
- maintenance run record

### 4.4 成功条件

- `MEMORY.md` 保持简洁
- 新 promote 内容进入正确位置
- 归档文件可追溯当天变更

### 4.5 失败处理

- 如果 `MEMORY.md` 写入失败：
  - 不覆盖原文件
  - 记录 failed status
  - 记录 patch 计划供下次重试

### 4.6 审计要求

- 记录：
  - memory before/after hash
  - archived file path
  - merged promoted count
  - rejected noise count

## 5. team-rollup

### 5.1 目标

- 汇总多 agent 的 reflection 结果
- 提炼团队级协作规则
- 更新 `AGENTS.md` 或团队级 `Skills`

### 5.2 输入

- 最近 reflection runs
- agent-specific learnings
- 协作失败案例
- 交付协议问题样本

### 5.3 输出

- 更新后的 `AGENTS.md`
- 新增或更新的 `Skills/`
- rollup run record

### 5.4 成功条件

- 团队级规则与技能更新可追溯
- 至少有明确的 kept / promoted / rejected 结论

### 5.5 失败处理

- 如果 `AGENTS.md` 冲突：
  - 记录 conflict status
  - 生成 pending patch plan
- 如果 `Skills/` 写入失败：
  - 保留 rollup record
  - 不标记为 applied

## 6. 运行频率建议

- `daily-reflection`: 每日固定时段 1 次
- `memory-maintenance`: 每日 1 次，晚于 `daily-reflection`
- `team-rollup`: 每日或每周 1 次，视 agent 协作强度而定

约束：

- 同类 cron 不允许并发跑多个实例
- 如果上一轮还未结束，新一轮应直接 skip 并记录原因

## 7. 运行状态与退出码

建议统一状态：

- `succeeded`
- `partial`
- `failed`
- `skipped`
- `conflict`

建议退出码：

- `0`: succeeded
- `10`: partial
- `20`: skipped
- `30`: conflict
- `50`: failed

## 8. health-monitor 的监督字段

health-monitor 至少要能派生出：

- `last_daily_reflection_at`
- `last_memory_maintenance_at`
- `last_team_rollup_at`
- `daily_reflection_status`
- `memory_maintenance_status`
- `team_rollup_status`
- `last_promoted_count`
- `last_discarded_count`
- `last_reuse_evidence_count`

## 9. shared-state 派生建议

建议新增或补强以下派生对象：

- `learning-runtime-status.json`
- `reflection-freshness.json`
- `memory-freshness.json`
- `reuse-evidence-summary.json`

## 10. 完成定义

以下条件满足后，才算 `WP-3` 进入可实施状态：

- 三类 cron 都有明确输入/输出/失败处理
- 每类 cron 都有统一 run record
- health-monitor 能从 run record 派生 freshness 与状态
- 即使运行失败，也不会破坏原有 memory / learning artifacts
