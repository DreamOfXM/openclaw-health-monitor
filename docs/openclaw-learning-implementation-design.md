# OpenClaw 学习闭环实施设计

## 1. 目标

本设计用于定义 OpenClaw 侧如何真正承担：

- learn
- reflect
- promote
- inject
- reuse

并与外层 `health-monitor` 形成明确分工：

- OpenClaw 负责学习本身
- health-monitor 负责监督学习是否发生

关联文档：

- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)
- [guardian-learning-migration-checklist.md](/Users/hangzhou/openclaw-health-monitor/docs/guardian-learning-migration-checklist.md)
- [learning-execution-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-execution-work-packages.md)
- [openclaw-learning-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-artifact-schema.md)
- [openclaw-learning-cron-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-cron-runtime-spec.md)
- [health-monitor-learning-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-learning-supervision-spec.md)

## 2. 核心原则

### 2.1 学习必须发生在 OpenClaw 内部

原因：

- 只有 OpenClaw 掌握真实 session / queue / subagent 上下文
- 只有 OpenClaw 能把学习结果稳定注入下一次执行
- 外层控制面无法可靠承担认知判断

### 2.2 学习必须是强证据制

每条 learning 至少要包含：

- `learning_id`
- `source_task_id`
- `source_session_id`
- `source_agent`
- `category`
- `summary`
- `detail`
- `positive_evidence`
- `negative_evidence`
- `occurrences`
- `decision`
- `injection_target`
- `updated_at`

### 2.3 promote 必须有明确注入位置

只允许四类目标：

- `MEMORY.md`
- `AGENTS.md`
- `Skills/`
- `watcher / guardrail rules`

没有注入位置的 promote 不算完成。

## 3. 文件与目录模型

OpenClaw workspace 至少包含：

- `SOUL.md`
- `AGENTS.md`
- `MEMORY.md`
- `memory/`
- `.learnings/`
- `Skills/`
- `shared-context/`

推荐结构：

```text
.learnings/
  pending.jsonl
  promoted.jsonl
  discarded.jsonl
  ERRORS.md
  LEARNINGS.md
  FEATURE_REQUESTS.md
memory/
  YYYY-MM-DD.md
Skills/
  <skill-name>.md
shared-context/
  intel/
  status/
  learning/
```

说明：

- `jsonl` 是机器可读主记录
- `md` 是人类可读视图
- `MEMORY.md` 只保留稳定、长期、短小的原则

## 4. 学习数据模型

建议每条 learning 使用如下结构：

```json
{
  "learning_id": "lrn_20260312_xxx",
  "source_task_id": "task-123",
  "source_session_id": "session-abc",
  "source_agent": "dev",
  "category": "delivery_failure",
  "summary": "Final reply often missing after implementation completion",
  "detail": "Implementation completed twice without visible delivery in Feishu thread",
  "positive_evidence": [
    "logs/openclaw-primary.log: PIPELINE_RECEIPT dev completed",
    "watcher task marked completed but not delivered"
  ],
  "negative_evidence": [
    "one similar task delivered successfully after retry"
  ],
  "occurrences": 3,
  "decision": "promoted",
  "injection_target": {
    "type": "Skills",
    "path": "Skills/delivery-finalization.md"
  },
  "created_at": 1770000000,
  "updated_at": 1770003600
}
```

## 5. 三类 cron 设计

### 5.1 daily-reflection

职责：

- 扫描 pending learning
- 聚合重复模式
- 做 keep / promote / discard 决策
- 输出 reflection run

输入：

- `.learnings/pending.jsonl`
- 运行日志
- 任务结果与失败样本

输出：

- `.learnings/promoted.jsonl`
- `.learnings/discarded.jsonl`
- reflection run record
- 需要更新的 memory / skills / agents patch plan

决策结果：

- `keep`
- `promote`
- `discard`

### 5.2 memory-maintenance

职责：

- 清理和压缩 `MEMORY.md`
- 将当日细节归档到 `memory/YYYY-MM-DD.md`
- 去重和保留稳定原则

约束：

