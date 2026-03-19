#!/usr/bin/env python3
"""
每日反思 - 让小忆自己发现问题
"""

import json
import time
from pathlib import Path
from datetime import datetime

WORKSPACE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi")
MEMORY_DIR = WORKSPACE / "memory"
LEARNINGS_FILE = WORKSPACE / ".learnings" / "LEARNINGS.md"
FACTS_FILE = Path("/Users/hangzhou/openclaw-health-monitor/data/current-task-facts.json")
GUARDIAN_LOG = Path("/Users/hangzhou/openclaw-health-monitor/logs/guardian.log")

def check_system_health():
    """检查系统健康"""
    issues = []
    
    # 检查 Gateway
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:18789/health"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or "ok" not in result.stdout:
            issues.append("Gateway 健康检查失败")
    except Exception as e:
        issues.append(f"Gateway 检查异常: {e}")
    
    # 检查 Guardian
    try:
        result = subprocess.run(
            ["launchctl", "print", "gui/$(id -u)/ai.openclaw.guardian"],
            capture_output=True, text=True, timeout=5
        )
        if "running" not in result.stdout:
            issues.append("Guardian 未运行")
    except Exception as e:
        issues.append(f"Guardian 检查异常: {e}")
    
    return issues

def check_undelivered_tasks():
    """检查未闭环任务"""
    if not FACTS_FILE.exists():
        return []
    
    try:
        with open(FACTS_FILE) as f:
            facts = json.load(f)
        
        current = facts.get("current_task", {})
        control_state = current.get("control_state", "")
        delivery_state = current.get("core_truth", {}).get("delivery_state", "")
        
        if control_state == "completed_verified" and delivery_state not in ["delivered", "delivery_confirmed"]:
            return [{
                "task_id": current.get("task_id"),
                "question": current.get("question", "")[:100],
                "control_state": control_state,
                "delivery_state": delivery_state
            }]
    except Exception:
        pass
    
    return []

def check_repeated_errors():
    """检查重复错误"""
    if not LEARNINGS_FILE.exists():
        return []
    
    try:
        with open(LEARNINGS_FILE) as f:
            content = f.read()
        
        # 简单统计：如果同一个 pattern 出现多次
        patterns = {}
        for line in content.split("\n"):
            if "Pattern-Key:" in line:
                key = line.split("Pattern-Key:")[1].strip()
                patterns[key] = patterns.get(key, 0) + 1
        
        repeated = [k for k, v in patterns.items() if v > 1]
        return repeated
    except Exception:
        pass
    
    return []

def write_daily_reflection(health_issues, undelivered, repeated_errors):
    """写入每日反思"""
    today = datetime.now().strftime("%Y-%m-%d")
    memory_file = MEMORY_DIR / f"{today}.md"
    
    content = f"""# 每日反思 - {today}

## 系统健康
"""
    
    if health_issues:
        content += "### 发现问题\n"
        for issue in health_issues:
            content += f"- {issue}\n"
    else:
        content += "✅ 系统正常\n"
    
    content += "\n## 未闭环任务\n"
    
    if undelivered:
        content += "### 发现问题\n"
        for task in undelivered:
            content += f"- task_id: {task['task_id']}\n"
            content += f"  question: {task['question']}\n"
            content += f"  control_state: {task['control_state']}\n"
            content += f"  delivery_state: {task['delivery_state']}\n"
    else:
        content += "✅ 所有任务已送达\n"
    
    content += "\n## 重复错误\n"
    
    if repeated_errors:
        content += "### 发现问题\n"
        for pattern in repeated_errors:
            content += f"- {pattern}\n"
    else:
        content += "✅ 无重复错误\n"
    
    content += f"""
## 下一步行动
"""
    
    actions = []
    if health_issues:
        actions.append("- 修复系统健康问题")
    if undelivered:
        actions.append("- 追踪未闭环任务，确保送达主人")
    if repeated_errors:
        actions.append("- 分析重复错误根因，加新约束")
    
    if not actions:
        actions.append("- 继续监控，保持系统稳定")
    
    content += "\n".join(actions)
    
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(memory_file, "w") as f:
        f.write(content)
    
    return memory_file

def main():
    print(f"=== 每日反思 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # 检查系统健康
    print("\n1. 检查系统健康...")
    health_issues = check_system_health()
    if health_issues:
        print(f"   发现 {len(health_issues)} 个问题")
        for issue in health_issues:
            print(f"   - {issue}")
    else:
        print("   ✅ 系统正常")
    
    # 检查未闭环任务
    print("\n2. 检查未闭环任务...")
    undelivered = check_undelivered_tasks()
    if undelivered:
        print(f"   发现 {len(undelivered)} 个未闭环任务")
        for task in undelivered:
            print(f"   - {task['task_id']}: {task['question']}")
    else:
        print("   ✅ 所有任务已送达")
    
    # 检查重复错误
    print("\n3. 检查重复错误...")
    repeated_errors = check_repeated_errors()
    if repeated_errors:
        print(f"   发现 {len(repeated_errors)} 个重复错误模式")
        for pattern in repeated_errors:
            print(f"   - {pattern}")
    else:
        print("   ✅ 无重复错误")
    
    # 写入每日反思
    print("\n4. 写入每日反思...")
    memory_file = write_daily_reflection(health_issues, undelivered, repeated_errors)
    print(f"   ✅ 已写入 {memory_file}")
    
    # 总结
    print("\n=== 总结 ===")
    total_issues = len(health_issues) + len(undelivered) + len(repeated_errors)
    if total_issues > 0:
        print(f"发现 {total_issues} 个问题，需要关注")
    else:
        print("系统稳定，无问题")
    
    return 0

if __name__ == "__main__":
    exit(main())
