# OpenClaw Self-Check Runtime Spec

## 1. 目的

定义 `runtime-self-check` 的运行方式、输入输出、失败处理和审计要求。

关联文档：

- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)
- [openclaw-self-check-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-artifact-schema.md)
- [openclaw-self-check-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-work-packages.md)

## 2. Job 定义

- 任务名：`runtime-self-check`
- 建议频率：30s - 90s
- 作用范围：活动 session / 活动 task / 活动 subagent
- 不扫描全量历史任务

## 3. 输入

每次运行至少读取：

- active task list
- current session state
- current stage / last progress time
- subagent receipt / pipeline progress
- final reply presence
- delivery status

## 4. 输出

每次运行至少产出：

- `self-check-runtime-status.json`
- `self-check-events.json`
- `self-check-runs.jsonl` 中的一条记录

## 5. 检测器

### 5.1 `silent_stage`

条件：

- 当前 task 仍 active
- 距离上次 progress 超过阈值
- 没有新 receipt 或 stage update

结果：

- 记录 `self_check_detected_stall`
- 进入 `recovering` 前置状态

### 5.2 `no_final_reply`

条件：

- 执行路径显示已进入完成态
- 但没有 final reply artifact

结果：

- 记录 `self_check_detected_no_final_reply`
- 触发 `finalization_retry`

### 5.3 `completed_not_delivered`

条件：

- final 已形成
- delivery state 仍未完成

结果：

- 记录 `self_check_detected_completed_not_delivered`
- 触发 `delivery_retry`

### 5.4 `stale_subagent`

条件：

- subagent 处于活动期
- 长时间无 receipt / progress

结果：

- 记录 `self_check_detected_stale_subagent`
- 触发 `subagent_reconciliation`

## 6. 恢复动作

### 6.1 `session_refresh`

- 重新装载当前 session 状态
- 校准 active task / final state / delivery state

### 6.2 `stage_nudge`

- 轻量提醒当前 coordinator 只同步现状与下一步
- 不开新任务，不重写需求

### 6.3 `finalization_retry`

- 在 `no_final_reply` 场景下重试 finalization

### 6.4 `delivery_retry`

- 在 `completed_not_delivered` 场景下重试 delivery

### 6.5 `subagent_reconciliation`

- 收拢 stale subagent 状态
- 视情况重绑当前活动任务

## 7. 单次运行状态

建议单次 run 状态：

- `succeeded`
- `partial`
- `failed`
- `skipped`

定义：

- `succeeded`: 检测和必要恢复都正常完成
- `partial`: 检测完成，但部分恢复动作失败
- `failed`: 本次运行无法完成检测
- `skipped`: 无 active tasks 或前一轮尚未结束

## 8. 失败处理

### 8.1 读状态失败

- 写一条 `self_check_runs.jsonl` 失败记录
- 不覆盖上一份 `runtime-status.json`

### 8.2 部分恢复失败

- 记录 `self_check_recovery_failed`
- 更新 `last_self_recovery_result`
- 不伪装成成功

### 8.3 输出写盘失败

- 记录运行失败
- 不写不完整的 JSON

## 9. 审计要求

每次运行必须至少记录：

- `run_id`
- `started_at`
- `finished_at`
- `status`
- `checked_task_count`
- `detected.*`
- `recovery_actions.*`
- `result`

## 10. 与 health-monitor 的契约

health-monitor 只读取，不驱动：

- `self-check-runtime-status.json`
- `self-check-events.json`
- `self-check-runs.jsonl`

外层不得：

- 自己补造 self-check 成功结论
- 把 guardian followup 伪装成 OpenClaw 内部自恢复

## 11. 完成定义

当以下条件满足时，runtime spec 层完成：

- OpenClaw self-check 每次运行都有明确输入输出
- 检测器和恢复动作都有结构化记录
- 失败场景不会破坏已有状态文件
- health-monitor 能无歧义消费结果
