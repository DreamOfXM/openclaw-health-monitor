# 字段归属矩阵 + 控制面边界表

> 本文档定义 OpenClaw（执行面）与 Health Monitor / helper（控制面）之间的职责边界。
> 
> 这不是偏好问题，是架构纪律。

---

## 一、核心原则

```
OpenClaw = 执行面（可以表达）
helper   = 控制面（负责怀疑）

OpenClaw 发 claim
helper 决定 claim 能不能升级成 fact
```

**本质问题**：谁有资格定义真相？

---

## 二、字段归属矩阵

### 2.1 执行面字段（OpenClaw 拥有）

| 字段 | 归属 | 说明 |
|------|------|------|
| `PIPELINE_RECEIPT` | OpenClaw | 执行层发出的结构化回执 |
| `PIPELINE_PROGRESS` | OpenClaw | 执行层发出的阶段进度声明 |
| `PIPELINE_PLAN` | OpenClaw | 产品方案内容 |
| `dispatching to agent` | OpenClaw | 调度行为日志 |
| `dispatch complete` | OpenClaw | 调度完成信号 |
| visible completion | OpenClaw | 用户可见的完成消息 |
| 业务方案内容 | OpenClaw | 需求分析、技术设计、实现细节 |
| 自然语言进度播报 | OpenClaw | 对用户的友好表达 |

**执行面可以**：
- 做事
- 发 receipt
- 发 progress
- 发 final
- 产出业务结果

### 2.2 控制面字段（helper / Health Monitor 拥有）

| 字段 | 归属 | 说明 |
|------|------|------|
| `task_id` | helper | 任务唯一标识（外部注册） |
| `facts source` | helper | 事实来源（必须是结构化证据） |
| `claim_level` | helper | 声明级别（received_only / planning_only / dev_running / verified） |
| `control_state` | helper | 控制状态（received_only / blocked / verified） |
| `evidence_level` | helper | 证据强度（weak / medium / strong） |
| `missing_receipts` | helper | 缺失的回执清单 |
| `next_action` | helper | 下一步控制动作 |
| `approved_summary` | helper | 批准的操作员可见摘要 |
| `contract_id` | helper | 任务契约类型 |
| `task registry` | helper | 任务注册表 |
| `current-task-facts` | helper | 当前任务事实快照 |

**控制面负责**：
- 注册任务
- 固化事实
- 核验 receipt
- 判断阶段是否真的成立
- 输出 received_only / blocked / verified

---

## 三、控制面边界表

### 3.1 helper 应该做的

| 职责 | 说明 |
|------|------|
| **验** | 核验 receipt 是否真实存在、格式是否正确、链路是否完整 |
| **记** | 记录 task_id、control_state、evidence_level 等控制面字段 |
| **判** | 判断 claim 能否升级为 fact，判断阶段是否成立 |
| **导出事实** | 输出 `current-task-facts.json` 供操作员和下游系统使用 |
| **怀疑** | 对执行层的声明保持怀疑，直到有结构化证据支持 |
| **阻塞标记** | 当证据不足时，标记 `blocked_unverified` |
| **催办** | 当 missing_receipts 存在时，发出 follow-up |

### 3.2 helper 不应该做的

| 禁止行为 | 原因 |
|----------|------|
| 改写业务目标 | 业务目标由用户和 OpenClaw 定义 |
| 帮 OpenClaw 决定怎么做业务方案 | 方案是执行层的职责 |
| 替执行链路补剧情 | 不能凭空生成 receipt 或 progress |
| 成为第二个 orchestrator | helper 是控制面，不是编排层 |
| 信任自由文本作为任务真相 | 必须依赖结构化证据 |
| 把"已派发"说成"已启动" | 这是模型乐观幻觉的典型表现 |

---

## 四、证据层级

### 4.1 强证据（控制面信任）

