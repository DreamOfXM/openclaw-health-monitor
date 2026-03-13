# OpenClaw Health Monitor Backlog

本文档是内部 backlog 视图。

它基于：

- [product-architecture.md](/Users/hangzhou/openclaw-health-monitor/docs/product-architecture.md)
- [product-architecture-review.md](/Users/hangzhou/openclaw-health-monitor/docs/product-architecture-review.md)
- [product-roadmap-execution.md](/Users/hangzhou/openclaw-health-monitor/docs/product-roadmap-execution.md)
- [internal-requirements.md](/Users/hangzhou/openclaw-health-monitor/docs/internal-requirements.md)
- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)
- [guardian-learning-migration-checklist.md](/Users/hangzhou/openclaw-health-monitor/docs/guardian-learning-migration-checklist.md)
- [openclaw-learning-implementation-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-implementation-design.md)
- [learning-execution-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-execution-work-packages.md)
- [openclaw-learning-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-artifact-schema.md)
- [openclaw-learning-cron-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-cron-runtime-spec.md)
- [health-monitor-learning-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-learning-supervision-spec.md)
- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)
- [openclaw-self-check-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-work-packages.md)
- [health-monitor-self-check-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-self-check-supervision-spec.md)
- [openclaw-self-check-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-artifact-schema.md)
- [openclaw-self-check-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-runtime-spec.md)
- [openclaw-main-closure-engineering-plan.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-main-closure-engineering-plan.md)
- [openclaw-main-closure-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-main-closure-work-packages.md)

目标是把路线图进一步拆成可排期、可指派、可验收的任务清单。

当前约束：

- `health-monitor` 是外层控制面，不是第二个 orchestrator
- OpenClaw 原生 session / queue / subagent state 是权威真相
- task registry、contracts、control actions 仅表达运维观察和治理建议

---

## Epic 0：运行基线

目标：

- 系统在任何时刻都明确只运行一个激活环境
- 所有启停、重启、切换行为一致
- 环境状态对操作员透明可信

### HM-001 单活环境入口收口

优先级：`P0`

问题：

- 仍存在 `primary` 与 `official` 双监听回归风险
- 说明仍有入口绕过统一环境调度

范围：

- 收口所有 gateway start/stop/restart/switch 入口
- 统一以 `ACTIVE_OPENCLAW_ENV` 为准

涉及文件：

- [desktop_runtime.sh](/Users/hangzhou/openclaw-health-monitor/desktop_runtime.sh)
- [manage_official_openclaw.sh](/Users/hangzhou/openclaw-health-monitor/manage_official_openclaw.sh)
- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 任意入口触发操作后，只存在一个 gateway listener
- `18789` 与 `19021` 不会同时处于有效运行态

### HM-002 单活环境回归测试补齐

优先级：`P0`

问题：

- 单活约束如果没有全链路测试，极易回归

范围：

- 补齐 Dashboard、Guardian、runtime controller 三类入口测试
- 把双监听视为 release blocker

涉及文件：

- [tests/test_dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/tests/test_dashboard_backend.py)
- [tests/test_guardian.py](/Users/hangzhou/openclaw-health-monitor/tests/test_guardian.py)

验收标准：

- 覆盖环境切换、面板重启、守护重启、gateway 启动
- 发生双监听时测试失败

### HM-003 环境卡片可信化

优先级：`P0`

问题：

- 操作员无法一眼确认当前运行的是哪套代码 / 状态 / token

范围：

- 环境卡片展示：
  - env id
  - code path
  - state path
  - git head
  - token 前缀
  - listener pid

涉及文件：

- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 不需要读日志即可区分 `primary` 和 `official`

### HM-004 环境不一致告警

优先级：`P0`

问题：

- 当前即使双开或状态漂移，面板未必立即显式告警

范围：

- 检测并告警：
  - `active=official` 但 `primary` 仍监听
  - `active=primary` 但 `official` 仍监听
  - `active env` 与 Dashboard 打开入口不一致

涉及文件：

- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)
- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)

验收标准：

- 不一致场景进入 recent events / diagnoses

---

## Epic 1：任务可信度

目标：

- 系统能判断任务是否真的开始、推进、完成
- 不再依赖模型自由文本当作任务真相

### HM-101 任务协议 formal 化

优先级：`P1`

问题：

- 现有 task contracts 已有雏形，但多 Agent 协作协议仍不够正式

范围：

- 定义 `request / confirmed / final / blocked`
- 引入 `ack_id`
- 明确 `final` 后静默规则

涉及文件：

- [task_contracts.py](/Users/hangzhou/openclaw-health-monitor/task_contracts.py)
- Agent workspace protocol files

验收标准：

- 同一任务不再重复确认
- `final` 后不会继续刷屏

### HM-102 任务证据链增强

优先级：`P1`

问题：

- 现在仍有大量 `received_only / missing_pipeline_receipt`

范围：

- 补强 progress / receipt / completion 解析
- 区分强证据与弱证据
- 为 task 输出 evidence summary

涉及文件：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)

验收标准：

