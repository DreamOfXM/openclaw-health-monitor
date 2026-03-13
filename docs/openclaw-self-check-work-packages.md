# OpenClaw Self-Check 执行工作包

## 1. 目的

把 `HM-404 OpenClaw 内部 Self-Check / Heartbeat` 拆成可实施工作包。

关联文档：

- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)
- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)
- [openclaw-self-check-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-artifact-schema.md)
- [openclaw-self-check-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-runtime-spec.md)

## 2. 工作包总览

建议按 5 个工作包推进：

1. `SC-1` 内部状态与事件协议
2. `SC-2` 检测器落地
3. `SC-3` 最小恢复动作落地
4. `SC-4` 外层监督契约接入
5. `SC-5` 验收与回归场景

## 3. 工作包定义

### SC-1 内部状态与事件协议

目标：

- 在 OpenClaw 内部统一 self-check 的状态与事件格式

最小状态：

- `healthy`
- `watching`
- `stalled`
- `recovering`
- `blocked`
- `delivered`

最小事件：

- `self_check_started`
- `self_check_detected_stall`
- `self_check_recovery_started`
- `self_check_recovery_succeeded`
- `self_check_recovery_failed`
- `self_check_delivery_retry`
- `self_check_blocked`

完成标准：

- health-monitor 不用猜语义，就能消费这些结果

### SC-2 检测器落地

目标：

- 让 OpenClaw 内部先具备最关键的自检能力

第一批检测器：

- `silent_stage`
- `no_final_reply`
- `completed_not_delivered`

第二批检测器：

- `stale_subagent`

完成标准：

- OpenClaw 自己能发现最常见的静默和交付缺口

### SC-3 最小恢复动作落地

目标：

- 让 OpenClaw 不只是“知道卡住”，还会做最小恢复

动作：

- `session_refresh`
- `stage_nudge`
- `finalization_retry`
- `delivery_retry`
- `subagent_reconciliation`

约束：

- 不允许新开任务
- 不允许改写用户需求
- 不允许把外层 guardian 当决策中心

完成标准：

- 检测到问题后，OpenClaw 至少能尝试一次结构化恢复

### SC-4 外层监督契约接入

目标：

- health-monitor 能直接监督 OpenClaw self-check 是否运行、结果如何

最小字段：

- `last_self_check_at`
- `self_check_status`
- `last_self_recovery_at`
- `last_self_recovery_result`
- `delivery_retry_count`
- `completed_not_delivered_count`
- `stale_subagent_count`

完成标准：

- Dashboard / shared-state 能看见 self-check 运行事实

### SC-5 验收与回归场景

目标：

- 形成固定回归场景，证明 OpenClaw 自检/自恢复有效

场景：

1. 阶段静默后自动检测
2. 已完成但 final 未形成
3. final 已形成但未 delivered
4. 子 agent stale 后被收拢或重试
5. 自恢复失败后进入 blocked 且外层可见

完成标准：

- 每个场景都能看到 detect -> recover -> result 的完整链路

## 4. 推荐顺序

优先顺序：

1. `SC-1`
2. `SC-2`
3. `SC-3` 中的 `finalization_retry / delivery_retry`
4. `SC-4`
5. `SC-5`
6. 再做 `stale_subagent reconciliation`

## 5. 完成定义

全部完成后，必须满足：

- OpenClaw 内部自检成为主路径
- health-monitor 只做外层监督
- completed / delivered / recovery 之间关系可追踪
- 不再依赖外层 heartbeat 假装“系统知道自己卡住了”
