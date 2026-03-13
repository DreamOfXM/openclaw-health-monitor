# OpenClaw 主闭环工程改造清单

## 1. 文档目的

本清单把“`OpenClaw` 主闭环 + `Health Monitor` 外层监督”方案拆成可直接进入开发的工程改造项。

核心原则只有两条：

- 闭环真相在 `OpenClaw`
- 闭环监督在 `Health Monitor`

本方案重点解决：

- 主任务被短 follow-up 冲散
- 子链完成但主链未采纳
- 已完成但未正式收口
- 已收口但未成功送达用户
- `health-monitor` 逐步长成第二个 orchestrator

关联文档：

- [product-architecture.md](/Users/hangzhou/openclaw-health-monitor/docs/product-architecture.md)
- [product-roadmap-execution.md](/Users/hangzhou/openclaw-health-monitor/docs/product-roadmap-execution.md)
- [product-backlog.md](/Users/hangzhou/openclaw-health-monitor/docs/product-backlog.md)
- [openclaw-main-closure-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-main-closure-work-packages.md)
- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)
- [openclaw-self-check-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-work-packages.md)

---

## 2. 非目标

本轮不做：

- 让 `health-monitor` 代替 OpenClaw 宣布 completion / adoption / finalization
- 继续堆更多短句 heuristics 来掩盖主闭环缺失
- 自动 reopen 已 final 的任务
- 在 `health-monitor` 内重建第二套 task orchestrator

---

## 3. 核心对象模型

## 3.1 RootTask

表示用户真正关心的主任务，而不是某个 agent 调用。

建议字段：

```text
root_task_id
session_key
user_goal_summary
task_kind                # delivery | analysis | calculation | ops | mixed
origin_message_id
reply_to_message_id
parent_root_task_id
superseded_by_root_task_id
status                   # active | completed | blocked | cancelled | superseded
foreground_priority
current_workflow_run_id
finalization_id
final_outcome
created_at
updated_at
finalized_at
```

约束：

- 一个用户主任务只能有一个 `root_task_id`
- follow-up 默认绑定当前 foreground `root_task_id`
- `RootTask` 终态只能由 `finalizer` 写入

## 3.2 WorkflowRun

表示围绕某个 `RootTask` 的一次执行尝试。

```text
workflow_run_id
root_task_id
parent_workflow_run_id
starter_agent
workflow_type            # pm_dev_test | calculator_verifier | ops_analysis | direct_main
trigger_reason           # initial_dispatch | retry | patch_run | verification_run
status                   # running | completed | blocked | cancelled
started_at
ended_at
```

关键规则：

- `WorkflowRun completed != RootTask completed`
- 一个 `RootTask` 可以有多个 `WorkflowRun`

## 3.3 Receipt

表示执行链里的结构化证据。

```text
receipt_id
idempotency_key
root_task_id
workflow_run_id
agent
phase
action
evidence
artifacts
created_at
adoption_status          # pending | adopted | rejected | superseded
adopted_by
adopted_at
superseded_by_receipt_id
late_result              # true | false
```

关键规则：

- `receipt arrived != receipt adopted`
- 只有 `adopted` receipt 才能推进主任务事实链
- `idempotency_key` 用于去重、重放保护、迟到重发保护

推荐幂等键：

```text
hash(root_task_id, workflow_run_id, agent, phase, action, evidence_hash)
```

## 3.4 FinalizationRecord

表示主任务最终收口决定。

```text
finalization_id
root_task_id
trigger_reason           # workflow_completed | blocked_confirmed | user_cancelled | superseded | manual_finalize
final_status             # completed | blocked | cancelled | superseded
delivery_status          # finalized | delivered | delivery_failed
selected_receipts
selected_artifacts
user_visible_summary
finalized_by
finalized_at
delivered_at
delivery_error
reopenable
reopened_from_finalization_id
```

关键规则：

- `final_status` 解决“是否闭环”
- `delivery_status` 解决“是否真的送达用户”
- `completed + delivery_failed` 必须被视为一等问题

## 3.5 ForegroundBinding

表示当前前台对话默认接续哪个 `RootTask`。

```text
session_key
foreground_root_task_id
bound_at
source                   # explicit_switch | reply_to | followup_default | classifier_fallback
confidence
reason
cleared_at
```

关键规则：

- `reply_to` 优先级高于普通 follow-up
- `explicit_switch` 优先级最高
- `classifier_fallback` 只能作为最后兜底

---

## 4. OpenClaw 侧改造清单

## 4.1 OCL-CL-1 RootTask 建模与透传

目标：

- 让“用户主任务”成为执行链一级对象

改造项：

- 新建 `RootTask` store / table / in-memory index
- 在主消息入口增加 root task 判定器
- 在 dispatch metadata 中强制透传：
  - `root_task_id`
  - `origin_message_id`
  - `reply_to_message_id`

