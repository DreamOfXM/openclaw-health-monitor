# OpenClaw Health Monitor 当前缺口与完整需求

本文档用于回答两个问题：

1. 现在核心能力里，哪些还没做完，哪些只是部分完成。
2. 下一阶段的完整需求应该怎么定义，避免继续零散补丁式开发。

---

## 1. 当前结论

当前系统已经具备以下主体能力：

- 环境治理
- 单活切换与重启链
- 任务注册表
- 任务合同与控制动作
- 学习 / 反思监督基础能力
- 官方验证版晋升控制器
- Dashboard 控制面基础视图

但它还没有达到“长期稳定、可解释、可发布”的完成状态。

准确判断是：

- 主体功能已形成
- 高优先级收口仍未完成
- 仍缺少一份以运行正确性和长期稳定性为核心的统一需求

---

## 2. 已完成到什么程度

### 2.1 已基本完成

- 双环境基础模型已经成立：`primary / official`
- Dashboard 能展示环境、任务、控制面、学习中心等核心区域
- Guardian 能做异常检测、控制跟进以及学习是否发生的外层观测
- State Store 已形成任务、合同、控制动作、learning、reflection 的统一存储
- Promotion Controller 已能支持 `official -> primary` 的晋升链路
- 全量测试目前通过，说明系统已经不是概念验证

### 2.2 已做但未完全收口

- 单活环境约束
- 环境可信展示
- 模型失败分类
- 任务证据链
- 学习闭环的权责收口与标准化输出

这些能力已经进入代码，但还没有达到“长期运行不回归、操作员不怀疑”的状态。

---

## 3. 还没做完或待完成的部分

以下是当前必须明确标记为“未完成”或“待完成”的部分。

### 3.1 P0：运行基线仍未彻底闭环

#### 3.1.1 单活环境约束未完全封死

现状：

- 已经修了一批入口
- 但运行期仍出现过 `18789` 与 `19021` 同时监听
- 说明仍存在绕过统一调度的启动/重启入口

未完成点：

- 所有 start / stop / restart / switch 入口还没有被证明完全统一
- 缺少“真实运行状态下的单活守卫”
- 缺少把双监听直接视为 release blocker 的机制

要求：

- 任意入口都必须只作用于 `ACTIVE_OPENCLAW_ENV`
- 任意时刻最多只允许一个 gateway listener 存活
- 一旦检测到双监听，Dashboard 和 Guardian 都必须立即告警

#### 3.1.2 环境状态虽然更清晰，但还不够“绝对可信”

现状：

- 环境卡片已经开始显示 code path、state path、git head、listener pid、token 前缀

未完成点：

- 这些信息还没有在所有关键视图中统一透出
- 用户仍需要交叉核对日志，不能完全只靠 UI 判断
- “当前看到的到底是哪套环境”还没有被做成第一层事实

要求：

- 总览页和环境页都要明确显示：
  - env id
  - code path
  - state path
  - git head
  - listener pid
  - gateway token 前缀
- active env 与实际 listener 不一致时必须进入 recent events / diagnoses

#### 3.1.3 重启链虽然修过，但仍需要统一验证

现状：

- Dashboard 重启、Guardian 自动重启、runtime start gateway 已做统一收口

未完成点：

- 缺少一套覆盖真实入口的回归验证矩阵
- 缺少“切环境 -> 重启 -> 打开 Dashboard -> 再次切换”的完整链路验证

要求：

- 对以下入口做统一验证：
  - Dashboard 重启
  - Guardian 自动重启
  - runtime start gateway
  - 环境切换
- 任何入口都不得把非激活环境重新拉起

### 3.2 P1：任务可信度还不够

#### 3.2.1 控制协议还没有 formal 化

现状：

- 任务合同已经有了
- control action 也有了
- receipts 也有了解析

未完成点：

- 多 Agent 协作没有统一协议外形
- 没有正式的 `request / confirmed / final / blocked`
- `ack_id` 还没有被产品化成统一约束

要求：

- 统一控制协议
- 每个任务链路必须可关联唯一 `ack_id`
- `final` 后禁止继续刷屏
- 同一任务不允许重复确认、重复终态

#### 3.2.2 任务证据链仍有明显空洞

现状：

- 系统已经能识别 `received_only / missing_pipeline_receipt`

未完成点：

- 还无法稳定解释“为什么它只是 received_only”
- progress / receipt / completion 的强弱证据层级还不够明确
- evidence summary 还没有做到真正面向操作员

要求：

