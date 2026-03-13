# OpenClaw 初始化与自进化验收清单

本清单用于对照 [openclaw-bootstrap-evolve-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-bootstrap-evolve-spec.md) 做逐条验收与回放。

## 1. 初始化验收

- `openclaw.json` 存在时，默认采用“合并补齐”而不是强制覆盖
- 缺失的 context lifecycle 基线项会被补齐
- 已有且达标的配置不会被改写
- `model/provider/auth/base_url` 不会被自动覆盖
- 工作区中存在：
  - `SOUL.md`
  - `AGENTS.md`
  - `MEMORY.md`
  - `memory/`
  - `.learnings/`
  - `shared-context/`

## 2. 上下文生命周期验收

- Dashboard 能显示 `ready / degraded / not_ready`
- 每个检查项都能看到 `actual / expected / detail`
- shared-state 中存在：
  - `context-lifecycle-baseline.json`
  - `bootstrap-status.json`

## 3. 学习闭环验收

- learning 主写入由 OpenClaw 产生
- reflection run 由 OpenClaw cron 产生并可记录
- promote 决策由 OpenClaw 产出并可追溯
- `MEMORY.md` 会更新且可追溯
- promoted item 必须能说明注入位置：
  - `MEMORY.md`
  - `AGENTS.md`
  - `Skills`
  - watcher / guardrail rules
- `.learnings/ERRORS.md`
- `.learnings/LEARNINGS.md`
- `.learnings/FEATURE_REQUESTS.md`
  三个文件内容与 state store 一致

## 3.1 学习监督验收

- Dashboard / shared-state 能看到 learning backlog
- Dashboard / shared-state 能看到 reflection history
- Dashboard / shared-state 能看到 promoted items
- health-monitor 能判断今天 reflection cron 是否执行
- health-monitor 能判断 `MEMORY.md` 是否更新
- health-monitor 能显示重复问题后续是否下降

## 4. shared-context 验收

- `shared-context/intel/`
- `shared-context/status/`
- `shared-context/job-status/`
- `shared-context/monitor-tasks/`
  结构存在
- `tech-radar.json` 存在
- 关键共享状态可导出到 `data/shared-state/`

## 5. 任务监督器验收

- `shared-context/monitor-tasks/tasks.jsonl` 可被读取并同步
- `completed` 与 `delivered` 明确区分
- `dlq.jsonl` 能被识别为隔离失败任务
- Dashboard/shared-state 能看到 watcher 摘要

## 6. 回放验收

至少能基于以下材料回放问题：

- task registry snapshot
- bootstrap status
- context readiness
- learning backlog
- reflection history
- watcher summary
- watcher DLQ

回放时必须能回答：

- 初始化是否完成
- 配置是否漂移
- 学习是否沉淀
- 学习是否真的发生在 OpenClaw 内部
- 任务是否完成但未投递
- 哪个环节失效
