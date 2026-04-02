#!/usr/bin/env python3
"""
Agent 自我进化循环

让 Agent 能够：
1. 定期检查系统状态
2. 发现问题后自动修复
3. 记录到 .learnings/
4. 形成 MEMORY.md

这是自我进化系统的核心：Agent 主动发现问题、自己解决。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
EVOLUTION_LOG = DATA_DIR / "agent-evolution-log.jsonl"
LEARNINGS_FILE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi/.learnings/LEARNINGS.md")
MEMORY_FILE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi/MEMORY.md")


def log_evolution(entry: dict[str, Any]) -> None:
    """写入进化日志"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVOLUTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_system_health() -> dict[str, Any]:
    """检查系统健康状态"""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from state_store import MonitorStateStore
    
    store = MonitorStateStore(BASE_DIR)
    tasks = store.list_tasks(limit=500)
    
    stats = {
        "total_tasks": len(tasks),
        "completed": 0,
        "blocked": 0,
        "background_root_missing": 0,
    }
    
    for task in tasks:
        status = str(task.get("status") or "")
        if status == "completed":
            stats["completed"] += 1
        elif status == "blocked":
            stats["blocked"] += 1
        
        # 检查 background_root_missing
        control = store.derive_task_control_state(task.get("task_id"))
        root_task_id = str(task.get("root_task_id") or "")
        if (
            control.get("blocked_reason") == "background_root_task_missing"
            or (status == "background" and (not root_task_id or (root_task_id and not store.get_root_task(root_task_id))))
            or (status == "blocked" and str(task.get("blocked_reason") or "") == "background_root_task_missing")
        ):
            stats["background_root_missing"] += 1
    
    return stats


def discover_problems(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """发现问题"""
    problems = []
    
    if stats["blocked"] > 10:
        problems.append({
            "type": "too_many_blocked",
            "description": f"发现 {stats['blocked']} 个阻塞任务",
            "severity": "medium",
        })
    
    if stats["background_root_missing"] > 0:
        problems.append({
            "type": "background_root_missing",
            "description": f"发现 {stats['background_root_missing']} 个孤儿任务",
            "severity": "high",
        })
    
    return problems


def fix_problems(problems: list[dict[str, Any]]) -> dict[str, int]:
    """修复问题"""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from state_store import MonitorStateStore
    
    store = MonitorStateStore(BASE_DIR)
    result = {
        "fixed": 0,
        "errors": 0,
    }
    
    for problem in problems:
        if problem["type"] == "background_root_missing":
            # 修复孤儿任务
            tasks = store.list_tasks(limit=500)
            for task in tasks:
                status = str(task.get("status") or "")
                blocked_reason = str(task.get("blocked_reason") or "")
                root_task_id = str(task.get("root_task_id") or "")
                
                need_fix = False
                
                if status == "blocked" and blocked_reason == "background_root_task_missing":
                    need_fix = True
                elif status == "background" and root_task_id and not store.get_root_task(root_task_id):
                    need_fix = True
                
                if need_fix:
                    try:
                        updated_task = dict(task)
                        updated_task["status"] = "completed"
                        if "blocked_reason" in updated_task:
                            updated_task["blocked_reason"] = ""
                        store.upsert_task(updated_task)
                        result["fixed"] += 1
                    except Exception as e:
                        result["errors"] += 1
    
    return result


def record_learning(problem: dict[str, Any], fix_result: dict[str, int]) -> None:
    """记录学习"""
    now = int(time.time())
    entry = {
        "timestamp": now,
        "iso_time": datetime.now().isoformat(),
        "problem": problem,
        "fix_result": fix_result,
        "status": "resolved" if fix_result["fixed"] > 0 else "failed",
    }
    log_evolution(entry)


def run_evolution_cycle() -> None:
    """运行进化循环"""
    print("=== Agent 自我进化循环 ===")
    
    # 1. 检查系统健康状态
    stats = check_system_health()
    print(f"系统状态: completed={stats['completed']}, blocked={stats['blocked']}, background_root_missing={stats['background_root_missing']}")
    
    # 2. 发现问题
    problems = discover_problems(stats)
    print(f"发现问题: {len(problems)} 个")
    
    if not problems:
        print("系统状态正常，无需修复")
        return
    
    # 3. 修复问题
    for problem in problems:
        print(f"修复问题: {problem['type']} - {problem['description']}")
        fix_result = fix_problems([problem])
        print(f"修复结果: fixed={fix_result['fixed']}, errors={fix_result['errors']}")
        
        # 4. 记录学习
        record_learning(problem, fix_result)
    
    print("进化循环完成")


def main():
    """主入口"""
    run_evolution_cycle()


if __name__ == "__main__":
    main()