- 每个任务都要能输出 evidence summary
- 明确区分：
  - 强证据
  - 弱信号
  - 缺失证据
- Dashboard 可直接解释任务为何 blocked / stuck / no visible reply

#### 3.2.3 旧任务迟到结果治理未完成

现状：

- 已识别这是一个问题

未完成点：

- 旧任务迟到结果仍可能干扰当前任务判断
- 缺少 background result 机制

要求：

- 当前活跃任务优先
- 旧任务结果只能附着为 background result
- 不允许旧任务尾包抢占当前首条对外回复位

#### 3.2.4 控制动作解释力不足

现状：

- control action 已经存在

未完成点：

- 操作员还不一定看得懂它为什么 pending / sent / blocked
- UI 还没有充分解释 action reason / next actor / missing receipts

要求：

- 每个 action 至少明确：
  - why this action exists
  - missing receipts
  - next actor
  - attempts
  - last error

#### 3.2.5 流水线失联恢复未完成

现状：

- 已出现真实案例：主任务已成功派发到 `pm`，并进一步派发到 `dev`
- 但主链路没有收到任何结构化 `PIPELINE_RECEIPT`
- 最终任务只能停在：
  - `received_only`
  - `missing_pipeline_receipt`
  - `blocked_unverified`
  - `manual_or_session_recovery`

未完成点：

- 守护系统现在只能识别并阻塞这类任务
- 还不能自动判断“是 pm 未回执、dev 已失联，还是 test 未接入”
- 还没有把“流水线失联恢复”做成明确产品能力
- 操作员仍需要手工翻 session jsonl 才能知道任务到底派发到了哪一步

要求：

- 增加“流水线失联”这一类显式问题类型
- 明确区分：
  - 根本未启动
  - 已启动但未回执
  - 已完成但未回传
- 对失联任务输出恢复方案：
  - session recovery
  - stale subagent detection
  - manual recovery hint
  - active task rebind
- Dashboard 不只显示 blocked，还要说明：
  - 最后成功派发到哪个 agent
  - 缺的是哪类 receipt
  - 下一步应该恢复哪一段流水线

### 3.3 P1：模型失败边界还需要彻底做实

#### 3.3.1 “为什么没回复”还不能总是一眼看懂

现状：

- 已经有模型失败分类和摘要能力雏形

未完成点：

- 仍需翻日志确认很多失败
- 失败分层还没完全变成 UI 第一事实

要求：

- 无回复必须至少分成：
  - `auth_failure`
  - `empty_response`
  - `fallback_exhausted`
  - `delivery_failed`
  - `control_followup_failed`
  - `no_visible_reply`
- 最近失败的 provider / model / status / message 必须直出

### 3.4 P2：上下文生命周期还没有真正落地

#### 3.4.1 目前更多是“检查”，不是“治理”

现状：

- Dashboard 已经能检查 context lifecycle readiness

未完成点：

- OpenClaw 本体配置还没有完整的：
  - memory flush
  - context pruning
  - daily / idle reset
  - session maintenance

要求：

- 给出推荐基线模板
- 面板检查是否达标
- 长 session 不再无限膨胀

### 3.5 P2：记忆闭环还未标准化

#### 3.5.1 learning / reflection 已有，但目录协议不统一

现状：

- 已有 learnings、reflection_runs、memory/.learnings 导出

未完成点：

- 还没统一成标准 Agent 记忆结构
- daily promote 规则还没被完整定义为产品能力

要求：

- 统一目录与职责：
  - `MEMORY.md`
  - `memory/YYYY-MM-DD.md`
  - `.learnings/ERRORS.md`
  - `.learnings/LEARNINGS.md`
  - `.learnings/FEATURE_REQUESTS.md`
- 定义 daily promotion 规则与阈值

### 3.6 P2：shared-state 还未产品化

现状：

- 已有 SQLite state 和 `data/` 导出

未完成点：

- shared-state 结构还不够显式
- 对外没有清晰 shared-state 模型

要求：

- 定义 shared-state 目录模型
- 让关键状态可被别的 Agent 或外部系统消费
- 让 shared-state 不再只是内部实现细节

---

## 4. 下一阶段完整需求

下面是建议采纳的完整需求定义。

## 4.1 产品目标

OpenClaw Health Monitor 的目标不再是“展示状态”，而是成为 OpenClaw 的外挂运行控制面，负责：

- 环境治理
- 任务治理
- 异常分层
- 恢复与晋升
- 学习与反思
- 长期运行基线检查

