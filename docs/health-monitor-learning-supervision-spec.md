# Health Monitor 学习监督规范

## 1. 目的

本文件定义 `health-monitor` 如何监督 OpenClaw 学习闭环是否真的发生。

它不定义 OpenClaw 如何学习，而定义外层如何：

- 读取 artifacts
- 派生监督指标
- 导出 shared-state
- 在 Dashboard / API 中展示

关联文档：

- [learning-reflection-rearchitecture.md](/Users/hangzhou/openclaw-health-monitor/docs/learning-reflection-rearchitecture.md)
- [openclaw-learning-artifact-schema.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-artifact-schema.md)
- [openclaw-learning-cron-runtime-spec.md](/Users/hangzhou/openclaw-health-monitor/docs/openclaw-learning-cron-runtime-spec.md)

## 2. 核心监督问题

health-monitor 必须直接回答：

1. 今天有没有新增 learning
2. reflection cron 今天有没有跑
3. promote 有没有产出
4. `MEMORY.md` 有没有更新
5. promoted knowledge 有没有被复用
6. 同类问题后续有没有下降

## 3. 监督字段

建议统一输出以下字段：

- `learning_freshness`
- `reflection_freshness`
- `memory_freshness`
- `promoted_items_count`
- `promoted_items_24h`
- `reuse_evidence_count`
- `reuse_evidence_7d`
- `repeat_error_trend`
- `last_daily_reflection_at`
- `last_memory_maintenance_at`
- `last_team_rollup_at`
- `daily_reflection_status`
- `memory_maintenance_status`
- `team_rollup_status`
- `artifact_status`

## 4. 字段定义

### 4.1 freshness 类

#### `learning_freshness`

- 含义：最近一条 pending/promoted/discarded learning 的更新时间距离当前的秒数
- 派生来源：`.learnings/*.jsonl`

#### `reflection_freshness`

- 含义：最近一次 reflection run 距离当前的秒数
- 派生来源：`.learnings/reflection-runs.jsonl`

#### `memory_freshness`

- 含义：`MEMORY.md` 最近更新时间距离当前的秒数
- 派生来源：文件 mtime 或 memory maintenance record

### 4.2 count 类

#### `promoted_items_count`

- 含义：累计 promoted learning 数

#### `promoted_items_24h`

- 含义：最近 24h 新增 promoted 数

#### `reuse_evidence_count`

- 含义：累计 reuse evidence 数

#### `reuse_evidence_7d`

- 含义：最近 7d reuse evidence 数

### 4.3 trend 类

#### `repeat_error_trend`

- 含义：同类错误在最近窗口内的变化趋势
- 建议值：`down / flat / up / insufficient_data`

### 4.4 status 类

#### `artifact_status`

- 含义：OpenClaw 学习 artifacts 是否齐全
- 建议值：
  - `ready`
  - `partial`
  - `missing`
  - `legacy_store_only`

## 5. shared-state 导出

建议新增这些对象到 `data/shared-state/`：

- `learning-runtime-status.json`
- `reflection-freshness.json`
- `memory-freshness.json`
- `reuse-evidence-summary.json`

### 5.1 learning-runtime-status.json

建议结构：

```json
{
  "generated_at": 1770100000,
  "env_id": "primary",
  "artifact_status": "ready",
  "learning_freshness": 600,
  "reflection_freshness": 3600,
  "memory_freshness": 5400,
  "promoted_items_count": 18,
  "promoted_items_24h": 3,
  "reuse_evidence_count": 11,
  "reuse_evidence_7d": 4,
  "repeat_error_trend": "down"
}
```

### 5.2 reflection-freshness.json

建议结构：

```json
{
  "generated_at": 1770100000,
  "env_id": "primary",
  "last_daily_reflection_at": 1770090000,
  "last_memory_maintenance_at": 1770093600,
  "last_team_rollup_at": 1770060000,
  "daily_reflection_status": "succeeded",
  "memory_maintenance_status": "succeeded",
  "team_rollup_status": "skipped"
}
```

### 5.3 memory-freshness.json

建议结构：

```json
{
  "generated_at": 1770100000,
  "env_id": "primary",
  "memory_path": "MEMORY.md",
  "updated_at": 1770093600,
  "freshness_seconds": 6400,
  "status": "fresh"
}
```

### 5.4 reuse-evidence-summary.json

建议结构：

```json
{
  "generated_at": 1770100000,
  "env_id": "primary",
  "total": 11,
  "last_7d": 4,
  "top_targets": [
    {"type": "Skills", "count": 5},
    {"type": "MEMORY.md", "count": 4}
  ],
  "top_categories": [
    {"category": "delivery_failure", "count": 6}
  ]
}
```

## 6. API 建议

建议在聚合接口中补以下段：

- `/api/health-acceptance`
- `/api/shared-state`

### 6.1 `/api/health-acceptance`

增加：

```json
{
  "learning_supervision": {
    "artifact_status": "ready",
    "learning_freshness": 600,
    "reflection_freshness": 3600,
    "memory_freshness": 5400,
    "promoted_items_count": 18,
    "reuse_evidence_count": 11,
    "repeat_error_trend": "down"
  }
}
```

### 6.2 `/api/shared-state`

增加：

- `learning_runtime_status`
- `reflection_freshness`
- `memory_freshness`
- `reuse_evidence_summary`

## 7. Dashboard 展示建议

学习中心至少展示：

- artifact status
- 最近 learning 更新时间
- 最近 reflection 时间与状态
- 最近 memory maintenance 时间与状态
- promoted items 数
- reuse evidence 数
- repeat error trend

展示原则：

- 先显示 verdict，再显示明细
- 缺失 artifacts 时高亮 `artifact_missing`
- 不再把 guardian 自己的 legacy promote 结果伪装成 OpenClaw 学习结果

## 8. 兼容模式

在 OpenClaw artifacts 还未落地前：

- 允许从 SQLite legacy store 兜底
- 但必须标记 `legacy_store_only`
- UI 上应明确“当前为过渡态，尚未切到 OpenClaw artifact 主路径”

## 9. 完成定义

以下条件满足后，`WP-5` 才算完成：

- health-monitor 能从 OpenClaw artifacts 直接派生监督指标
- shared-state 有明确学习监督对象
- API 能直接返回学习监督字段
- Dashboard 能明确区分：
  - artifact ready
  - artifact missing
  - legacy store only
- 用户可以直接判断“有没有学、学得新不新、学了有没有用”
