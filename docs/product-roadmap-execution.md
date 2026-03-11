# OpenClaw Health Monitor 执行路线图

本文档用于把产品架构结论落成可执行路线图。

与 [product-architecture-review.md](/Users/hangzhou/openclaw-health-monitor/docs/product-architecture-review.md) 的区别是：

- 评审稿回答“我们是什么”
- 本文回答“下一步具体做什么”

---

## 1. 总体目标

未来一阶段的目标不是继续堆页面功能，而是补齐 3 个底座：

1. 单活环境与运行治理基线
2. 任务控制协议与证据链
3. 长时间运行的上下文 / 记忆演化能力

按优先级划分：

- `P0` 先保运行正确
- `P1` 再保任务可信
- `P2` 再保系统可进化

---

## 2. P0：运行基线

目标：

- 系统在任何时刻都清楚“哪套环境在跑”
- 所有启动、重启、切换行为都只能作用于当前激活环境
- 不再出现双环境同时监听但系统认知漂移的情况

### P0-1 单活环境硬约束

问题：

- 当前 `primary` / `official` 仍存在双监听回归风险
- 说明还有启动入口未完全收口

交付内容：

- 统一所有 gateway 启停入口
- 所有 start / stop / restart / switch 都必须走 `ACTIVE_OPENCLAW_ENV`
- 增加后台自检：发现 `18789` 与 `19021` 同时监听时，记录为 P0 异常

涉及模块：

- [desktop_runtime.sh](/Users/hangzhou/openclaw-health-monitor/desktop_runtime.sh)
- [manage_official_openclaw.sh](/Users/hangzhou/openclaw-health-monitor/manage_official_openclaw.sh)
- [dashboard.py](/Users/hangzhou/openclaw-health-monitor/dashboard.py)
- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)

验收标准：

- 任意入口触发重启后，只存在一套 gateway listener
- Dashboard 显示的 active env 与实际 listener 一致
- 自动化测试覆盖：
  - 切换环境
  - Dashboard 重启
  - Guardian 自动重启
  - runtime `start gateway`

### P0-2 环境状态可信化

问题：

- 现在环境卡片虽然有 code/path/head，但辨识度仍然不足
- 用户仍可能怀疑“官方验证版是不是其实还是主用版”

交付内容：

- Dashboard 环境卡片直接显示：
  - env id
  - code path
  - state path
  - git head
  - gateway token 前缀
  - listener pid
- 加入环境不一致告警：
  - active=official 但 primary 仍在监听
  - active=primary 但 official 仍在监听

涉及模块：

- [dashboard.py](/Users/hangzhou/openclaw-health-monitor/dashboard.py)

验收标准：

- 仅看卡片即可判断当前运行的是哪套代码和状态
- 发生双开时，UI 不再静默

### P0-3 模型失败边界显式化

问题：

- 当前用户容易把“无回复”误判成重启没生效或环境切错
- 实际上可能是 provider 401 / empty response / fallback 问题

交付内容：

- 在 Dashboard 中新增“最新模型调用失败摘要”
- 区分：
  - auth failure
  - empty response
  - no visible reply
  - control followup failed

涉及模块：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [dashboard.py](/Users/hangzhou/openclaw-health-monitor/dashboard.py)

验收标准：

- 用户问“为什么没回复”时，面板可直接给出失败层级

---

## 3. P1：任务可信度

目标：

- 让系统知道“任务是否真的开始、真的推进、真的完成”
- 把自由文本判断进一步替换为结构化证据

### P1-1 控制协议 formal 化

问题：

- 现在虽然已有 task contracts 和 control actions
- 但多 Agent 协作还没有统一协议外形

交付内容：

- 定义统一控制协议：
  - `request`
  - `confirmed`
  - `final`
  - `blocked`
- 每次链路都带 `ack_id`
- `final` 后禁止继续刷屏
- 对外只允许单条最终收敛消息

涉及模块：

- [task_contracts.py](/Users/hangzhou/openclaw-health-monitor/task_contracts.py)
- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- Agent workspace rules

验收标准：

- 同一任务不会因重复确认而多次播报
- `final` 后不会被旧结果覆盖
- 控制动作能关联到唯一 `ack_id`

### P1-2 任务证据链增强

问题：

- 当前很多任务落在 `received_only / missing_pipeline_receipt`
- 说明“任务开始”和“任务真正完成”之间证据不够