它的核心不是更复杂的页面，而是让系统具备：

- 可解释运行
- 可恢复运行
- 可持续运行

## 4.2 一级需求

### R1. 单活环境运行基线

系统必须保证：

- 同一时间只能有一个激活环境对外工作
- 所有 start / stop / restart / switch 入口统一以 `ACTIVE_OPENCLAW_ENV` 为准
- 一旦出现双监听或状态漂移，必须立即显式告警

验收标准：

- `18789` 与 `19021` 不得同时处于有效运行态
- active env、listener、Dashboard 打开入口三者必须一致

### R2. 环境状态可信化

系统必须让操作员仅靠 UI 就能判断：

- 当前激活的是哪套环境
- 跑的是哪套代码
- 用的是哪套状态目录
- 当前 listener pid 是什么

验收标准：

- 总览页和环境页直接展示：
  - env id
  - code path
  - state path
  - git head
  - token 前缀
  - listener pid

### R3. 任务可信度

系统必须能够判断：

- 任务是否真的开始
- 是否真的推进
- 是否真的完成
- 卡在哪一层

验收标准：

- 每个任务都有 contract、evidence summary、control state、next action
- `received_only / missing_pipeline_receipt / blocked` 均可解释

### R4. 控制协议 formal 化

系统必须引入统一控制协议：

- `request`
- `confirmed`
- `final`
- `blocked`
- `ack_id`

验收标准：

- 同一任务不重复确认
- `final` 后不再刷屏
- control action 与唯一 `ack_id` 关联

### R4.1 流水线失联恢复

系统必须能够处理“主任务已经派发子任务，但结构化回执链断掉”的情况。

验收标准：

- 系统能识别：
  - 子任务未启动
  - 子任务已启动但未回执
  - 子任务已完成但主链未收到结果
- Dashboard 能直接显示：
  - last dispatched agent
  - missing receipts
  - recommended recovery action
- Guardian 能为这类任务给出明确恢复建议，必要时进入 session recovery
- 操作员不再需要手工翻 session jsonl 才能定位卡点

### R5. 模型失败边界显式化

系统必须把“没回复”明确拆层，而不是只呈现一个现象。

验收标准：

- 面板可区分：
  - auth failure
  - empty response
  - fallback exhausted
  - delivery failed
  - control followup failed
  - no visible reply

### R6. 恢复与晋升治理

系统必须支持：

- 配置快照
- 环境恢复
- 官方验证版更新
- 官方验证版晋升为主用版
- 切换 / 回滚留痕

验收标准：

- 每次晋升前都有 preflight
- 每次切换和回滚均有历史记录

### R7. 学习与反思闭环

系统必须把 learn / reflect / promote / reuse 的主责任放回 OpenClaw，
同时让 health-monitor 能监督这些动作是否真的发生。

验收标准：

- learning 主写入由 OpenClaw 产生
- reflection runs 可追踪且来源于 OpenClaw cron
- promoted items 可展示并关联证据与注入位置
- health-monitor 能判断 `MEMORY.md` 是否更新
- health-monitor 能判断同类问题后续是否下降

### R8. 上下文生命周期治理

系统必须检查并推动 OpenClaw 达到长期运行基线。

验收标准：

- 面板能检查：
  - memory flush
  - context pruning
  - reset
  - maintenance
- 达标与否一眼可见

### R9. shared-state 产品化

系统必须把核心共享状态从“内部实现”提升为“外部可消费模型”。

验收标准：

- 有明确 shared-state 结构
- 关键状态支持外部 Agent / 自动化读取

---

## 5. 当前建议优先级

### 第一阶段：必须先做完

- 单活环境入口彻底收口
- 单活环境全链路测试补齐
- 环境卡片可信化
- 环境不一致显式告警
- 模型失败分层摘要

### 第二阶段：必须补强

- 任务证据链增强
- 控制协议 formal 化
- 流水线失联恢复
- 控制动作解释力提升
- 旧任务迟到结果治理

### 第三阶段：长期能力建设

- 上下文生命周期基线
- learning 目录协议标准化
- reflection / promotion 规则产品化
- shared-state 产品化

---

## 6. 最终判断

当前系统不是“没做完”，而是“主体已完成，但还没有达到可收口交付状态”。

最准确的说法是：

- 运行控制面主体已经成型
- 高优先级缺口主要集中在“单活硬约束、任务可信度、失败分层、长期运行底座”
- 下一阶段不能再零散加功能，而应该按统一需求继续收口

这份文档就是后续收口的统一基线。