| 证据类型 | 来源 | 说明 |
|----------|------|------|
| `dispatching to agent` | OpenClaw Gateway | 真实调度行为 |
| `PIPELINE_RECEIPT` | OpenClaw Agent | 结构化回执，包含 agent/phase/action/evidence |
| `PIPELINE_PROGRESS` | OpenClaw Agent | 阶段转换信号 |
| visible completion | OpenClaw Agent | 用户可见的完成消息 |
| `dispatch complete` | OpenClaw Gateway | 调度完成信号 |

### 4.2 弱证据（控制面不信任）

| 证据类型 | 问题 |
|----------|------|
| 自由文本进度描述 | 可能是模型乐观幻觉 |
| "我已经安排了" | 语义连贯 ≠ 现实成立 |
| "方案已经有了" | 有方案 ≠ 研发已启动 |
| "测试快好了" | 快好了 ≠ 测试在跑 |

---

## 五、控制状态定义

| 状态 | 含义 | 证据要求 |
|------|------|----------|
| `received_only` | 任务已接收，但无结构化证据 | 无 required_receipts |
| `planning_only` | 方案存在，但开发未启动 | 有 pm receipt，无 dev receipt |
| `dev_running` | 开发已启动 | 有 dev:started receipt |
| `awaiting_test` | 开发完成，测试未启动 | 有 dev:completed，无 test:started |
| `test_running` | 测试已启动 | 有 test:started receipt |
| `calculator_running` | 计算已启动 | 有 calculator:started receipt |
| `awaiting_verifier` | 计算完成，复核未完成 | 有 calculator:completed，无 verifier:completed |
| `blocked_unverified` | 证据不足，已阻塞 | 重试后仍无 required_receipts |
| `completed_verified` | 完成且验证通过 | 有 terminal_receipts |

---

## 六、模型乐观幻觉防护

### 6.1 问题本质

LLM 天然倾向：**把语义上连贯，当成现实上成立**

典型表现：
- "我已经安排了" → 好像等于"事情已经往下走了"
- "方案已经有了" → 好像等于"研发已经开始了"
- "测试快好了" → 好像等于"测试 actually 在跑"

### 6.2 防护机制

| 机制 | 实现位置 |
|------|----------|
| 结构化回执协议 | OpenClaw Agent 发出 `PIPELINE_RECEIPT` |
| 证据层级判断 | helper 根据 evidence_level 决定是否信任 |
| 控制状态机 | helper 根据 receipt 链推导 control_state |
| 阻塞标记 | helper 在证据不足时标记 blocked |
| 操作员可见摘要 | helper 输出 approved_summary，不依赖自由文本 |

---

## 七、历史混乱的根源

过去为什么会乱？因为很多概念糊在一起：

| 混淆 | 正确分离 |
|------|----------|
| receipt 和 narration | receipt 是结构化证据，narration 是自然语言表达 |
| completed 和 delivered | completed 是执行层声明，delivered 是控制层验证 |
| accepted 和 verified | accepted 是接收信号，verified 是证据链完整 |
| final summary 和 final fact | summary 是表达，fact 是验证后的事实 |
| 执行进展和用户可见播报 | 执行进展依赖 receipt，播报可以是自然语言 |

---

## 八、架构纪律（硬约束）

```
1. OpenClaw 不负责自证推进
2. helper 负责控制面验真
3. OpenClaw 发 claim
4. helper 决定 claim 能不能升级成 fact
5. helper 要强，但强在"冷"，不是强在"会编排"
```

**如果不按这套切，系统会继续出现**：
- 嘴上很完整
- 过程很热闹
- 日志很多
- 但一问"到底有没有真的推进"，没人能给出冷的事实答案

---

## 九、相关文档

- `docs/architecture.md` - 控制面架构
- `docs/product-architecture.md` - 产品架构
- `task_contracts.json` - 任务契约定义
- `data/current-task-facts.json` - 当前任务事实快照

---

## 十、版本历史

| 日期 | 变更 |
|------|------|
| 2026-03-14 | 初始版本，基于主人5个结论整理 |