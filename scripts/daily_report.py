#!/usr/bin/env python3
"""
每日报告 - 让小忆产出系统报告
"""

import json
import subprocess
from pathlib import Path
from datetime import datetime

WORKSPACE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi")
MEMORY_FILE = WORKSPACE / "MEMORY.md"
FACTS_FILE = Path("/Users/hangzhou/openclaw-health-monitor/data/current-task-facts.json")


def _load_facts() -> dict:
    if not FACTS_FILE.exists():
        return {}
    try:
        return json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _launchctl_running(service_name: str) -> bool:
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        target = f"gui/{uid}/{service_name}"
        result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, timeout=5)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return "state = running" in output
    except Exception:
        return False


def _extract_delivery_state(current: dict) -> str:
    control = current.get("control") if isinstance(current.get("control"), dict) else {}
    core_supervision = control.get("core_supervision") if isinstance(control.get("core_supervision"), dict) else {}
    for value in [
        current.get("delivery_state"),
        control.get("delivery_state"),
        core_supervision.get("delivery_state"),
        core_supervision.get("delivery_confirmation_level"),
    ]:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _delivery_confirmed(current: dict) -> bool:
    control = current.get("control") if isinstance(current.get("control"), dict) else {}
    core_supervision = control.get("core_supervision") if isinstance(control.get("core_supervision"), dict) else {}
    state = _extract_delivery_state(current).lower()
    return state in {"delivered", "delivery_confirmed", "confirmed"} or bool(core_supervision.get("delivery_confirmed"))


def get_system_stats():
    """获取系统统计"""
    stats = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "gateway_healthy": False,
        "guardian_running": False,
        "dashboard_running": False,
        "tasks_completed": 0,
        "tasks_delivered": 0,
        "tasks_blocked_undelivered": 0,
        "issues_found": 0
    }

    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:18789/health"],
            capture_output=True, text=True, timeout=5
        )
        stats["gateway_healthy"] = "ok" in result.stdout
    except Exception:
        pass

    stats["guardian_running"] = _launchctl_running("ai.openclaw.guardian")
    stats["dashboard_running"] = _launchctl_running("ai.openclaw.dashboard")

    facts = _load_facts()
    current = facts.get("current_task") if isinstance(facts.get("current_task"), dict) else {}
    if current:
        control_state = str(current.get("control_state") or "")
        delivery_confirmed = _delivery_confirmed(current)
        if control_state == "completed_verified":
            stats["tasks_completed"] += 1
            if delivery_confirmed:
                stats["tasks_delivered"] += 1
        if control_state in {"blocked_unverified", "blocked_control_followup_failed"} and not delivery_confirmed:
            stats["tasks_blocked_undelivered"] += 1

    learnings_file = WORKSPACE / ".learnings" / "LEARNINGS.md"
    if learnings_file.exists():
        try:
            content = learnings_file.read_text(encoding="utf-8")
            stats["issues_found"] = content.count("## [LRN-")
        except Exception:
            pass

    return stats


def update_memory(stats):
    """更新 MEMORY.md"""
    if not MEMORY_FILE.exists():
        return False

    try:
        report = f"""

---

## 每日统计 - {stats['date']}

- Gateway: {'✅ 正常' if stats['gateway_healthy'] else '❌ 异常'}
- Guardian: {'✅ 运行' if stats['guardian_running'] else '❌ 未运行'}
- Dashboard: {'✅ 运行' if stats['dashboard_running'] else '❌ 未运行'}
- 任务完成: {stats['tasks_completed']}
- 任务送达: {stats['tasks_delivered']}
- 阻塞未送达: {stats['tasks_blocked_undelivered']}
- 发现问题: {stats['issues_found']}
"""
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(report)
        return True
    except Exception:
        return False


def main():
    print(f"=== 每日报告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    print("\n1. 获取系统统计...")
    stats = get_system_stats()

    print(f"   Gateway: {'✅ 正常' if stats['gateway_healthy'] else '❌ 异常'}")
    print(f"   Guardian: {'✅ 运行' if stats['guardian_running'] else '❌ 未运行'}")
    print(f"   Dashboard: {'✅ 运行' if stats['dashboard_running'] else '❌ 未运行'}")
    print(f"   任务完成: {stats['tasks_completed']}")
    print(f"   任务送达: {stats['tasks_delivered']}")
    print(f"   阻塞未送达: {stats['tasks_blocked_undelivered']}")
    print(f"   发现问题: {stats['issues_found']}")

    print("\n2. 更新 MEMORY.md...")
    if update_memory(stats):
        print("   ✅ 已更新")
    else:
        print("   ⚠️ 更新失败")

    return 0


if __name__ == "__main__":
    exit(main())
