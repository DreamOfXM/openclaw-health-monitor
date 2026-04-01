#!/usr/bin/env python3
import json
import sys
from datetime import datetime


def decide(req: dict) -> dict:
    problem = req.get("problem") or {}
    risk = req.get("risk") or {}
    proposed_fix = req.get("proposed_fix") or {}
    pattern = str(problem.get("pattern") or "unknown")
    action = str(proposed_fix.get("action") or "investigate")
    execution_mode = str(risk.get("execution_mode") or "auto_apply")

    decision = {
        "decision": "approve_auto_fix",
        "rationale": ["external_decider_default"],
        "apply_action": action,
        "requires_human": False,
        "recommended_changes": list(proposed_fix.get("code_changes") or []),
        "next_step": "execute_fix",
        "decider": "external_model_decider",
        "decided_at": datetime.now().isoformat(),
    }

    if pattern == "blocked:background_root_task_missing":
        decision.update({
            "decision": "review_binding_logic",
            "apply_action": "repair_background_root_binding",
            "next_step": "trace_root_binding_and_patch",
        })
        decision["rationale"].append("background task 缺 root binding，优先补 projection/root linkage")
        return decision

    if pattern == "suspended:received_only":
        decision.update({
            "decision": "approve_with_review",
            "apply_action": "promote_stalled_received_only",
            "next_step": "execute_fix_and_track",
        })
        decision["rationale"].append("received_only 长时间悬挂，应自动升级为恢复动作并跟踪")
        return decision

    if execution_mode == "model_required":
        decision.update({
            "decision": "investigate_with_human_context",
            "requires_human": True,
            "next_step": "collect_more_evidence",
        })
        decision["rationale"].append("高风险问题需要更多证据")
    elif execution_mode == "model_review":
        decision.update({
            "decision": "approve_with_review",
            "next_step": "execute_fix_and_track",
        })
        decision["rationale"].append("允许执行，但需持续跟踪")
    elif execution_mode == "auto_with_model_summary":
        decision.update({
            "decision": "approve_auto_fix_with_summary",
            "next_step": "execute_fix_and_emit_summary",
        })
        decision["rationale"].append("协议类问题允许自动修复并保留摘要")

    return decision


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({
            "decision": "investigate_with_human_context",
            "rationale": ["empty_request"],
            "apply_action": "investigate",
            "requires_human": True,
            "recommended_changes": [],
            "next_step": "collect_more_evidence",
        }, ensure_ascii=False))
        return 0
    req = json.loads(raw)
    print(json.dumps(decide(req), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