- Dashboard 可解释任务为何卡在某状态

### HM-103 旧任务迟到结果治理

优先级：`P1`

问题：

- 旧任务迟到结果可能覆盖新问题

范围：

- 引入 background result 机制
- 当前活跃任务优先
- 旧任务只允许附加，不允许抢占当前回复位

涉及文件：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- 相关任务状态计算逻辑

验收标准：

- 新问题不会被旧任务尾包覆盖

### HM-104 主任务闭环主源切换

优先级：`P1`

问题：

- 当前系统还没有把主任务闭环真相完全放回 OpenClaw
- 如果继续让 `health-monitor` 补 adoption / completion / final 判断，会重新长成第二个 orchestrator

范围：

- 在 OpenClaw 内实现：
  - `RootTask`
  - `WorkflowRun`
  - `Receipt adoption`
  - `FinalizationRecord`
  - `ForegroundBinding`
- 明确 `final_status` 与 `delivery_status` 的分离
- 让短句 follow-up 默认绑定 foreground root task

涉及文档：

- [openclaw-main-closure-engineering-plan.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-main-closure-engineering-plan.md)
- [openclaw-main-closure-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-main-closure-work-packages.md)

验收标准：

- `WorkflowRun completed != RootTask completed` 成为系统内建规则
- 子链完成但 adoption 未发生时，主任务不会被误判 completed
- finalizer 成为 root task 终态唯一入口
- `health-monitor` 只镜像和告警，不独立写终态

### HM-105 控制动作队列解释力提升

优先级：`P1`

问题：

- 当前 control action 已有，但人不一定看得懂为什么 pending / blocked

范围：

- 增加 action reason、缺失 receipts、期望下一 actor 说明
- 提高 Dashboard 可解释性

涉及文件：

- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)
- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 操作员能看懂每个 action 为什么存在

### HM-105 流水线失联恢复

优先级：`P1`

问题：

- 已出现真实案例：主任务已经成功派发 `pm -> dev`，但没有任何结构化回执回到主链路
- 这类任务最后只会停在 `received_only / missing_pipeline_receipt / blocked_unverified`
- 守护系统目前已经能识别并告警，但还不能把这类“半启动半失联”的流水线自动收口

范围：

- 明确识别“已派发子任务但无 receipts 返回”的失联场景
- 为失联任务增加恢复策略：
  - session recovery
  - manual recovery hint
  - stale subagent detection
  - rebind active task
- 区分：
  - 根本未启动
  - 已启动但未回执
  - 已完成但未回传
- 将 `manual_or_session_recovery` 从状态名提升为明确的恢复流程

涉及文件：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)
- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 对“子任务已派发但 receipts 缺失”的任务，Dashboard 能明确显示为“流水线失联”
- 能输出具体恢复建议，而不是只显示 blocked
- 同类任务不再只能靠人工翻 session jsonl 才知道卡在哪
- 至少对 `pm -> dev -> test` 流水线提供可执行恢复动作或恢复指引

---

## Epic 2：模型失败边界

目标：

- 把“环境问题”“任务协议问题”“模型调用问题”分层显示
- 避免所有无回复都混成一个现象

### HM-201 模型失败分类

优先级：`P1`

问题：

- 当前无回复可能来自 auth failure、empty response、delivery failure、control failure

范围：

- 分类模型失败：
  - `auth_failure`
  - `empty_response`
  - `fallback_exhausted`
  - `delivery_failed`
  - `control_followup_failed`

涉及文件：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 面板能明确告诉操作员失败在哪一层

### HM-202 最新模型失败摘要卡片

优先级：`P1`

问题：

- 操作员无法快速知道当前主失败类型

范围：

- 在 Dashboard 增加最新失败摘要区域
- 展示最近失败 provider/model/status/message

涉及文件：

- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 用户问“为什么不回”时，不需要先翻日志

---

## Epic 3：上下文生命周期

目标：

- 把系统从“能跑”推进到“长期运行也不退化”

### HM-301 上下文治理配置基线

优先级：`P2`

问题：

- 当前 OpenClaw 配置没有完整 context lifecycle 策略

范围：

- 定义推荐基线：
  - memory flush
  - context pruning
  - daily / idle reset
  - session maintenance

涉及对象：

- OpenClaw config
- 相关内部设计文档

验收标准：

- 形成可复用配置模板

### HM-302 上下文治理达标检查

优先级：`P2`

问题：

- 即使有推荐配置，也需要面板识别是否达标

范围：

- 在 Dashboard 上加入 context lifecycle readiness 检查

涉及文件：

- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 面板能判断当前环境是否具备长期运行基线

---

## Epic 4：记忆与反思

目标：

- 把学习主责任迁回 OpenClaw，把 health-monitor 收口为监督与验收层

### HM-401 标准化 learning 目录协议

优先级：`P2`

问题：

- 当前 `.learnings / MEMORY / memory` 已有雏形，但 OpenClaw 内部写入协议还不统一

范围：

