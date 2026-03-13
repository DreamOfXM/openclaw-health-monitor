# OpenClaw 学习产物 Schema

## 1. 目的

本文件定义 OpenClaw 学习闭环的机器可读产物格式。

目标：

- 让 OpenClaw 有稳定写入协议
- 让 health-monitor 无需猜测即可读取和审计
- 让 backlog / promote / reuse 能形成可验证链路

关联文档：

- [openclaw-learning-implementation-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-implementation-design.md)
- [learning-execution-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-execution-work-packages.md)

## 2. 目录与文件

推荐最小机器可读文件集：

- `.learnings/pending.jsonl`
- `.learnings/promoted.jsonl`
- `.learnings/discarded.jsonl`
- `.learnings/reflection-runs.jsonl`
- `.learnings/reuse-evidence.jsonl`

人类可读镜像文件：

- `.learnings/ERRORS.md`
- `.learnings/LEARNINGS.md`
- `.learnings/FEATURE_REQUESTS.md`

规则：

- `jsonl` 是权威机器记录
- `md` 是派生阅读视图
- health-monitor 优先读取 `jsonl`

## 3. 通用字段约定

所有记录建议共用以下基础字段：

- `schema_version`: 当前 schema 版本，例如 `learning.v1`
- `record_id`: 当前记录唯一 id
- `env_id`: `primary`
- `created_at`: unix timestamp
- `updated_at`: unix timestamp
- `source`: `openclaw`

## 4. pending.jsonl

用途：

- 记录尚未完成 reflection 决策的 learning

每行一条 JSON，建议字段：

```json
{
  "schema_version": "learning.pending.v1",
  "record_id": "lrn_20260312_001",
  "env_id": "primary",
  "source": "openclaw",
  "learning_id": "lrn_20260312_001",
  "status": "pending",
  "category": "delivery_failure",
  "summary": "final reply missing after implementation completion",
  "detail": "task completed internally twice but user-visible delivery did not happen",
  "source_task_id": "task-123",
  "source_session_id": "session-abc",
  "source_agent": "main",
  "source_run_id": "run-001",
  "positive_evidence": [
    {"type": "log", "ref": "logs/openclaw.log#receipt-dev-completed"},
    {"type": "watcher", "ref": "watcher:task-123", "note": "completed_not_delivered"}
  ],
  "negative_evidence": [
    {"type": "counterexample", "ref": "task-099", "note": "similar task delivered normally"}
  ],
  "occurrences": 3,
  "stability_score": 0.82,
  "priority": "high",
  "candidate_targets": ["Skills", "MEMORY.md"],
  "tags": ["delivery", "finalization", "receipt"],
  "created_at": 1770000000,
  "updated_at": 1770003600
}
```

必填字段：

- `schema_version`
- `record_id`
- `learning_id`
- `status`
- `category`
- `summary`
- `source_task_id`
- `source_session_id`
- `occurrences`
- `created_at`
- `updated_at`

## 5. promoted.jsonl

用途：

- 记录已经完成 promote 决策并进入注入阶段或已注入完成的 learning

建议字段：

```json
{
  "schema_version": "learning.promoted.v1",
  "record_id": "lrn_20260312_001",
  "env_id": "primary",
  "source": "openclaw",
  "learning_id": "lrn_20260312_001",
  "status": "promoted",
  "category": "delivery_failure",
  "summary": "final reply missing after implementation completion",
  "decision": "promote",
  "decision_reason": "repeated delivery loss with strong evidence",
  "source_task_ids": ["task-123", "task-124", "task-130"],
  "occurrences": 3,
  "promoted_by_run_id": "refl_20260312_daily",
  "injection_target": {
    "type": "Skills",
    "path": "Skills/delivery-finalization.md",
    "status": "applied",
    "applied_at": 1770007200
  },
  "expected_outcome": "reduce completed_not_delivered failures",
  "created_at": 1770000000,
  "updated_at": 1770007200
}
```

