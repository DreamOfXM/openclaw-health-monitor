# P0 回执硬门禁闭环

## 1. 上一轮停留阶段判断

判定：**已卡住**。

依据：
- 多轮任务反复落在 `received_only` / `missing_pipeline_receipt` / `blocked_unverified`。
- 说明主链路只看到“任务被接收/执行过”，但没有被验证的结构化下游回执。
- 这不是“仍在正常执行”，也不是“已有结果但仅缺展示”；它已经触发控制面缺证据阻塞逻辑。

## 2. 本轮落地目标

必须同时满足：
1. 无回执自动追证
2. 追证失败自动 `blocked`
3. `blocked` 对主人可见
4. 不允许长期停在 `received_only`
5. 任一智能体没有终态标志时，必须被追问、被收口

## 3. 时间策略

### 3.1 duration profile

| profile | first_ack_sla | heartbeat_interval | soft_followup | hard_followup | hard_timeout | auto_blocked_unverified |
|---|---:|---:|---:|---:|---:|---:|
| short | 30s | 45s | 30s | 75s | 180s | 180s |
| medium | 60s | 120s | 60s | 180s | 900s | 900s |
| long | 120s | 300s | 120s | 420s | 2700s | 2700s |

### 3.2 phase -> profile

| phase | profile |
|---|---|
| planning | short |
| implementation | long |
| testing | medium |
| calculation | short |
| verification | short |
| risk_assessment | short |

## 4. 状态机

```text
received_only
  -> soft followup
  -> hard followup
  -> blocked_unverified

planning_only
  -> require_dev_receipt
  -> dev_running / dev_blocked

dev_running
  -> heartbeat healthy
  -> hard followup when heartbeat stale
  -> awaiting_test / dev_blocked / blocked_unverified

awaiting_test
  -> require_test_receipt
  -> test_running / test_blocked / blocked_unverified

test_running
  -> completed_verified / test_blocked / blocked_unverified
```

## 5. 用户可见模板

- 已开始且心跳正常  
  `已开始且心跳正常：当前阶段=<phase>，心跳窗口=<n>s。`
- 超过窗口正在追证  
  `超过窗口，正在追证：当前阶段=<phase>，已进入<soft|hard>追证窗口。`
- 追证失败已 blocked  
  `追证失败，已 blocked：任务缺少可验证结构化回执，主人当前可见为阻塞状态。`

## 6. 实现落点

- `task_contracts.py` / `task_contracts.json`
  - 增加 `duration_profiles`、`phase_policies`
  - 显式包含 `soft_followup` / `hard_followup` / `auto_blocked_unverified` / `blocked_user_visible`
- `state_store.py`
  - 统一解析 phase/profile timing
  - 在控制面导出 timing 元数据
  - 在 `evidence_summary` 和 `user_visible_progress` 中体现追证/blocked
- `guardian.py`
  - `current-task-facts.json` 导出 timing / followup / heartbeat 事实
  - 控制面 followup 失败后自动转 `blocked_*`

## 7. 验收证明

回归测试至少证明：
- `received_only` 能进入 soft followup
- 心跳超窗能进入 hard followup
- followup 失败后变成 `blocked_unverified` / `blocked_control_followup_failed`
- `current-task-facts.json` / dashboard / state_store 同源
- `completed != delivered/completed_verified`
