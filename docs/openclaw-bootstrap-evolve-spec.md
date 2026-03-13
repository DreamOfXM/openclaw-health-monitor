# OpenClaw 初始化与自进化实施方案

## 1. Purpose

本文档定义了一套严格的实施与验收方案，用来建设“可学习、可进化”的
OpenClaw 系统，其中 `openclaw-health-monitor` 负责初始化与治理，
OpenClaw 自身负责后续运行中的学习与成长。

目标不是“Prompt 更强”，而是让 Agent 系统可以长期稳定运行，并具备：

1. context lifecycle control
2. memory evolution
3. protocolized collaboration
4. asynchronous task supervision

本文档用于：

- implementation alignment
- code review calibration
- regression/backtest verification
- future capability audit

## 2. 核心原则

整体设计是：

- `health-monitor` 负责初始化
- `OpenClaw` 负责自进化

意思是：

- `health-monitor` 先把最小可用的目录结构、规则和治理基线搭好
- `OpenClaw` 在这套基础上运行，并通过记录学习项、定时反思、升级记忆、
  沉淀技能的方式逐步成长

## 3. 明确边界

### 3.1 OpenClaw 负责什么

OpenClaw 是执行内核，负责：

- session lifecycle
- queue/session serialization
- subagent orchestration
- final reply lifecycle
- agent workspace behavior
- native context lifecycle primitives
- native collaboration protocol enforcement if later added to core

### 3.2 health-monitor 负责什么

health-monitor 是外层控制面，负责：

- bootstrap scaffolding
- environment governance
- runtime health and anomaly detection
- shared-state export
- learning/reflection/promotion control
- Task Watcher
- ops dashboard and audit views

### 3.3 明确不做什么

health-monitor 不应该做：

- intercept user messages
- replace OpenClaw session/queue routing
- become a second orchestrator
- redefine OpenClaw internal execution truth

## 4. 能力模型

### 4.1 上下文生命周期

目标：

- prevent long-running sessions from bloating
- avoid stale context contaminating new work
- keep memory compact and recoverable

要求的基线能力：

- compaction with memory flush
- context pruning
- daily reset
- idle reset
- session maintenance

### 4.2 记忆进化

目标：

- today's error becomes tomorrow's rule
- repeated findings become stable memory
- stable workflows become reusable skills

要求的分层：

1. `SOUL.md`
2. `MEMORY.md`
3. `memory/YYYY-MM-DD.md`
4. `.learnings/`
5. `Skills/`

### 4.3 协作协议

目标：

- no reply storms
- deterministic convergence
- traceable multi-agent handoff

要求的协议：

- `[request]`
- `[confirmed]`
- `[final]`
- `ack_id`
- `NO_REPLY after final`

### 4.4 任务监督器

目标：

- detect "said it would do it, but did not"
- separate `completed` from `delivered`
- detect async silent failures

要求的能力：

- task registration
- polling/watch cycle
- delivery confirmation
- dead-letter isolation

## 5. 初始化方案

health-monitor 必须能为 OpenClaw 初始化一套最小可用工作区。

### 5.1 工作区基础结构

每个受管工作区必须包含：

- `SOUL.md`
- `AGENTS.md`
- `MEMORY.md`
- `memory/`
- `.learnings/ERRORS.md`
- `.learnings/LEARNINGS.md`
- `.learnings/FEATURE_REQUESTS.md`
- `shared-context/intel/`
- `shared-context/status/`
- `shared-context/job-status/`
- `shared-context/monitor-tasks/tasks.jsonl`

### 5.2 SOUL 初始基线

`SOUL.md` 必须定义：

- agent identity
- non-negotiable constraints
- decision hierarchy
- what the agent may not self-modify

硬规则：

- `SOUL.md` is human-owned
- the agent must not rewrite it automatically

### 5.3 AGENTS 初始基线

`AGENTS.md` 必须定义：

- multi-agent collaboration protocol
- message formats
- `ack_id` usage
- final convergence behavior
- no-reply rule after final
- shared-context usage conventions

### 5.4 MEMORY 初始基线

`MEMORY.md` 必须保持简短、稳定。它用于保存：

- durable user preferences
- validated long-term workflows
- validated protocol rules
- promoted learnings only

它不能变成日常噪音的堆积区。

### 5.5 OpenClaw 配置初始基线

初始化阶段必须校验或写入上下文治理的最小运行时基线。

推荐基线：

```json
{
  "compaction": {
    "mode": "safeguard",
    "memoryFlush": {
      "enabled": true,
      "softThresholdTokens": 40000,
      "prompt": "Distill to memory/YYYY-MM-DD.md. Focus: decisions, state changes, lessons, blockers."
    }
  },
  "contextPruning": {
    "mode": "cache-ttl",
    "ttl": "6h",
    "keepLastAssistants": 3
  },
  "session": {
    "reset": {
      "mode": "daily",
      "atHour": 5,
      "idleMinutes": 30
    },
    "maintenance": {
      "pruneAfter": "7d",
      "maxDiskBytes": 104857600
    }
  }
}
```

其中：

- readiness validation
- drift detection
- baseline reporting

OpenClaw 负责：

- actual runtime execution of these controls

## 6. 自进化闭环

必须具备的闭环是：