必填字段：

- `schema_version`
- `record_id`
- `learning_id`
- `status`
- `decision`
- `promoted_by_run_id`
- `injection_target.type`
- `injection_target.path`
- `created_at`
- `updated_at`

## 6. discarded.jsonl

用途：

- 记录被 reflection 判定为不再保留的 learning

建议字段：

```json
{
  "schema_version": "learning.discarded.v1",
  "record_id": "lrn_20260310_009",
  "env_id": "primary",
  "source": "openclaw",
  "learning_id": "lrn_20260310_009",
  "status": "discarded",
  "category": "noise",
  "summary": "single isolated timeout without repeat evidence",
  "discard_reason": "not stable enough to promote",
  "discarded_by_run_id": "refl_20260312_daily",
  "occurrences": 1,
  "created_at": 1769900000,
  "updated_at": 1770007200
}
```

## 7. reflection-runs.jsonl

用途：

- 记录每次 reflection cron 的执行结果

建议字段：

```json
{
  "schema_version": "learning.reflection-run.v1",
  "record_id": "refl_20260312_daily",
  "env_id": "primary",
  "source": "openclaw",
  "run_id": "refl_20260312_daily",
  "run_type": "daily-reflection",
  "status": "succeeded",
  "scope": {
    "pending_count": 12,
    "agents": ["main", "dev", "test"]
  },
  "decisions": {
    "kept": 6,
    "promoted": 3,
    "discarded": 3
  },
  "promoted_targets": {
    "MEMORY.md": 1,
    "AGENTS.md": 1,
    "Skills": 1,
    "guardrail_rules": 0
  },
  "duration_ms": 1840,
  "started_at": 1770003600,
  "finished_at": 1770003602,
  "created_at": 1770003602,
  "updated_at": 1770003602
}
```

## 8. reuse-evidence.jsonl

用途：

- 记录 promoted knowledge 在后续任务中被命中和产生效果的证据

建议字段：

```json
{
  "schema_version": "learning.reuse-evidence.v1",
  "record_id": "reuse_20260313_001",
  "env_id": "primary",
  "source": "openclaw",
  "learning_id": "lrn_20260312_001",
  "source_promoted_run_id": "refl_20260312_daily",
  "reused_in_task_id": "task-201",
  "reused_in_session_id": "session-xyz",
  "reuse_mode": "task_start_injection",
  "injection_target": {
    "type": "Skills",
    "path": "Skills/delivery-finalization.md"
  },
  "observed_effect": "final reply delivered successfully",
  "effect_score": 0.9,
  "created_at": 1770090000,
  "updated_at": 1770090000
}
```

## 9. health-monitor 读取约定

health-monitor 应按以下顺序读取：

1. 读取 `reflection-runs.jsonl`
2. 读取 `pending.jsonl`
3. 读取 `promoted.jsonl`
4. 读取 `discarded.jsonl`
5. 读取 `reuse-evidence.jsonl`

由此派生：

- `learning_freshness`
- `reflection_freshness`
- `memory_freshness`
- `promoted_items_count`
- `reuse_evidence_count`
- `repeat_error_trend`

如果 `jsonl` 缺失：

- health-monitor 应显示 `artifact_missing`
- 不应自动把 `md` 视为权威来源

## 10. 向后兼容规则

迁移期间允许：

- health-monitor 继续从 SQLite 读取旧 learning / reflection 记录
- 但 UI 上必须标明：
  - `legacy_store_only`
  - `openclaw_artifact_missing`

一旦 OpenClaw artifact 可用：

- `jsonl` 优先级高于 SQLite
- `md` 仅作为辅助阅读视图

## 11. 完成定义

以下条件满足后，才算 `WP-2` 完成：

- OpenClaw 能产出 5 类 `jsonl` 文件
- health-monitor 能按 schema 稳定读取
- backlog / promoted / reflection / reuse 都可直接展示
- UI 不再依赖猜测或解析自由文本来判断学习状态