- 定义：
  - `.learnings/ERRORS.md`
  - `.learnings/LEARNINGS.md`
  - `.learnings/FEATURE_REQUESTS.md`
  - `MEMORY.md`
  - `memory/YYYY-MM-DD.md`

验收标准：

- learning 有统一写入路径，且主写入发生在 OpenClaw 内部
- promote 目标位置可显式标记为 `MEMORY.md / AGENTS.md / Skills / guardrail rules`

### HM-402 每日反思 promote 机制

优先级：`P2`

问题：

- 当前 reflection 仍混有外层 guardian 逻辑，主责任没有完全回到 OpenClaw

范围：

- 定义 OpenClaw 内部 cron：
  - `daily-reflection`
  - `memory-maintenance`
  - `team-rollup`
- 定义 repeated issue 晋升阈值
- 标准化进入 `MEMORY / AGENTS / Skills / watcher rules` 的路径

涉及文件：

- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)
- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)

验收标准：

- reflection 主执行发生在 OpenClaw cron
- promote 决策由 OpenClaw 产出
- repeated issue 能从 pending 学习项升级并落地到明确注入位置

### HM-402A 学习监督与验收面

优先级：`P2`

问题：

- 即使 OpenClaw 已开始学习，如果外层无法证明学习真的发生，产品仍然不可信

范围：

- 在 Dashboard / shared-state 中展示：
  - learning backlog
  - reflection history
  - promoted items
  - memory freshness
  - reuse evidence
  - repeat-error trend

涉及文件：

- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)
- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)
- [shared-state-model.md](/Users/hangzhou/openclaw-health-monitor/docs/shared-state-model.md)

验收标准：

- health-monitor 能判断今天 reflection cron 是否运行
- health-monitor 能判断 `MEMORY.md` 是否更新
- health-monitor 能展示同类问题后续是否下降

### HM-403 初始化基线与配置补齐

优先级：`P2`

问题：

- 当前缺少对 OpenClaw workspace 的统一初始化能力
- readiness 能检查基线，但缺少“合并补齐 + drift 报告”

范围：

- 初始化工作区基础文件与目录
- 对现有 `openclaw.json` 执行“合并补齐”
- 输出 bootstrap status 和 config drift

验收标准：

- 缺失项被补齐
- 高风险配置不被自动覆盖
- Dashboard/shared-state 可见 bootstrap status

### HM-404 OpenClaw 内部 Self-Check / Heartbeat

优先级：`P1`

问题：

- 外层 guardian 能发现 silent / no_reply，但这不等于 OpenClaw 自己知道自己卡住
- 当前仍缺少 OpenClaw 内部的最小 self-check / self-recovery 机制

范围：

- 在 OpenClaw 内部增加 `runtime-self-check`
- 检测：
  - `silent_stage`
  - `no_final_reply`
  - `completed != delivered`
  - `stale_subagent`
- 触发最小恢复动作：
  - session refresh
  - finalization retry
  - delivery retry
  - subagent reconciliation

涉及文件：

- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)

验收标准：

- OpenClaw 自己能发现卡住
- OpenClaw 自己能触发最小恢复动作
- health-monitor 只监督，不接管判断

---

## Epic 5：shared-state 产品化

目标：

- 把当前 scattered data/state 文件升级成清晰共享状态模型

### HM-501 shared-state 模型定义

优先级：`P2`

问题：

- 当前 `data/` 与 SQLite 已经在承载共享状态，但结构尚不统一

范围：

- 明确定义共享对象：
  - current task facts
  - task registry snapshot
  - control action queue
  - runtime health
  - learning backlog

验收标准：

- 形成统一文档和字段约定

### HM-502 shared-state 导出层

优先级：`P2`

问题：

- 当前状态虽然存在，但不够稳定可消费

范围：

- 建立稳定导出文件/API
- 让 Agent 和 UI 都读统一事实源

涉及文件：

- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)
- [dashboard_backend.py](/Users/hangzhou/openclaw-health-monitor/dashboard_backend.py)

验收标准：

- 聊天不再承担状态数据库角色

### HM-503 Task Watcher 最小状态面

优先级：`P2`

问题：

- 当前缺少对 `completed != delivered` 的最小结构化监督

范围：

- watcher task 持久化
- shared-context/monitor-tasks 同步
- DLQ 统计
- watcher summary 导出

验收标准：

- watcher 能区分 completed / delivered / dlq
- Dashboard/shared-state 可直接展示 watcher summary

---

## 建议排期

### Sprint A

- HM-001
- HM-002
- HM-003
- HM-004

### Sprint B

- HM-201
- HM-202
- HM-102

### Sprint C

- HM-101
- HM-103
- HM-104

### Sprint D

- HM-301
- HM-302
- HM-401
- HM-402
- HM-403
- HM-501
- HM-502
- HM-503

---

## 完成定义

### 近期 Done

- 环境单活可验证
- 面板状态可信
- 无回复可分层定位

### 中期 Done

- 任务协议清晰
- 控制动作可解释
- 旧任务不会污染新任务

### 长期 Done

- 长 session 可治理
- learning 可沉淀
- Health Monitor 真正成为外挂运行控制面
