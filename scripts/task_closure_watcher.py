#!/usr/bin/env python3
"""
任务闭环看门狗 - 发现"完成但未送达"或"阻塞但未解释送达"的任务
"""

import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "task-closure"
FACTS_FILE = BASE_DIR / "data" / "current-task-facts.json"
MONITOR_DIR = Path.home() / ".openclaw" / "shared-context" / "monitor-tasks"
WATCHER_LOG = MONITOR_DIR / "watcher.log"
AUDIT_LOG = MONITOR_DIR / "audit.log"


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


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main():
    started_at = int(time.time())
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

    record = {
        "ts": started_at,
        "iso_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_at)),
        "current": current,
        "undelivered_count": len(undelivered),
        "undelivered_preview": undelivered[:5],
        "healthy": not current and not undelivered,
    }
    _append_jsonl(WATCHER_LOG, record)
    if current or undelivered:
        _append_jsonl(
            AUDIT_LOG,
            {
                "ts": started_at,
                "event": "task_closure_watcher_alert",
                "current": current,
                "undelivered_count": len(undelivered),
            },
        )

    # 写一个简单的控制信号文件，供 guardian 下次同步时读取
    control_signal = {
        "ts": started_at,
        "needs_delivery_retry": bool(current and current.get("reason") in {
            "completed_not_delivered",
            "visible_completion_without_delivery_confirmation",
        }),
        "needs_receipt_or_block": bool(current and current.get("reason") == "followup_pending_without_delivery"),
        "task_id": (current or {}).get("task_id"),
        "reason": (current or {}).get("reason"),
    }
    signal_file = MONITOR_DIR / "watcher-control-signal.json"
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(json.dumps(control_signal, ensure_ascii=False), encoding="utf-8")

    return 0


if __name__ == "__main__":
    exit(main())
