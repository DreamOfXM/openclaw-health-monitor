#!/usr/bin/env python3
"""
任务闭环看门狗 - 发现"完成但未送达"的任务
"""

import json
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "task-closure"
FACTS_FILE = Path(__file__).parent.parent / "data" / "current-task-facts.json"

def scan_undelivered_tasks():
    """扫描所有完成但未送达的任务"""
    if not DATA_DIR.exists():
        return []
    
    undelivered = []
    for task_file in DATA_DIR.glob("*.json"):
        try:
            with open(task_file) as f:
                task = json.load(f)
            
            if task.get("status") == "completed" and not task.get("delivered"):
                undelivered.append({
                    "task_id": task.get("task_id"),
                    "question": task.get("question", "")[:100],
                    "completed_at": task.get("completed_at"),
                    "age_seconds": int(time.time()) - task.get("completed_at", 0)
                })
        except Exception:
            pass
    
    return undelivered

def check_current_task():
    """检查当前任务是否完成但未送达"""
    if not FACTS_FILE.exists():
        return None
    
    try:
        with open(FACTS_FILE) as f:
            facts = json.load(f)
        
        current = facts.get("current_task", {})
        control_state = current.get("control_state", "")
        delivery_state = current.get("core_truth", {}).get("delivery_state", "")
        
        # 如果 control_state 是 completed_verified 但 delivery_state 不是 delivered
        if control_state == "completed_verified" and delivery_state not in ["delivered", "delivery_confirmed"]:
            return {
                "task_id": current.get("task_id"),
                "control_state": control_state,
                "delivery_state": delivery_state,
                "question": current.get("question", "")[:100]
            }
    except Exception:
        pass
    
    return None

def main():
    print("=== 任务闭环看门狗 ===")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 检查当前任务
    current = check_current_task()
    if current:
        print(f"\n[警告] 当前任务完成但未送达:")
        print(f"  task_id: {current['task_id']}")
        print(f"  control_state: {current['control_state']}")
        print(f"  delivery_state: {current['delivery_state']}")
        print(f"  question: {current['question']}")
    
    # 扫描历史任务
    undelivered = scan_undelivered_tasks()
    if undelivered:
        print(f"\n[警告] 发现 {len(undelivered)} 个完成但未送达的历史任务:")
        for task in undelivered[:5]:
            print(f"  - {task['task_id']}: {task['question']} (age: {task['age_seconds']}s)")
    
    if not current and not undelivered:
        print("\n[正常] 所有任务都已送达")
    
    return 0

if __name__ == "__main__":
    exit(main())
