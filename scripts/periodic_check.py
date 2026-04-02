#!/usr/bin/env python3
"""
定期检查机制

每天检查系统状态，每周检查记忆，每月检查规则。

这是自我进化系统的核心：定期检查，持续进化。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CHECK_LOG = DATA_DIR / "periodic-check.log"


def log(message: str) -> None:
    """写入日志"""
    timestamp = datetime.now().isoformat()
    line = f"{timestamp} {message}\n"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHECK_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(line.strip())


def check_system_status() -> dict[str, Any]:
    """检查系统状态"""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from state_store import MonitorStateStore
    
    store = MonitorStateStore(BASE_DIR)
    tasks = store.list_tasks(limit=500)
    
    stats = {
        "total_tasks": len(tasks),
        "completed": 0,
        "running": 0,
        "background": 0,
        "blocked": 0,
        "background_root_missing": 0,
    }
    
    for task in tasks:
        status = str(task.get("status") or "")
        if status == "completed":
            stats["completed"] += 1
        elif status == "running":
            stats["running"] += 1
        elif status == "background":
            stats["background"] += 1
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


def check_memory() -> dict[str, Any]:
    """检查记忆"""
    memory_dir = BASE_DIR.parent / "workspace-xiaoyi" / "memory"
    
    stats = {
        "memory_files": 0,
        "total_lines": 0,
        "issues": [],
    }
    
    if memory_dir.exists():
        for file in memory_dir.glob("*.md"):
            stats["memory_files"] += 1
            try:
                content = file.read_text(encoding="utf-8")
                lines = content.split("\n")
                stats["total_lines"] += len(lines)
            except Exception as e:
                stats["issues"].append(f"无法读取 {file.name}: {e}")
    
    return stats


def check_rules() -> dict[str, Any]:
    """检查规则"""
    agents_file = BASE_DIR.parent / "workspace-xiaoyi" / "AGENTS.md"
    
    stats = {
        "rules_count": 0,
        "issues": [],
    }
    
    if agents_file.exists():
        try:
            content = agents_file.read_text(encoding="utf-8")
            # 统计规则数量（以 ### 开头的行）
            stats["rules_count"] = content.count("### ")
        except Exception as e:
            stats["issues"].append(f"无法读取 AGENTS.md: {e}")
    
    return stats


def run_daily_check() -> None:
    """每天检查"""
    log("=== 每日检查 ===")
    
    # 检查系统状态
    stats = check_system_status()
    log(f"系统状态: total={stats['total_tasks']}, completed={stats['completed']}, blocked={stats['blocked']}, background_root_missing={stats['background_root_missing']}")
    
    # 如果有问题，记录下来
    if stats["background_root_missing"] > 0:
        log(f"⚠️ 发现 {stats['background_root_missing']} 个孤儿任务")
    if stats["blocked"] > 10:
        log(f"⚠️ 发现 {stats['blocked']} 个阻塞任务")


def run_weekly_check() -> None:
    """每周检查"""
    log("=== 每周检查 ===")
    
    # 检查记忆
    stats = check_memory()
    log(f"记忆状态: files={stats['memory_files']}, lines={stats['total_lines']}")
    
    if stats["issues"]:
        for issue in stats["issues"]:
            log(f"⚠️ {issue}")


def run_monthly_check() -> None:
    """每月检查"""
    log("=== 每月检查 ===")
    
    # 检查规则
    stats = check_rules()
    log(f"规则状态: rules={stats['rules_count']}")
    
    if stats["issues"]:
        for issue in stats["issues"]:
            log(f"⚠️ {issue}")


def main():
    """主入口"""
    run_daily_check()
    run_weekly_check()
    run_monthly_check()


if __name__ == "__main__":
    main()