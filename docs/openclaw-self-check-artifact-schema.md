# OpenClaw Self-Check Artifact Schema

## 1. 目的

定义 OpenClaw 内部 `runtime-self-check` 的机器可读产物格式。

目标：

- 让 OpenClaw 自检结果可稳定落盘
- 让 health-monitor 不需要猜测语义
- 让 detect / recover / result 形成可审计链路

关联文档：

- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)
- [openclaw-self-check-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-work-packages.md)
- [health-monitor-self-check-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-self-check-supervision-spec.md)

## 2. 目录与文件

推荐最小产物目录：

- `shared-context/self-check/self-check-runtime-status.json`
- `shared-context/self-check/self-check-events.json`
- `shared-context/self-check/self-check-runs.jsonl`

规则：

- `runtime-status.json` 是最近状态快照
- `events.json` 是最近事件窗口
- `runs.jsonl` 是长期运行记录

## 3. 通用字段约定

所有记录建议包含：

- `schema_version`
- `env_id`
- `created_at`
- `updated_at`
- `source = openclaw`

## 4. self-check-runtime-status.json

用途：

- 暴露最近一次 self-check 的整体状态和关键计数

建议结构：

```json
{
  "schema_version": "self_check.runtime.v1",
  "env_id": "primary",
  "source": "openclaw",
  "last_self_check_at": 1770201000,
  "self_check_status": "succeeded",
  "last_self_recovery_at": 1770201012,
  "last_self_recovery_result": "delivery_retry_succeeded",
  "delivery_retry_count": 2,
  "completed_not_delivered_count": 1,
  "stale_subagent_count": 0,
  "active_stalled_task_count": 1,
  "self_check_artifact_status": "ready",
  "recent_event_types": [
    "self_check_detected_stall",
    "self_check_delivery_retry",
    "self_check_recovery_succeeded"
  ],
  "updated_at": 1770201012
}
```

必填字段：

- `schema_version`
- `env_id`
- `last_self_check_at`
- `self_check_status`
- `self_check_artifact_status`
- `updated_at`

## 5. self-check-events.json

用途：

- 提供最近一小段 self-check 事件窗口给外层面板和审计使用

建议结构：

```json
{
  "schema_version": "self_check.events.v1",
  "env_id": "primary",
  "source": "openclaw",
  "generated_at": 1770201012,
  "events": [
    {
      "event_type": "self_check_detected_stall",
      "task_id": "task-1",
      "session_id": "session-a",
      "reason": "silent_stage",
      "stage": "implementation",
      "created_at": 1770201000
    },
    {
      "event_type": "self_check_delivery_retry",
      "task_id": "task-1",
      "session_id": "session-a",
      "reason": "completed_not_delivered",
      "created_at": 1770201008
    },
    {
      "event_type": "self_check_recovery_succeeded",
      "task_id": "task-1",
      "session_id": "session-a",
      "result": "delivery_retry_succeeded",
      "created_at": 1770201012
    }
  ]
}
```

## 6. self-check-runs.jsonl

用途：

- 保留每次 `runtime-self-check` 运行记录，便于回放和趋势分析

每行一条 JSON，建议结构：

```json
{
  "schema_version": "self_check.run.v1",
  "run_id": "sc_20260312_001",
  "env_id": "primary",
  "source": "openclaw",
  "status": "succeeded",
  "started_at": 1770201000,
  "finished_at": 1770201012,
  "checked_task_count": 3,
  "detected": {
    "silent_stage": 1,
    "no_final_reply": 0,
    "completed_not_delivered": 1,
    "stale_subagent": 0
  },
  "recovery_actions": {
    "session_refresh": 1,
    "stage_nudge": 0,
    "finalization_retry": 0,
    "delivery_retry": 1,
    "subagent_reconciliation": 0
  },
  "result": "delivery_retry_succeeded",
  "updated_at": 1770201012
}
```

## 7. 状态与枚举

### 7.1 `self_check_status`

建议值：

- `succeeded`
- `partial`
- `failed`
- `skipped`
- `missing`

### 7.2 `event_type`

建议值：

- `self_check_started`
- `self_check_detected_stall`
- `self_check_detected_no_final_reply`
- `self_check_detected_completed_not_delivered`
- `self_check_detected_stale_subagent`
- `self_check_recovery_started`
- `self_check_recovery_succeeded`
- `self_check_recovery_failed`
- `self_check_delivery_retry`
- `self_check_blocked`

### 7.3 `reason`

建议值：

- `silent_stage`
- `no_final_reply`
- `completed_not_delivered`
- `stale_subagent`

## 8. health-monitor 读取约定

health-monitor 应按以下优先级读取：

1. `self-check-runtime-status.json`
2. `self-check-events.json`
3. `self-check-runs.jsonl`

如果这些文件不存在：

- 外层应显示 `self_check_artifact_status = missing`
- 不应伪造 OpenClaw 已具备 self-check 能力

## 9. 完成定义

以下条件满足后，`SC-1` 的 schema 层完成：

- OpenClaw 有稳定的 self-check artifact 结构
- health-monitor 能稳定解析 freshness / counters / recent event types
- 外层不再依赖自由文本推测 self-check 结果