交付内容：

- 提升 receipts / progress 解析覆盖率
- 明确哪些事件是强证据，哪些只是弱信号
- 为每个 task 增加 evidence summary

涉及模块：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)

验收标准：

- Dashboard 能解释任务为何是 `received_only`
- 任务为何 blocked、缺哪类 receipt 可直接看到

### P1-3 旧任务迟到结果治理

问题：

- 新问题进入后，旧任务迟到结果仍可能干扰当前会话

交付内容：

- 引入“当前活跃任务优先”规则
- 旧任务结果只能作为 background result 附着
- 不允许抢占当前对外首条回复位

涉及模块：

- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- Agent workspace protocol

验收标准：

- 用户新问题不会被旧任务尾包覆盖
- 旧任务仍可补交付，但不会扰乱当前任务

---

## 4. P2：长期运行能力

目标：

- 不只是“看住 OpenClaw”
- 而是把整个系统推向可持续运行的 Agent OS

### P2-1 上下文生命周期治理

问题：

- 当前 OpenClaw 配置只有轻量 compaction
- 还没有 memory flush / pruning / reset / maintenance 体系

交付内容：

- 制定推荐配置模板：
  - `memoryFlush`
  - `contextPruning`
  - `daily/idle reset`
  - `session maintenance`
- 让 Health Monitor 对这套配置做显式检查

涉及模块：

- OpenClaw config
- [dashboard.py](/Users/hangzhou/openclaw-health-monitor/dashboard.py)

验收标准：

- 面板能识别“context lifecycle 配置是否达标”
- 长 session 不再无限膨胀

### P2-2 记忆闭环统一化

问题：

- 我们已经有 learning / reflection
- 但没有统一成标准 Agent 记忆结构

交付内容：

- 形成统一目录约定：
  - `MEMORY.md`
  - `memory/YYYY-MM-DD.md`
  - `.learnings/ERRORS.md`
  - `.learnings/LEARNINGS.md`
  - `.learnings/FEATURE_REQUESTS.md`
- 形成 daily promotion 规则

涉及模块：

- Agent workspace
- [guardian.py](/Users/hangzhou/openclaw-health-monitor/guardian.py)
- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)

验收标准：

- repeated issue 能自动从 pending 升级到长期记忆
- `MEMORY.md` 不失控膨胀

### P2-3 shared-state 模型产品化

问题：

- 现在已有 `data/` 和 SQLite state，但 shared-context 还不够清晰

交付内容：

- 把共享状态归一化成清晰产品模型：
  - current task facts
  - control action queue
  - job status
  - runtime health
  - learning backlog

涉及模块：

- [state_store.py](/Users/hangzhou/openclaw-health-monitor/state_store.py)
- [dashboard.py](/Users/hangzhou/openclaw-health-monitor/dashboard.py)

验收标准：

- 共享状态可追踪、可审计、可导出
- 聊天不再承担状态数据库角色

---

## 5. 推荐迭代顺序

建议按下面顺序推进：

### Iteration 1

- P0-1 单活环境硬约束
- P0-2 环境状态可信化

### Iteration 2

- P0-3 模型失败边界显式化
- P1-2 任务证据链增强

### Iteration 3

- P1-1 控制协议 formal 化
- P1-3 旧任务迟到结果治理

### Iteration 4

- P2-1 上下文生命周期治理
- P2-2 记忆闭环统一化
- P2-3 shared-state 模型产品化

---

## 6. 每阶段完成定义

### P0 完成定义

- 任意时刻只允许一套 gateway 有效运行
- 环境切换与重启行为完全可预测
- 用户能直接区分“环境问题”和“模型问题”

### P1 完成定义

- 系统能解释任务当前状态和阻塞原因
- 多 Agent 协作不再靠隐式约定
- 新消息与旧结果不会互相污染

### P2 完成定义

- 长时间运行不会因为上下文膨胀而退化
- 记忆与学习能稳定沉淀
- Health Monitor 成为真正的外挂 Agent OS 控制层

---

## 7. 当前建议结论

当前最不该做的，是继续堆新的表层功能。

当前最该做的，是按这个顺序收口：

1. 把运行基线打牢
2. 把任务协议做实
3. 再把长期运行能力补齐

也就是说，下一阶段的判断标准不是“页面看起来更丰富”，而是：

- 是否更稳定
- 是否更可信
- 是否更可持续运行
