# OpenClaw 内部 Self-Check / Heartbeat 设计

## 1. 目标

这个设计解决的问题不是“外层替 OpenClaw 补一句话”，而是：

- OpenClaw 自己知道自己卡了
- OpenClaw 自己判断当前是静默、阻塞、还是丢了最终回复
- OpenClaw 自己触发最小恢复动作
- health-monitor 只负责监督这些动作有没有真的发生

## 2. 设计原则

- heartbeat 必须在 OpenClaw 内部运行
- heartbeat 不接管主消息链
- heartbeat 不承担学习 / 反思主路径
- heartbeat 只做运行自检与恢复触发
- 所有触发、判断、恢复都必须留痕

## 3. 关注的最小问题集

内部 self-check 只盯四类问题：

1. `silent_stage`
   - 某阶段长时间没有新进展
2. `no_final_reply`
   - 内部执行完成，但最终回复未形成
3. `completed_not_delivered`
   - 已有 final / completed 事实，但未真正送达用户
4. `stale_subagent`
   - 子 agent 长时间不再产出 receipt / progress

## 4. 不负责的事

这个 heartbeat 不负责：

- 学习 capture
- promote 决策
- reflection 总结
- 外层消息补发策略
- 对用户做额外解释型对话

这些仍分别属于：

- OpenClaw 主学习循环
- OpenClaw 主对话链
- health-monitor 外层监督与验收

## 5. 运行方式

建议作为 OpenClaw 内部一个轻量 cron / scheduler job：

- 名称可为 `runtime-self-check`
- 频率建议 30s - 90s 一次
- 只检查活动 session / 活动任务
- 不扫描全量历史会话

## 6. 输入信号

Self-check 至少读取：

- 当前 session state
- 当前 task state
- subagent receipt / progress
- final reply state
- delivery state
- 最后一次可见进展时间

它不应依赖外层 guardian 解析日志后再告诉它。

## 7. 最小状态机

建议内部维护如下状态：

- `healthy`
- `watching`
- `stalled`
- `recovering`
- `blocked`
- `delivered`

转换示意：

- `healthy -> watching`
  - 接近静默阈值
- `watching -> stalled`
  - 超过静默阈值且无新 receipt
- `stalled -> recovering`
  - 触发最小恢复动作
- `recovering -> healthy`
  - 收到新进展
- `recovering -> blocked`
  - 恢复失败且确认阻塞
- `healthy/recovering -> delivered`
  - 最终回复形成并送达

## 8. 最小恢复动作

只允许非常克制的内部动作：

### 8.1 session refresh

- 重读当前 session 上下文
- 确认当前是否仍存在 active task

### 8.2 stage nudge

- 向当前 active coordinator 发内部提醒
- 只允许“同步当前任务现状 / 明确下一步”

### 8.3 finalization retry

- 若已完成执行但无 final reply，触发 finalization 补跑

### 8.4 delivery retry

- 若已形成 final 但未 delivered，触发 delivery retry

### 8.5 subagent reconciliation

- 若子 agent stale，尝试收拢其最后状态或重新绑定活动任务

## 9. 必须记录的事件

每次 self-check 都要留下结构化事件：

- `self_check_started`
- `self_check_detected_stall`
- `self_check_recovery_started`
- `self_check_recovery_succeeded`
- `self_check_recovery_failed`
- `self_check_delivery_retry`
- `self_check_blocked`

这些记录应成为 health-monitor 的监督输入。

## 10. 与 health-monitor 的关系

health-monitor 不参与判断，只消费结果：

- 最近一次 self-check 时间
- 最近一次 stall 检测
- 最近一次 recovery 结果
- 是否发生 completed != delivered
- 是否发生 delivery retry

所以外层应该显示：

- `last_self_check_at`
- `self_check_status`
- `last_self_recovery_at`
- `last_self_recovery_result`
- `delivery_retry_count`

## 11. 验收标准

完成后必须能证明：

- OpenClaw 自己能发现静默卡住
- OpenClaw 自己能触发最小恢复动作
- OpenClaw 自己能区分 completed 与 delivered
- health-monitor 不介入判断，但能看见全过程

## 12. 实施顺序

建议顺序：

1. 在 OpenClaw 内部落 `runtime-self-check`
2. 先做 `silent_stage / no_final_reply / completed_not_delivered` 三类检测
3. 再做 `finalization retry / delivery retry`
4. 最后接 `stale_subagent reconciliation`

## 13. 与外层 heartbeat 的关系

外层 heartbeat 不是目标路径。

如果保留，也只能是：

- 可选兼容层
- 明确低优先级
- 不得掩盖 OpenClaw 内部 self-check 缺失

目标架构永远是：

- OpenClaw 自检、自恢复
- health-monitor 监督、验收

配套执行文档：

- [openclaw-self-check-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-work-packages.md)
- [health-monitor-self-check-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-self-check-supervision-spec.md)
- [openclaw-self-check-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-artifact-schema.md)
- [openclaw-self-check-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-runtime-spec.md)
- [openclaw-self-check-detector-rules.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-detector-rules.md)
