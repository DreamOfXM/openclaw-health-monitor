# 学习反思能力重构方案

## 1. 背景

这一轮讨论已经明确了产品边界：

- `OpenClaw` 是执行内核
- `health-monitor` 是外层控制面
- 外层不接管消息入口，不侵入主消息流
- 学习与反思的主路径不依赖 heartbeat
- 学习与反思应回到 OpenClaw 自己的 cron

当前问题不是“要不要学习反思”，而是“谁负责真正学习、谁负责监督学习是否发生”。

现在看起来“反思成了笑话”的原因是：

- `health-monitor` 做了太多像在替 Agent 反思的事情
- 这些记录没有稳定回注到 OpenClaw 的执行层
- 结果是有记录、有面板，但缺少真实行为变化

一句话：

- 让 `OpenClaw` 真正负责“学”
- 让 `health-monitor` 负责“看它有没有学会”

配套执行文档：

- [guardian-learning-migration-checklist.md](/Users/hangzhou/openclaw-health-monitor/docs/guardian-learning-migration-checklist.md)
- [openclaw-learning-implementation-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-implementation-design.md)
- [learning-execution-work-packages.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-execution-work-packages.md)
- [openclaw-learning-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-artifact-schema.md)
- [openclaw-learning-cron-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-cron-runtime-spec.md)
- [health-monitor-learning-supervision-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/health-monitor-learning-supervision-spec.md)
- [openclaw-self-check-heartbeat-design.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-self-check-heartbeat-design.md)

## 2. 新角色定义

### 2.1 OpenClaw 的职责

`OpenClaw` 必须对自己的学习闭环负责。

它负责：

- 自己写 `.learnings`
- 自己运行 daily reflection
- 自己决定是否 promote 到 `MEMORY.md`
- 自己把稳定经验沉淀到 `Skills`
- 自己把经验重新注入后续执行

这意味着：

- 学习判断是 OpenClaw 的认知职责
- promote 决策是 OpenClaw 的认知职责
- 经验复用是 OpenClaw 的执行职责

### 2.2 Health Monitor 的职责

`health-monitor` 不做认知判断，只做外层监督与治理。

它负责：

- 展示 learning backlog
- 展示 reflection runs
- 展示 promoted items
- 审计学习流程是否真的执行
- 展示 `MEMORY.md` / `.learnings/` / shared-state 变化
- 验证同类问题后续是否下降

它不负责：

- 替 OpenClaw 判断什么值得学习
- 替 OpenClaw 做反思结论
- 替 OpenClaw 决定 promote 内容
- 通过外层消息链把所谓“学习结果”塞回主执行链

## 3. 架构原则

### 3.1 不接管消息流

学习反思能力不能通过外层消息 orchestrator 来实现。

原因：

- 外层接管消息流会导致 attribution 混乱
- 会引入 packet loss / dropped progress 风险
- 会让 health-monitor 演化成第二个 brain

因此：

- 用户消息仍直接进入 OpenClaw
- session / queue / subagent state 仍以 OpenClaw 为权威真相
- health-monitor 只读观察、审计、治理、验收

### 3.2 heartbeat 不再承担主责任

新的默认规则：

- heartbeat 继续默认关闭
- `HEARTBEAT.md` 允许保持空
- heartbeat 不作为学习反思主路径

heartbeat 只保留为：

- 可选轻量巡检机制
- 极少量环境自检/保活入口

但它不再承担：

- learning capture 主责任
- reflection 主责任
- memory promotion 主责任

### 3.3 cron 回到 OpenClaw 内部

学习反思正式迁回 OpenClaw 自己的调度体系。

至少需要三类 cron：

1. `daily-reflection`
   - 扫描 `.learnings/` 中 pending 条目
   - 评估保留 / promote / 淘汰
   - 产出 reflection run 记录

2. `memory-maintenance`
   - 压缩 `MEMORY.md`
   - 归档到 `memory/YYYY-MM-DD.md`
   - 保持长期记忆简洁、稳定、可追溯

3. `team-rollup`
   - 由 `main/Zoe` 汇总各 agent 的反思结果
   - 合并团队级经验
   - 形成稳定共识或协作规则

## 4. 学习闭环重定义

新的闭环不再是“有日志、有记录就算学习”，而是强证据制闭环：

