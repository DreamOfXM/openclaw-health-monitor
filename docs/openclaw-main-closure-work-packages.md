# OpenClaw 主闭环实施工作包

## 1. 目的

把 [openclaw-main-closure-engineering-plan.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-main-closure-engineering-plan.md) 进一步拆成可指派、可并行、可验收的实施工作包。

本清单默认：

- `OpenClaw` 负责主闭环真相
- `Health Monitor` 负责监督、镜像、审计、验收

---

## 2. 工作包总览

建议按 8 个工作包推进：

1. `CL-1` RootTask / ForegroundBinding 基础建模
2. `CL-2` WorkflowRun 与 dispatch metadata 透传
3. `CL-3` Receipt 协议升级与幂等保护
4. `CL-4` Receipt Adoption 引擎
5. `CL-5` Finalizer 与 delivery 分离
6. `CL-6` late result / superseded / background 规则
7. `CL-7` Health Monitor 镜像与告警接入
8. `CL-8` 回归场景与验收套件

---

## 3. 工作包定义

### CL-1 RootTask / ForegroundBinding 基础建模

目标：

- 把“用户主任务”和“当前前台焦点”变成 OpenClaw 一等对象

改造点：

- 新增 `root_tasks` 存储
- 新增 `foreground_bindings` 存储
- 主消息入口增加 root-task 创建/绑定逻辑
- 支持：
  - `origin_message_id`
  - `reply_to_message_id`
  - `source`
  - `confidence`

完成标准：

- 同一主任务不会因短 follow-up 被轻易重建
- 能审计“为什么当前 foreground 绑定到这个 root task”

### CL-2 WorkflowRun 与 dispatch metadata 透传

目标：

- 把执行尝试和用户主任务区分开

改造点：

- 新增 `workflow_runs` 存储
- 每次真正启动执行链时创建 `workflow_run_id`
- dispatch metadata 全链路透传：
  - `root_task_id`
  - `workflow_run_id`
  - `origin_message_id`
  - `reply_to_message_id`

完成标准：

- 任一 subagent receipt 都能回溯到 root task 和 workflow run

### CL-3 Receipt 协议升级与幂等保护

目标：

- 让 receipt 先成为结构化对象，再进入 adoption

改造点：

- 兼容现有 `PIPELINE_RECEIPT`
- 内部补齐：
  - `receipt_id`
  - `idempotency_key`
  - `root_task_id`
  - `workflow_run_id`
  - `adoption_status=pending`
- 对重复 receipt 做幂等保护

完成标准：

- receipt 重放、重复上报、迟到重发不会污染事实链

### CL-4 Receipt Adoption 引擎

目标：

- 建立主闭环唯一采纳入口

校验规则：

- root / workflow 归属合法
- agent / phase / action 合法
- evidence 可审计
- 幂等键不冲突
- 不覆盖已 adopted 更强证据

输出状态：

- `adopted`
- `rejected`
- `superseded`

完成标准：

- adoption 成为所有任务真相推进的唯一权威写入点

### CL-5 Finalizer 与 delivery 分离

目标：

- 正式区分“闭环结论已形成”和“结论已送达用户”

改造点：

- 新增 `finalization_records`
- finalizer 仅决定：
  - `final_status`
  - `selected_receipts`
  - `user_visible_summary`
- delivery 单独决定：
  - `delivered`
  - `delivery_failed`

完成标准：

- `completed + delivery_failed` 被系统显式识别

### CL-6 late result / superseded / background 规则

目标：

- 防止旧任务结果污染当前前台任务

改造点：

- 引入 `late_result` 标记
- 引入 `superseded_by_root_task_id`
- foreground / background 切换后，旧结果只进 timeline，不抢占主回复位

完成标准：

- 新任务不会被旧任务尾包覆盖

### CL-7 Health Monitor 镜像与告警接入

目标：

- 让外层监督直接消费 OpenClaw 闭环事实，而不是二次猜测

改造点：

- RootTask 镜像视图
- adoption / finalizer / delivery 时间线
- 告警：
  - receipt 未 adoption
  - completed 未 finalizer
  - finalized 未 delivered
  - late result after finalized

完成标准：

- `health-monitor` 只报告风险与事实，不再自己写终态

### CL-8 回归场景与验收套件

目标：

- 固化关键闭环场景，避免回归

必须覆盖：

1. 正常开发闭环
2. 短句 follow-up 绑定
3. 新需求切换 foreground
4. receipt arrived 但 adoption 卡住
5. finalized 但 delivery failed
6. superseded 后的 late result
7. blocked receipt 正式收口

完成标准：

- 每个场景都能看到 create -> run -> receipt -> adoption -> finalizer -> delivery 的完整链路

---

## 4. 推荐顺序

推荐实施顺序：

1. `CL-1`
2. `CL-2`
3. `CL-3`
4. `CL-4`
5. `CL-5`
6. `CL-7`
7. `CL-6`
8. `CL-8`

理由：

- 没有 root / workflow / receipt 身份，就无法做 adoption
- 没有 adoption，就没有 finalizer 的可靠输入
- 没有 finalizer / delivery 分离，就无法真正解决“已做完但没回给用户”

---

## 5. 完成定义

全部完成后，必须满足：

- 主任务不会被短 follow-up 冲散
- receipt 到达后必须经过 adoption 才能推进真相
- workflow 完成后必须经过 finalizer 才能进入 root 终态
- finalizer 与 delivery 被显式分离
- Health Monitor 只能监督，不再承担第二套主闭环逻辑
