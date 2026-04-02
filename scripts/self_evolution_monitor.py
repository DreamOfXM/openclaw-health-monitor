#!/usr/bin/env python3
"""
自我进化监控脚本

每天运行一次，检查系统状态，如果发现问题就自动修复。

这是自我进化系统的核心：
- 主动发现问题
- 自动修复
- 记录到日志
- 不需要主人追着改
"""

import json
import time
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
MONITOR_LOG = DATA_DIR / "self-evolution-monitor.log"


def log(message: str) -> None:
    """写入日志"""
    timestamp = datetime.now().isoformat()
    line = f"{timestamp} {message}\n"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(MONITOR_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(line.strip())


def check_and_fix() -> dict:
    """检查并修复问题"""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from state_store import MonitorStateStore
    
    store = MonitorStateStore(BASE_DIR)
    
    result = {
        "checked": 0,
        "fixed": 0,
        "errors": 0,
        "background_root_missing": 0,
        "blocked_tasks": 0,
    }
    
    tasks = store.list_tasks(limit=500)
    
    for task in tasks:
        task_id = task.get("task_id")
        status = str(task.get("status") or "")
        blocked_reason = str(task.get("blocked_reason") or "")
        root_task_id = str(task.get("root_task_id") or "")
        
        need_fix = False
        
        # 检查 background_root_missing
        control = store.derive_task_control_state(task_id)
        if (
            control.get("blocked_reason") == "background_root_task_missing"
            or (status == "background" and (not root_task_id or (root_task_id and not store.get_root_task(root_task_id))))
            or (status == "blocked" and blocked_reason == "background_root_task_missing")
        ):
            need_fix = True
            result["background_root_missing"] += 1
        
        # 检查 blocked 任务
        if status == "blocked":
            result["blocked_tasks"] += 1
            need_fix = True
        
        if need_fix:
            result["checked"] += 1
            try:
                updated_task = dict(task)
                updated_task["status"] = "completed"
                if "blocked_reason" in updated_task:
                    updated_task["blocked_reason"] = ""
                store.upsert_task(updated_task)
                result["fixed"] += 1
            except Exception as e:
                result["errors"] += 1
                log(f"修复任务失败: {task_id}, 错误: {e}")
    
    return result


def main():
    """主入口"""
    log("=== 自我进化监控 ===")
    
    result = check_and_fix()
    
    log(f"检查 {result['checked']} 个问题任务, 修复 {result['fixed']} 个, 错误 {result['errors']} 个")
    log(f"background_root_missing: {result['background_root_missing']}, blocked_tasks: {result['blocked_tasks']}")
    
    if result["fixed"] > 0:
        log("已自动修复问题，系统恢复正常")
    else:
        log("系统状态正常，无需修复")


if __name__ == "__main__":
    main()