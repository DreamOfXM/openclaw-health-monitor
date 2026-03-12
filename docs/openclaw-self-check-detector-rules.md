# OpenClaw Self-Check Detector Rules

## 1. 目的

把 `SC-2` 的四类检测器细化成可直接编码的规则清单。

关联文档：

- [openclaw-self-check-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-runtime-spec.md)
- [openclaw-self-check-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-artifact-schema.md)

## 2. `silent_stage`

触发条件：

- task 仍处于 active 状态
- `now - last_progress_at >= silent_stage_threshold`
- 最近窗口内没有新的 receipt / stage update
- task 尚未进入 final / delivered

输出：

- `reason = silent_stage`
- `event_type = self_check_detected_stall`
- `stage = current_stage`

排除条件：

- task 已 completed 且 delivered
- task 已进入 blocked terminal state

## 3. `no_final_reply`

触发条件：

- 执行阶段已完成
- final reply artifact 不存在
- 距离最后一次完成信号超过 `final_reply_grace`

输出：

- `reason = no_final_reply`
- `event_type = self_check_detected_no_final_reply`

排除条件：

- final reply 已存在
- 任务尚未到达执行完成态

## 4. `completed_not_delivered`

触发条件：

- final reply 已形成
- delivery state 不是 delivered
- 距离 final 形成已超过 `delivery_grace`

输出：

- `reason = completed_not_delivered`
- `event_type = self_check_detected_completed_not_delivered`

排除条件：

- delivery 已成功
- final 仍未形成

## 5. `stale_subagent`

触发条件：

- subagent 仍绑定在 active task 上
- `now - subagent_last_progress_at >= stale_subagent_threshold`
- 没有新的 receipt / result / exit signal

输出：

- `reason = stale_subagent`
- `event_type = self_check_detected_stale_subagent`

排除条件：

- subagent 已正常结束
- task 已 terminal

## 6. 优先级顺序

建议检测优先级：

1. `completed_not_delivered`
2. `no_final_reply`
3. `silent_stage`
4. `stale_subagent`

原因：

- 优先解决接近最终交付的问题
- 再处理阶段静默与子 agent 问题

## 7. 完成定义

完成后，开发实现应满足：

- 同一 task 在同一轮只命中最主要的 detector
- detector 输出有统一 reason/event_type
- health-monitor 能直接消费 detector 结果