完成标准：

- 任何执行链都能追溯到唯一 `root_task_id`
- 短 follow-up 不再轻易创建新 root task

## 4.2 OCL-CL-2 WorkflowRun 建模

目标：

- 区分“主任务”与“执行尝试”

改造项：

- 建立 `WorkflowRun` 表 / 状态机
- 每次真正启动执行链时创建 `workflow_run_id`
- retry / patch / verification 必须创建新 run

完成标准：

- 能回答“这个任务做过几次执行尝试”
- 不再把 run 完成误当成 root 完成

## 4.3 OCL-CL-3 Receipt 协议升级

目标：

- 把现有 `PIPELINE_RECEIPT` 升级为结构化证据协议

改造项：

- 保持兼容文本协议
- 内部解析后补齐：
  - `receipt_id`
  - `idempotency_key`
  - `root_task_id`
  - `workflow_run_id`
  - `adoption_status=pending`
- 后续逐步推动 subagent 显式携带 `root_task_id` / `workflow_run_id`

完成标准：

- 没有结构化 receipt 的链路不能推进为强事实

## 4.4 OCL-CL-4 Receipt Adoption 引擎

目标：

- 建立唯一权威采纳点

校验项：

- `root_task_id` 是否存在
- `workflow_run_id` 是否属于该 root
- `agent / phase / action` 是否符合上下文
- `evidence` 是否可审计
- `idempotency_key` 是否重复
- 是否与已有 adopted receipt 冲突

输出：

- `adopted`
- `rejected`
- `superseded`

完成标准：

- adoption 成为任务事实推进的唯一入口
- `health-monitor` 不再需要猜哪个 receipt 才算有效

## 4.5 OCL-CL-5 Finalizer 最小闭环

目标：

- 建立正式主任务收口器

触发条件：

- workflow completed
- adopted blocked receipt
- user cancelled
- superseded by new root
- manual finalize

职责：

- 聚合 adopted receipts
- 选择可信最终证据
- 写 `FinalizationRecord.final_status`
- 生成 `user_visible_summary`
- 更新 `RootTask.status`

完成标准：

- `test completed != root completed` 成为系统内建规则

## 4.6 OCL-CL-6 Delivery 状态分离

目标：

- 区分“已收口”和“已送达”

改造项：

- finalizer 输出后进入 delivery stage
- 写入：
  - `delivery_status=finalized`
  - 成功后 `delivery_status=delivered`
  - 失败后 `delivery_status=delivery_failed`
- delivery retry 进入 OpenClaw 内部 self-check/recovery 范畴

完成标准：

- 已完成但未送达不会再被误判为“已闭环”

## 4.7 OCL-CL-7 Foreground / Background 绑定规则

目标：

- 防止短句 follow-up 冲散主任务

规则顺序：

1. `explicit_switch`
2. `reply_to`
3. `followup_default`
4. `classifier_fallback`

新 root 触发条件：

- 明确提出新目标
- 明确提出新范围 / 新对象 / 新约束
- 明确要求切换当前任务

完成标准：

- “继续 / 到哪了 / 还有吗”不会默认新建 root task

## 4.8 OCL-CL-8 Late Result 规则

目标：

- 防止迟到结果污染当前前台任务

规则：

- root 仍 active：允许 adoption
- root 已 superseded：标 `late_result=true`，只进 timeline
- root 已 finalized：默认不改主结论，只记录 late result

完成标准：

- 旧任务迟到结果不会抢占当前 foreground 的首条回复位

---

## 5. Health Monitor 侧改造清单

## 5.1 HM-CL-1 RootTask 镜像页

目标：

- 展示 OpenClaw 当前 root task 真相镜像，而不是本地猜测结果

展示字段：

- `root_task_id`
- `status`
- `foreground / background`
- `current_workflow_run_id`
- `latest adopted receipt`
- `finalization status`
- `delivery status`

## 5.2 HM-CL-2 Adoption / Finalizer / Delivery 时间线

目标：

- 操作员可以清楚知道问题卡在哪一层

必须区分：

- receipt arrived
- receipt adopted
- finalizer executed
- delivery delivered / delivery failed

## 5.3 HM-CL-3 外层告警

必须支持的监督告警：

- receipt 已到但长时间未 adoption
- workflow completed 但 finalizer 未触发
- finalizer 已执行但 `delivery_status=delivery_failed`
- foreground task 长时间无新 adopted evidence
- late result 到达但 root 已 superseded / finalized
- follow-up 绑定来源为 `classifier_fallback` 且低 confidence

## 5.4 HM-CL-4 审计与回放

目标：

- 支持回放“为什么这个任务被绑成这样、为什么没有闭环”

最小回放轴：