`Run -> Learn -> Reflect -> Promote -> Reuse`

### 6.1 记录学习项

运行过程中，系统必须能捕获：

- repeated errors
- blocked reasons
- missing delivery patterns
- protocol violations
- user-requested improvements

这些内容必须写入 `.learnings/`。

### 6.2 定时反思

定时反思循环必须能够：

- scan pending learnings
- group repeated patterns
- evaluate promotion threshold
- decide keep / promote / discard

### 6.3 升级记忆

升级规则：

- repeated enough
- high-value enough
- stable enough

升级目标：

- `MEMORY.md` for durable principles
- `Skills/` for reusable workflows/templates
- `shared-context/intel/` for operational rules or team conventions

### 6.4 再利用

后续运行必须能通过这些载体重新使用已升级的经验：

- workspace files
- skills
- shared operational state

系统应该随着时间推移减少重复犯错。

## 7. 任务监督器方案

任务监督器属于 health-monitor。

### 7.1 作用

它用于发现：

- promised-but-undelivered tasks
- silent async failures
- completion without delivery
- output missing after claimed execution

### 7.2 存储

监督器状态存放在：

- `shared-context/monitor-tasks/tasks.jsonl`
- `watcher.log`
- `audit.log`
- `dlq.jsonl`

### 7.3 最小状态集

每个任务至少要跟踪：

- `task_id`
- `source_agent`
- `target_agent`
- `intent`
- `current_state`
- `completed_at`
- `delivered_at`
- `last_checked_at`
- `error_count`

### 7.4 核心规则

`completed != delivered`

这个区分必须在存储、界面和恢复逻辑中都被保留。

## 8. shared-context 方案

`shared-context/` 不是长期记忆，它是跨 Agent 的共享状态层。

最小结构：

- `shared-context/intel/`
- `shared-context/status/`
- `shared-context/job-status/`
- `shared-context/monitor-tasks/`
- `shared-context/tech-radar.json`

使用规则：

- key state goes to files
- chat only triggers or references state
- cross-agent contracts should prefer structured files over free-form messages

## 9. 职责归属表

| Capability | OpenClaw | health-monitor |
|---|---|---|
| Session / queue / orchestration | Primary owner | Observe only |
| Context lifecycle runtime execution | Primary owner | Validate/report |
| Workspace memory files | Primary owner | Bootstrap/init |
| Learning capture | Shared | Shared |
| Reflection / promotion policy | Consumer | Primary owner |
| Shared-context export | Consumer | Primary owner |
| Task Watcher | Consumer of results | Primary owner |
| Environment governance | No | Primary owner |
| Dashboard / ops control plane | No | Primary owner |

## 10. 什么才算“已经实现”

目标不是“文档写了就算做完”。只有同时满足下面三点，才算真正实现：

1. structure exists
2. runtime behavior exists
3. verification exists

例如：

- `.learnings/` folder existing is not enough
- reflection cron existing is not enough
- the system must also show promoted memory and learning backlog in a verifiable
  way

## 11. 验收清单

### 11.1 初始化验收

- workspace contains all required files/directories
- baseline config exists or readiness reports drift
- `SOUL.md` is not auto-mutated
- `AGENTS.md` includes protocol contract

### 11.2 上下文生命周期验收

- readiness reports each required control
- drift is visible when config drops below baseline
- memory/session artifacts are actually rotated over time

### 11.3 学习闭环验收

- blocked/error patterns create learning entries
- reflection runs are recorded
- promotion threshold changes learning state
- `MEMORY.md` updates are bounded and auditable

### 11.4 任务监督器验收

- registered tasks persist
- poller updates state
- `completed` and `delivered` are distinct
- failed delivery enters DLQ

### 11.5 协议验收

- `request / confirmed / final` format is present in templates/rules
- `ack_id` is required by protocol
- final implies no-reply guidance

## 12. 回放 / 回测清单

这套方案必须支持回放式验证。

对于每一类能力，回放时都应该能回答：

- was the state written?
- was the evolution rule triggered?
- was the promotion decision correct?
- did the user-visible memory stay bounded?
- did async completion reach delivery?

最少需要保留这些回放材料：

- task registry snapshot
- learning backlog
- reflection history
- watcher audit log
- promoted memory snapshot

## 13. 最终验收标准

只有满足以下条件，才能认为这套方案真正落地：

1. a fresh OpenClaw workspace can be bootstrapped into the target structure
2. context lifecycle readiness is machine-verifiable
3. `.learnings -> reflection -> promote -> MEMORY` works end-to-end
4. Task Watcher can prove `completed != delivered`
5. shared-context contains structured cross-agent state
6. the system can be audited after the fact without relying on chat transcripts

## 14. 不在本方案范围内的内容

以下内容明确不属于本方案范围：

- replacing OpenClaw orchestration with health-monitor
- intercepting user messages
- building a second internal truth source for agent execution
- promising 100% autonomous correct long-term memory abstraction

## 15. 使用说明

这份文档应被用作：

- the implementation checklist for code changes
- the review checklist before merge
- the replay checklist after runtime incidents

Related documents:

- `docs/context-lifecycle-baseline.md`
- `docs/learning-promotion-policy.md`
- `docs/shared-state-model.md`
- `docs/product-architecture.md`
- `docs/product-backlog.md`