`Run -> Learn -> Reflect -> Promote -> Inject -> Reuse`

### 4.1 Learn

每条 learning 至少要带：

- 来源任务 / 日志
- 问题模式
- 重复次数
- 正证据
- 反证据

### 4.2 Reflect

reflection 不是简单汇总，而是做判断：

- keep
- promote
- discard

每次 reflection run 至少要记录：

- 扫描范围
- 审阅条目数
- promote 数
- discard 数
- 结论摘要

### 4.3 Promote

promote 决策必须明确目标位置。

允许的注入位置只有：

- `MEMORY.md`
- `AGENTS.md`
- `Skills`
- 外层 watcher / guardrail 规则

不允许只停留在“state store 里有条记录，但没有实际注入位置”。

### 4.4 Reuse

真正的学习必须在后续任务里可复用。

可验证表现包括：

- 同类错误率下降
- 重复阻塞减少
- 任务完成更稳定
- 人工兜底次数减少

如果没有 reuse 证据，就不能算“真的学会了”。

## 5. Health Monitor 的监督口径

health-monitor 要检查的不是“我替你想了什么”，而是“你有没有真的完成学习流程”。

它需要监督的最小问题集是：

- 今天 `.learnings` 有没有新增
- 今天 reflection cron 跑没跑
- promote 是否有结果
- `MEMORY.md` 是否更新
- 同类问题后续是否下降

因此，外层控制面重点显示：

- learning backlog
- reflection history
- promoted items
- memory freshness
- reuse evidence
- repeat-error trend

## 6. Task Watcher 保持在外层

Task Watcher 仍然属于 `health-monitor`，因为它是基础设施，不是认知学习。

外层继续负责：

- 注册异步任务
- 轮询检查
- 区分 `completed != delivered`
- 维护 DLQ / audit
- 对 silent async failure 做审计

这是运行监督能力，不是学习能力。

## 7. 对现有实现的调整要求

### 7.1 需要弱化的外层行为

当前 health-monitor 中这类行为要逐步退出主路径：

- 由 guardian 主导 learning capture 决策
- 由 guardian 主导 reflection 判断
- 由 guardian 主导 promote 决策

它们可以短期保留为兼容层或迁移辅助，但不应继续被定义为目标架构。

### 7.2 需要增强的外层能力

外层要加强的是监督与验收：

- reflection 是否按计划执行
- promote 是否真正落地到文件/规则
- `MEMORY.md` 是否过期
- reuse 是否有证据
- 学习闭环是否只停留在展示层

### 7.3 需要补齐的 OpenClaw 内部能力

OpenClaw 侧至少要补齐：

- `.learnings` 标准写入协议
- reflection cron
- memory maintenance cron
- team rollup cron
- promote 决策格式
- Skills 注入约定

## 8. 验收标准

重构完成后，以下事实必须成立：

1. learning 的主写入发生在 OpenClaw 内部
2. reflection 的主执行发生在 OpenClaw cron
3. promote 决策由 OpenClaw 产出，不由 health-monitor 代判
4. health-monitor 能展示 backlog / reflection / promoted / memory freshness
5. health-monitor 能证明学习闭环是否真的发生
6. Task Watcher 继续由外层维护
7. heartbeat 不再是学习反思主路径

## 9. 落地顺序

建议按以下顺序推进：

### Phase 1：边界收口

- 明确文档与产品口径
- 把 health-monitor 对学习的职责改写为监督/展示/审计
- 把 heartbeat 从主路径描述中移除

### Phase 2：OpenClaw 内部学习循环

- 落地 `.learnings` 写入协议
- 落地 daily reflection cron
- 落地 memory maintenance cron
- 落地 team rollup cron

### Phase 3：外层监督验收

- dashboard 展示 learning backlog / reflection history / promoted items
- shared-state 导出 learning freshness / reflection freshness / memory freshness
- 增加“学习是否真的发生”的验收指标

### Phase 4：闭环验证

- 验证 promote 到 `MEMORY.md` / `AGENTS.md` / `Skills` 的路径
- 验证后续任务 reuse 证据
- 验证重复错误率下降趋势

## 10. 最终形态

最终形态应当是：

- `OpenClaw`：自己学习、自己反思、自己成长
- `health-monitor`：看板、监督、审计、治理、验收

这不是把学习能力删除，而是把学习能力放回正确的位置。
