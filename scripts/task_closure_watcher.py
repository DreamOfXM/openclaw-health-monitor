#!/usr/bin/env python3
"""
任务闭环看门狗 - 发现"完成但未送达"或"阻塞但未解释送达"的任务
"""

import json
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "task-closure"
FACTS_FILE = Path(__file__).parent.parent / "data" / "current-task-facts.json"


def _load_facts() -> dict:
    if not FACTS_FILE.exists():
        return {}
    try:
        return json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
    delivery_state = _extract_delivery_state(current).lower()
    return delivery_state in {"delivered", "delivery_confirmed", "confirmed"} or bool(core_supervision.get("delivery_confirmed"))


def scan_undelivered_tasks():
    """扫描所有完成但未送达的任务"""
    if not DATA_DIR.exists():
        return []

    undelivered = []
    for task_file in DATA_DIR.glob("*.json"):
        try:
            task = json.loads(task_file.read_text(encoding="utf-8"))
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
    """检查当前任务是否未送达或阻塞未解释"""
    facts = _load_facts()
    current = facts.get("current_task") if isinstance(facts.get("current_task"), dict) else {}
    if not current:
        return None

    control = current.get("control") if isinstance(current.get("control"), dict) else {}
    flags = control.get("flags") if isinstance(control.get("flags"), dict) else {}
    core_supervision = control.get("core_supervision") if isinstance(control.get("core_supervision"), dict) else {}

    control_state = str(current.get("control_state") or "")
    next_action = str(current.get("next_action") or "")
    delivery_state = _extract_delivery_state(current)
    confirmed = _delivery_confirmed(current)
    visible_completion = bool(flags.get("visible_completion"))
    blocked = control_state in {"blocked_unverified", "blocked_control_followup_failed"} or bool(core_supervision.get("is_blocked"))

    if blocked and not confirmed:
        reason = "blocked_not_delivered"
    elif control_state == "completed_verified" and not confirmed:
        reason = "completed_not_delivered"
    elif next_action in {"require_receipt_or_block", "await_delivery_confirmation", "delivery_retry"} and not confirmed:
        reason = "followup_pending_without_delivery"
    elif visible_completion and not confirmed:
        reason = "visible_completion_without_delivery_confirmation"
    else:
        return None

    return {
        "task_id": current.get("task_id"),
        "control_state": control_state,
        "delivery_state": delivery_state,
        "question": current.get("question", "")[:100],
        "next_action": next_action,
        "reason": reason,
    }


def main():
    print("=== 任务闭环看门狗 ===")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    current = check_current_task()
    if current:
        print(f"\n[警告] 当前任务未形成送达闭环:")
        print(f"  task_id: {current['task_id']}")
        print(f"  control_state: {current['control_state']}")
        print(f"  delivery_state: {current['delivery_state']}")
        print(f"  next_action: {current['next_action']}")
        print(f"  reason: {current['reason']}")
        print(f"  question: {current['question']}")

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
