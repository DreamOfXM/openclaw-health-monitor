#!/usr/bin/env python3
"""
每日报告 - 让小忆产出系统报告
"""

import json
import time
from pathlib import Path
from datetime import datetime

WORKSPACE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi")
MEMORY_FILE = WORKSPACE / "MEMORY.md"

def get_system_stats():
    """获取系统统计"""
    stats = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "gateway_healthy": False,
        "guardian_running": False,
        "dashboard_running": False,
        "tasks_completed": 0,
        "tasks_delivered": 0,
        "issues_found": 0
    }
    
    # 检查 Gateway
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:18789/health"],
            capture_output=True, text=True, timeout=5
        )
        stats["gateway_healthy"] = "ok" in result.stdout
    except Exception:
        pass
    
    # 检查 Guardian
    try:
        result = subprocess.run(
            ["launchctl", "print", "gui/$(id -u)/ai.openclaw.guardian"],
            capture_output=True, text=True, timeout=5
        )
        stats["guardian_running"] = "running" in result.stdout
    except Exception:
        pass
    
    # 检查 Dashboard
    try:
        result = subprocess.run(
            ["launchctl", "print", "gui/$(id -u)/ai.openclaw.dashboard"],
            capture_output=True, text=True, timeout=5
        )
        stats["dashboard_running"] = "running" in result.stdout
    except Exception:
        pass
    
    # 统计任务
    facts_file = Path("/Users/hangzhou/openclaw-health-monitor/data/current-task-facts.json")
    if facts_file.exists():
        try:
            with open(facts_file) as f:
                facts = json.load(f)
            
            current = facts.get("current_task", {})
            if current.get("control_state") == "completed_verified":
                stats["tasks_completed"] += 1
                if current.get("core_truth", {}).get("delivery_state") in ["delivered", "delivery_confirmed"]:
                    stats["tasks_delivered"] += 1
        except Exception:
            pass
    
    # 统计问题
    learnings_file = WORKSPACE / ".learnings" / "LEARNINGS.md"
    if learnings_file.exists():
        try:
            with open(learnings_file) as f:
                content = f.read()
            stats["issues_found"] = content.count("## [LRN-")
        except Exception:
            pass
    
    return stats

def update_memory(stats):
    """更新 MEMORY.md"""
    if not MEMORY_FILE.exists():
        return
    
    try:
        with open(MEMORY_FILE) as f:
            content = f.read()
        
        # 在文件末尾添加每日统计
        report = f"""

---

## 每日统计 - {stats['date']}

- Gateway: {'✅ 正常' if stats['gateway_healthy'] else '❌ 异常'}
- Guardian: {'✅ 运行' if stats['guardian_running'] else '❌ 未运行'}
- Dashboard: {'✅ 运行' if stats['dashboard_running'] else '❌ 未运行'}
- 任务完成: {stats['tasks_completed']}
- 任务送达: {stats['tasks_delivered']}
- 发现问题: {stats['issues_found']}
"""
        
        # 追加到文件
        with open(MEMORY_FILE, "a") as f:
            f.write(report)
        
        return True
    except Exception:
        return False

def main():
    print(f"=== 每日报告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # 获取系统统计
    print("\n1. 获取系统统计...")
    stats = get_system_stats()
    
    print(f"   Gateway: {'✅ 正常' if stats['gateway_healthy'] else '❌ 异常'}")
    print(f"   Guardian: {'✅ 运行' if stats['guardian_running'] else '❌ 未运行'}")
    print(f"   Dashboard: {'✅ 运行' if stats['dashboard_running'] else '❌ 未运行'}")
    print(f"   任务完成: {stats['tasks_completed']}")
    print(f"   任务送达: {stats['tasks_delivered']}")
    print(f"   发现问题: {stats['issues_found']}")
    
    # 更新 MEMORY.md
    print("\n2. 更新 MEMORY.md...")
    if update_memory(stats):
        print("   ✅ 已更新")
    else:
        print("   ⚠️ 更新失败")
    
    return 0

if __name__ == "__main__":
    exit(main())