- user message
- foreground binding changes
- workflow runs
- receipts
- adoption decisions
- finalization record
- delivery outcome

## 5.5 HM-CL-5 验收面板

必须能直接判断：

- 当前 root 是否已终态
- 若终态，是否已送达
- 若未终态，卡在 receipt / adoption / finalizer / delivery 哪一层

---

## 6. 存储与接口改造

## 6.1 OpenClaw 存储新增

最低需要新增四类持久化对象：

- `root_tasks`
- `workflow_runs`
- `receipts`
- `finalization_records`

另加一类会话级索引：

- `foreground_bindings`

## 6.2 共享上下文 / 导出契约

OpenClaw 对 `health-monitor` 暴露的共享事实建议至少包含：

```json
{
  "session_key": "...",
  "foreground_root_task_id": "rt_xxx",
  "roots": [
    {
      "root_task_id": "rt_xxx",
      "status": "active",
      "current_workflow_run_id": "wr_xxx",
      "finalization_status": null,
      "delivery_status": null,
      "latest_adopted_receipt": {
        "receipt_id": "rc_xxx",
        "agent": "dev",
        "phase": "implementation",
        "action": "completed"
      }
    }
  ]
}
```

## 6.3 API / 事件面建议

建议至少补这些事件类型：

- `root_task_created`
- `foreground_binding_changed`
- `workflow_run_started`
- `receipt_arrived`
- `receipt_adopted`
- `receipt_rejected`
- `receipt_superseded`
- `finalizer_started`
- `finalizer_completed`
- `final_delivery_succeeded`
- `final_delivery_failed`
- `late_result_recorded`

---

## 7. 迁移策略

## 7.1 P0：先修“完成不蒸发”

OpenClaw：

- 建 `RootTask`
- 建 `WorkflowRun`
- 建 `Receipt pending -> adopted`
- 建最小 `finalizer`
- 建 `delivery_status`

Health Monitor：

- 镜像 root task
- 展示 adoption / finalizer / delivery 状态
- 告警“已完成未收口”与“已收口未送达”

## 7.2 P1：再修“短句不抢主任务”

OpenClaw：

- 建 `ForegroundBinding`
- 加 `reply_to_message_id`
- 建 follow-up 默认绑定规则
- 建 foreground / background 切换规则

Health Monitor：

- 展示当前 foreground / background root task
- 告警绑定来源不明或 classifier fallback 过多

## 7.3 P2：最后做智能增强

可选增强：

- classifier 优化
- pending_bind 机制
- reopen / merge / split
- 误绑定分析报告

要求：

- 不得破坏“主闭环真相在 OpenClaw”的原则

---

## 8. 验收用例

## 8.1 正常开发闭环

场景：

- 用户发起实现型任务
- main -> pm -> dev -> test
- test completed
- finalizer 执行
- delivery 成功

验收：

- `root_task_id` 稳定存在
- receipt 被 adoption
- finalizer 已执行
- `RootTask=completed`
- `delivery_status=delivered`

## 8.2 中途短句追问

场景：

- 用户发起主任务
- 中途发“继续 / 到哪了 / 还有吗”

验收：

- 不新建 root task
- follow-up 绑定到 foreground root
- 回复仅基于 adopted receipt 或明确事实

## 8.3 新需求打断旧任务

场景：

- 用户先做 A
- 再说“另外做 B”

验收：

- B 新建 root task
- foreground 切到 B
- A 进入 background 或 superseded
- A 的迟到结果不会抢占 B 的首条回复位

## 8.4 子链完成但 adoption 卡住

场景：

- dev/test receipt 已到
- adoption 未完成

验收：

- OpenClaw 不直接标 root completed
- Health Monitor 告警 adoption 卡住
- 问题可定位到 adoption / finalizer 层

## 8.5 已收口但 delivery 失败

场景：

- finalizer 已输出 `completed`
- 发送用户消息失败

验收：

- `final_status=completed`
- `delivery_status=delivery_failed`
- Health Monitor 告警“已收口未送达”
- OpenClaw 可进入 delivery retry

## 8.6 被阻塞

场景：

- adopted blocked receipt 到达

验收：

- root task 最终进入 `blocked`
- 用户可见摘要明确说明卡点、缺失项和下一步

---

## 9. 完成定义

这套改造完成后，必须同时满足：

- `root_task_id` 成为全链路一级对象
- `receipt adoption` 成为唯一权威写入点
- `finalizer` 成为主任务终态唯一入口
- `delivery_status` 与 `final_status` 被明确区分
- 短句默认绑定 foreground root，而不是轻易新建 root
- `health-monitor` 只监督，不再做主闭环判断

一句话完成定义：

- OpenClaw 负责主任务闭环真相；Health Monitor 负责闭环监督、审计与验收。
