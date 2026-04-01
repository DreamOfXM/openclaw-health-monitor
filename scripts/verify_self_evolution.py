#!/usr/bin/env python3
"""
自我进化验证脚本

每天运行一次，记录关键指标，追踪问题数量变化。

指标：
1. background_root_missing - 孤儿任务数量
2. pending_actions - 待执行的 action 数量
3. blocked_actions - 阻塞的 action 数量
4. stalled_received_only - 长期停留在 received_only 的任务数量
5. tasks_without_root_binding - 缺失 root_task 绑定的任务数量

输出：
- 写入 data/self-evolution-metrics.jsonl
- 每天一条记录
"""

import json
import time
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
METRICS_FILE = DATA_DIR / "self-evolution-metrics.jsonl"


def load_store():
    """加载数据库"""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from state_store import MonitorStateStore
    return MonitorStateStore(BASE_DIR)


def count_background_root_missing(store) -> int:
    """统计 background_root_missing 问题数量"""
    tasks = store.list_tasks(limit=500)
    count = 0
    for task in tasks:
        control = store.derive_task_control_state(task["task_id"])
        status = str(task.get("status") or "")
        root_task_id = str(task.get("root_task_id") or "")
        if (
            control.get("blocked_reason") == "background_root_task_missing"
            or (status == "background" and (not root_task_id or (root_task_id and not store.get_root_task(root_task_id))))
            or (status == "blocked" and str(task.get("blocked_reason") or "") == "background_root_task_missing")
        ):
            count += 1
    return count


def count_stalled_received_only(store) -> int:
    """统计长期停留在 received_only 的任务数量"""
    tasks = store.list_tasks(limit=500)
    now = int(time.time())
    count = 0
    for task in tasks:
        control = store.derive_task_control_state(task["task_id"])
        updated_at = int(task.get("updated_at") or task.get("created_at") or now)
        idle = now - updated_at
        if control.get("control_state") == "received_only" and idle >= 180:
            count += 1
    return count


def count_action_status(store) -> dict[str, int]:
    """统计 action 状态"""
    from collections import Counter
    tasks = store.list_tasks(limit=500)
    statuses = []
    for task in tasks:
        action = store.get_open_control_action(task["task_id"])
        if action:
            statuses.append(action["status"])
    return dict(Counter(statuses))


def count_tasks_without_root_binding(store) -> int:
    """统计缺失 root_task 绑定的任务数量"""
    tasks = store.list_tasks(limit=500)
    count = 0
    for task in tasks:
        status = str(task.get("status") or "")
        root_task_id = str(task.get("root_task_id") or "")
        if status in ("background", "running") and not root_task_id:
            count += 1
    return count


def run_verification() -> dict:
    """运行验证，返回指标"""
    store = load_store()
    now = int(time.time())
    
    action_status = count_action_status(store)
    
    metrics = {
        "timestamp": now,
        "iso_time": datetime.now().isoformat(),
        "background_root_missing": count_background_root_missing(store),
        "stalled_received_only": count_stalled_received_only(store),
        "tasks_without_root_binding": count_tasks_without_root_binding(store),
        "pending_actions": action_status.get("pending", 0),
        "blocked_actions": action_status.get("blocked", 0),
        "sent_actions": action_status.get("sent", 0),
        "resolved_actions": action_status.get("resolved", 0),
    }
    
    return metrics


def save_metrics(metrics: dict) -> None:
    """保存指标到文件"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(METRICS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(metrics, ensure_ascii=False) + "\n")


def should_run_today(last_run_file: Path = DATA_DIR / "self-evolution-metrics-last-run.txt") -> bool:
    """检查今天是否已经运行过"""
    if not last_run_file.exists():
        return True
    try:
        last_run = int(last_run_file.read_text().strip())
        now = int(time.time())
        # 24小时内不重复运行
        return (now - last_run) > 86400
    except:
        return True


def mark_run_today(last_run_file: Path = DATA_DIR / "self-evolution-metrics-last-run.txt") -> None:
    """标记今天已运行"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    last_run_file.write_text(str(int(time.time())))


def main():
    """主入口"""
    if not should_run_today():
        print("今天已经运行过，跳过")
        return
    
    print("=== 自我进化验证 ===")
    print(f"时间: {datetime.now().isoformat()}")
    
    metrics = run_verification()
    
    print("\n指标:")
    for key, value in metrics.items():
        if key not in ("timestamp", "iso_time"):
            print(f"  {key}: {value}")
    
    save_metrics(metrics)
    mark_run_today()
    
    print(f"\n指标已保存到: {METRICS_FILE}")


if __name__ == "__main__":
    main()