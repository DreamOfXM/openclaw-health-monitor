#!/usr/bin/env python3
"""
每日反思 - 让小忆自己发现问题
"""

import json
import subprocess
from pathlib import Path
from datetime import datetime

WORKSPACE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi")
MEMORY_DIR = WORKSPACE / "memory"
LEARNINGS_FILE = WORKSPACE / ".learnings" / "LEARNINGS.md"
FACTS_FILE = Path("/Users/hangzhou/openclaw-health-monitor/data/current-task-facts.json")
GUARDIAN_LOG = Path("/Users/hangzhou/openclaw-health-monitor/logs/guardian.log")


def _load_facts() -> dict:
    if not FACTS_FILE.exists():
        return {}
    try:
        return json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_current_task(facts: dict) -> dict:
    current = facts.get("current_task")
    return current if isinstance(current, dict) else {}


def _extract_delivery_state(current: dict) -> str:
    control = current.get("control") if isinstance(current.get("control"), dict) else {}
    core_supervision = control.get("core_supervision") if isinstance(control.get("core_supervision"), dict) else {}
    current_delivery = current.get("current_delivery_attempt") if isinstance(current.get("current_delivery_attempt"), dict) else {}

    candidates = [
        current.get("delivery_state"),
        control.get("delivery_state"),
        current_delivery.get("delivery_state"),
        current_delivery.get("current_state"),
        core_supervision.get("delivery_state"),
        core_supervision.get("delivery_confirmation_level"),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _is_delivery_confirmed(current: dict) -> bool:
    control = current.get("control") if isinstance(current.get("control"), dict) else {}
    core_supervision = control.get("core_supervision") if isinstance(control.get("core_supervision"), dict) else {}
    flags = control.get("flags") if isinstance(control.get("flags"), dict) else {}
    delivery_state = _extract_delivery_state(current).lower()

    if delivery_state in {"delivered", "delivery_confirmed", "confirmed"}:
        return True
    if bool(core_supervision.get("delivery_confirmed")):
        return True
    # visible_completion 不是 delivery confirmed，但它至少说明用户看到了结果；
    # 这里保留为 False，让反思继续把“可见但未确认送达”和“不可见”都视作需要关注。
    _ = flags.get("visible_completion")
    return False


def _launchctl_print(service_name: str) -> str:
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        target = f"gui/{uid}/{service_name}"
        result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, timeout=5)
        return result.stdout or result.stderr or ""
    except Exception as e:
        return f"ERROR: {e}"


def check_system_health():
    """检查系统健康"""
    issues = []

    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:18789/health"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or "ok" not in result.stdout:
            issues.append("Gateway 健康检查失败")
    except Exception as e:
        issues.append(f"Gateway 检查异常: {e}")

    guardian_output = _launchctl_print("ai.openclaw.guardian")
    if "ERROR:" in guardian_output or "state = running" not in guardian_output:
        issues.append("Guardian 未运行")

    return issues


def check_undelivered_tasks():
    """检查未闭环/未送达任务"""
    facts = _load_facts()
    if not facts:
        return []

    current = _get_current_task(facts)
    if not current:
        return []

    control = current.get("control") if isinstance(current.get("control"), dict) else {}
    flags = control.get("flags") if isinstance(control.get("flags"), dict) else {}
    core_supervision = control.get("core_supervision") if isinstance(control.get("core_supervision"), dict) else {}

    task_id = current.get("task_id")
    control_state = str(current.get("control_state") or "")
    evidence_level = str(current.get("evidence_level") or "")
    next_action = str(current.get("next_action") or "")
    delivery_state = _extract_delivery_state(current)
    delivery_confirmed = _is_delivery_confirmed(current)
    visible_completion = bool(flags.get("visible_completion"))
    is_closed = bool(current.get("is_closed"))
    blocked = control_state in {"blocked_unverified", "blocked_control_followup_failed"} or bool(core_supervision.get("is_blocked"))
    pending_followup = next_action in {"require_receipt_or_block", "await_delivery_confirmation", "delivery_retry"}

    should_flag = False
    reason = ""

    if blocked and not delivery_confirmed:
        should_flag = True
        reason = "blocked_not_delivered"
    elif control_state == "completed_verified" and not delivery_confirmed:
        should_flag = True
        reason = "completed_not_delivered"
    elif pending_followup and not delivery_confirmed:
        should_flag = True
        reason = "followup_pending_without_delivery"
    elif is_closed and not delivery_confirmed:
        should_flag = True
        reason = "closed_without_delivery_confirmation"
    elif visible_completion and not delivery_confirmed:
        should_flag = True
        reason = "visible_completion_without_delivery_confirmation"
    elif evidence_level == "weak" and control_state == "progress_only":
        should_flag = True
        reason = "progress_only_unverified"

    if not should_flag:
        return []

    return [{
        "task_id": task_id,
        "question": str(current.get("question", ""))[:100],
        "control_state": control_state,
        "delivery_state": delivery_state,
        "next_action": next_action,
        "evidence_level": evidence_level,
        "visible_completion": visible_completion,
        "reason": reason,
    }]


def check_repeated_errors():
    """检查重复错误"""
    if not LEARNINGS_FILE.exists():
        return []

    try:
        content = LEARNINGS_FILE.read_text(encoding="utf-8")
        patterns = {}
        for line in content.split("\n"):
            if "Pattern-Key:" in line:
                key = line.split("Pattern-Key:")[1].strip()
                patterns[key] = patterns.get(key, 0) + 1
        return [k for k, v in patterns.items() if v > 1]
    except Exception:
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
            content += f"  next_action: {task['next_action']}\n"
            content += f"  evidence_level: {task['evidence_level']}\n"
            content += f"  reason: {task['reason']}\n"
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
    memory_file.write_text(content, encoding="utf-8")
    return memory_file


def main():
    print(f"=== 每日反思 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    print("\n1. 检查系统健康...")
    health_issues = check_system_health()
    if health_issues:
        print(f"   发现 {len(health_issues)} 个问题")
        for issue in health_issues:
            print(f"   - {issue}")
    else:
        print("   ✅ 系统正常")

    print("\n2. 检查未闭环任务...")
    undelivered = check_undelivered_tasks()
    if undelivered:
        print(f"   发现 {len(undelivered)} 个未闭环任务")
        for task in undelivered:
            print(f"   - {task['task_id']}: {task['question']} ({task['reason']})")
    else:
        print("   ✅ 所有任务已送达")

    print("\n3. 检查重复错误...")
    repeated_errors = check_repeated_errors()
    if repeated_errors:
        print(f"   发现 {len(repeated_errors)} 个重复错误模式")
        for pattern in repeated_errors:
            print(f"   - {pattern}")
    else:
        print("   ✅ 无重复错误")

    print("\n4. 写入每日反思...")
    memory_file = write_daily_reflection(health_issues, undelivered, repeated_errors)
    print(f"   ✅ 已写入 {memory_file}")

    print("\n=== 总结 ===")
    total_issues = len(health_issues) + len(undelivered) + len(repeated_errors)
    if total_issues > 0:
        print(f"发现 {total_issues} 个问题，需要关注")
    else:
        print("系统稳定，无问题")

    return 0


if __name__ == "__main__":
    exit(main())