- `MEMORY.md` 只保留 promoted durable principles
- 不允许无限膨胀
- 不允许把短期噪声直接写进长期记忆

### 5.3 team-rollup

职责：

- 汇总 `main / Zoe / subagents` 的 reflection 结果
- 提炼团队级协作规则
- 决定是否进入 `AGENTS.md` 或团队级 `Skills`

适合处理：

- 多 agent 分工边界
- 交付协议
- 回执规范
- 协作模板

## 6. Promote 决策规则

### 6.1 promote 判定条件

满足以下条件才可 promote：

- 重复次数达到阈值
- 证据充分
- 模式稳定
- 有明确收益
- 有明确注入位置

### 6.2 注入位置选择规则

#### 写入 `MEMORY.md`

适合：

- 长期稳定原则
- 高层行为约束
- 长期避免重复错误的原则

#### 写入 `AGENTS.md`

适合：

- agent 分工规则
- 协作协议
- 角色职责边界

#### 写入 `Skills/`

适合：

- 可复用操作流程
- 特定任务模板
- 标准执行步骤

#### 写入 watcher / guardrail rules

适合：

- 外层运行治理规则
- completed != delivered 类防护
- protocol guard

## 7. Reuse 设计

真正的学习必须在运行前或运行中被重新注入。

建议 reuse 路径：

### 7.1 启动注入

OpenClaw 启动时载入：

- `SOUL.md`
- `AGENTS.md`
- `MEMORY.md`
- 关键 `Skills`

### 7.2 任务级注入

任务开始时根据任务类型选择：

- 相关 `Skills`
- 相关近期 learning
- 相关 shared-context rules

### 7.3 协作级注入

subagent spawn 时附带：

- 当前任务相关约束
- 已 promote 的协作模式
- 必要的 receipt / delivery discipline

## 8. Reflection Run 记录格式

建议每次 reflection run 都输出统一记录：

```json
{
  "run_id": "refl_20260312_daily",
  "run_type": "daily-reflection",
  "scope": {
    "pending_count": 12,
    "agents": ["main", "dev", "test"]
  },
  "decisions": {
    "kept": 6,
    "promoted": 3,
    "discarded": 3
  },
  "promoted_targets": {
    "MEMORY.md": 1,
    "AGENTS.md": 1,
    "Skills": 1
  },
  "summary": "delivery finalization and receipt discipline promoted",
  "created_at": 1770003600
}
```

## 9. 与 Health Monitor 的接口

OpenClaw 不需要让外层参与认知判断，但需要暴露可审计事实。

建议 health-monitor 读取或同步以下内容：

- `.learnings/pending.jsonl`
- `.learnings/promoted.jsonl`
- `.learnings/discarded.jsonl`
- reflection run records
- `MEMORY.md` 更新时间
- `memory/YYYY-MM-DD.md` 最近归档
- promoted targets 变更记录
- reuse evidence summary

health-monitor 只做：

- freshness check
- diff / audit
- dashboard display
- repeat-error trend analysis

## 10. 最小实施顺序

### Step 1：主记录格式

- 定义 `.learnings/*.jsonl` 结构
- 定义 reflection run 结构
- 定义 promoted target 结构

### Step 2：daily-reflection

- 实现 pending scan
- 实现 keep / promote / discard 决策输出

### Step 3：memory-maintenance

- 实现 `MEMORY.md` 压缩
- 实现 `memory/YYYY-MM-DD.md` 归档

### Step 4：team-rollup

- 汇总多 agent reflection
- 写入 `AGENTS.md` / `Skills`

### Step 5：reuse evidence

- 记录某条 promoted knowledge 在后续任务中的命中与效果

## 11. 完成定义

以下条件全部满足才算 OpenClaw 学习闭环真正落地：

- OpenClaw 能自己写 `.learnings`
- OpenClaw 能自己跑 reflection cron
- OpenClaw 能自己做 promote 决策
- promote 能落到 `MEMORY.md / AGENTS.md / Skills / guardrail rules`
- 后续任务能看到 reuse evidence
- health-monitor 能证明这些事情真的发生了
