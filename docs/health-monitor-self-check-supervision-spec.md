# Health Monitor 对 OpenClaw Self-Check 的监督规范

## 1. 目的

定义 health-monitor 如何监督 OpenClaw 内部 `runtime-self-check`。

核心原则：

- 不参与判断
- 不参与恢复决策
- 只消费 OpenClaw 输出的结构化 self-check 事实

关联文档：

- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)
- [openclaw-self-check-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-artifact-schema.md)
- [openclaw-self-check-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-runtime-spec.md)

## 2. 最小监督问题

health-monitor 必须能回答：

1. 最近一次 self-check 什么时候运行
2. 最近一次 self-check 是成功、失败还是跳过
3. 最近一次 recovery 是否成功
4. 最近是否发生 `completed != delivered`
5. 最近是否发生 `delivery_retry`
6. 最近是否存在 stale subagent

## 3. 建议字段

- `last_self_check_at`
- `self_check_freshness`
- `self_check_status`
- `last_self_recovery_at`
- `last_self_recovery_freshness`
- `last_self_recovery_result`
- `delivery_retry_count`
- `completed_not_delivered_count`
- `stale_subagent_count`
- `self_check_artifact_status`
- `recent_event_types`

`self_check_artifact_status` 建议值：

- `ready`
- `invalid`
- `missing`

## 4. shared-state 建议对象

建议新增：

- `self-check-runtime-status.json`
- `self-check-events.json`

### 4.1 self-check-runtime-status.json

```json
{
  "generated_at": 1770200000,
  "env_id": "primary",
  "last_self_check_at": 1770199900,
  "self_check_freshness": 100,
  "self_check_status": "succeeded",
  "last_self_recovery_at": 1770199910,
  "last_self_recovery_freshness": 90,
  "last_self_recovery_result": "delivery_retry_succeeded",
  "delivery_retry_count": 2,
  "completed_not_delivered_count": 1,
  "stale_subagent_count": 0,
  "self_check_artifact_status": "ready",
  "recent_event_types": ["self_check_detected_stall", "self_check_recovery_succeeded"]
}
```

### 4.2 self-check-events.json

```json
{
  "generated_at": 1770200000,
  "env_id": "primary",
  "events": [
    {
      "event_type": "self_check_detected_stall",
      "task_id": "task-1",
      "created_at": 1770199890
    },
    {
      "event_type": "self_check_recovery_succeeded",
      "task_id": "task-1",
      "created_at": 1770199910
    }
  ]
}
```

## 5. API 建议

建议补充到：

- `/api/shared-state`
- `/api/health-acceptance`

### 5.1 `/api/health-acceptance`

增加：

```json
{
  "self_check": {
    "last_self_check_at": 1770199900,
    "self_check_status": "succeeded",
    "last_self_recovery_result": "delivery_retry_succeeded",
    "completed_not_delivered_count": 1
  }
}
```

## 6. Dashboard 建议

运行中心应显示：

- 最近一次 self-check
- 最近一次 self-recovery 结果
- `completed != delivered`
- `delivery_retry_count`
- `stale_subagent_count`

## 7. 完成定义

当以下条件满足时，监督接入完成：

- health-monitor 可读取 OpenClaw self-check 结果
- 外层 UI 能展示 self-check 运行事实
- 外层不会把自己的判断伪装成 OpenClaw 自检结果
