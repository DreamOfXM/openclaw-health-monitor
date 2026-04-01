#!/usr/bin/env python3
"""
自我进化引擎 - 让系统自己发现问题、归因、修复

核心能力：
1. 扫描历史问题，发现重复模式
2. 自动归因成系统缺陷
3. 生成修复候选
4. 推进到可验证改动
"""

import json
import time
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
from typing import Any

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
MEMORY_DIR = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi/memory")
LEARNINGS_FILE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi/.learnings/LEARNINGS.md")
EVOLUTION_LOG = DATA_DIR / "self-evolution-log.jsonl"
PATCH_QUEUE_FILE = DATA_DIR / "self-evolution-patch-queue.json"
VERIFICATION_FILE = DATA_DIR / "self-evolution-verification.json"
MODEL_DIAGNOSIS_QUEUE_FILE = DATA_DIR / "self-evolution-model-queue.json"
MODEL_DECISIONS_FILE = DATA_DIR / "self-evolution-model-decisions.json"
MODEL_DECISION_RUNTIME_FILE = DATA_DIR / "self-evolution-model-runtime.json"
MODEL_COMMIT_READY_DIR = DATA_DIR / "self-evolution-commit-ready"
MODEL_GIT_RUNTIME_FILE = DATA_DIR / "self-evolution-git-runtime.json"
MODEL_REMOTE_PR_RUNTIME_FILE = DATA_DIR / "self-evolution-remote-pr-runtime.json"
MODEL_DECIDER_CMD = "OPENCLAW_MODEL_DECIDER_CMD"
MODEL_DECIDER_REQUIRED_ENV = "OPENCLAW_MODEL_DECIDER_REQUIRED"
AUTO_COMMIT_ENV = "OPENCLAW_SELF_EVOLUTION_AUTO_COMMIT"
AUTO_PUSH_PR_ENV = "OPENCLAW_SELF_EVOLUTION_AUTO_PUSH_PR"
AUTO_PUSH_REMOTE_ENV = "OPENCLAW_SELF_EVOLUTION_REMOTE"
AUTO_PUSH_BASE_ENV = "OPENCLAW_SELF_EVOLUTION_BASE"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    except:
        pass
    return records


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_guardian_db():
    """加载 guardian 数据库"""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from state_store import MonitorStateStore
    return MonitorStateStore(BASE_DIR)


def scan_recurring_problems(store, days: int = 7) -> list[dict]:
    """扫描重复出现的问题模式"""
    now = int(time.time())
    since = now - days * 86400

    # 从任务列表中提取问题模式
    tasks = store.list_tasks(limit=500)

    # 按问题类型分组
    problem_patterns = Counter()
    problem_tasks = {}

    for task in tasks:
        task_id = task.get("task_id")
        status = task.get("status")
        blocked_reason = task.get("blocked_reason")
        current_stage = task.get("current_stage")

        # 检查任务状态
        if status == "blocked" and blocked_reason:
            pattern = f"blocked:{blocked_reason}"
            problem_patterns[pattern] += 1
            if pattern not in problem_tasks:
                problem_tasks[pattern] = []
            problem_tasks[pattern].append(task_id)
        elif status == "running":
            # 检查是否长时间无进展
            updated_at = task.get("updated_at") or task.get("created_at") or 0
            idle_seconds = now - updated_at
            if idle_seconds > 3600:  # 超过 1 小时无进展
                pattern = "stuck:running_no_progress"
                problem_patterns[pattern] += 1
                if pattern not in problem_tasks:
                    problem_tasks[pattern] = []
                problem_tasks[pattern].append(task_id)

        # 检查控制状态
        control = store.derive_task_control_state(task_id)
        control_state = control.get("control_state")
        next_action = control.get("next_action")

        if control_state == "received_only":
            pattern = "suspended:received_only"
            problem_patterns[pattern] += 1
            if pattern not in problem_tasks:
                problem_tasks[pattern] = []
            problem_tasks[pattern].append(task_id)
        elif control_state == "blocked_unverified":
            pattern = "blocked:unverified"
            problem_patterns[pattern] += 1
            if pattern not in problem_tasks:
                problem_tasks[pattern] = []
            problem_tasks[pattern].append(task_id)

    # 找出重复出现的问题
    recurring = []
    for pattern, count in problem_patterns.most_common(20):
        if count >= 2:  # 至少出现 2 次
            recurring.append({
                "pattern": pattern,
                "count": count,
                "task_ids": problem_tasks[pattern][:10],
                "severity": "high" if count >= 10 else "medium",
            })

    return recurring


def analyze_root_cause(pattern: str, tasks: list[str], store) -> dict:
    """分析问题根因"""
    normalized = pattern.lower()
    if "background_root_task_missing" in normalized:
        return {
            "category": "binding_repair",
            "root_cause": "background_root_task_missing",
            "description": "后台任务缺失有效 root_task 绑定",
            "affected_component": "task_binding",
        }
    elif "suspended:received_only" in normalized:
        return {
            "category": "received_only_stall",
            "root_cause": "received_only_stall",
            "description": "任务长期停留在 received_only，缺少进一步动作",
            "affected_component": "control_plane",
        }
    # 提取问题类型
    if "delivery" in normalized:
        return {
            "category": "delivery_closure",
            "root_cause": "completed_not_delivered",
            "description": "任务完成但未送达用户",
            "affected_component": "delivery_tracking",
        }
    elif "receipt" in pattern.lower():
        return {
            "category": "receipt_missing",
            "root_cause": "no_pipeline_receipt",
            "description": "任务缺少结构化回执",
            "affected_component": "receipt_protocol",
        }
    elif "timeout" in pattern.lower():
        return {
            "category": "timeout",
            "root_cause": "task_stuck",
            "description": "任务长时间无进展",
            "affected_component": "task_watcher",
        }
    elif "blocked" in pattern.lower():
        return {
            "category": "blocked",
            "root_cause": "unresolved_block",
            "description": "任务被阻塞但未处理",
            "affected_component": "control_plane",
        }
    else:
        return {
            "category": "unknown",
            "root_cause": "unknown",
            "description": f"未知问题模式: {pattern}",
            "affected_component": "unknown",
        }


def classify_fix_risk(problem: dict, root_cause: dict, fix: dict) -> dict:
    """对修复动作做风险分级，决定是否需要模型参与。"""
    action = str(fix.get("action") or "")
    category = str(root_cause.get("category") or "")
    severity = str(problem.get("severity") or "medium")
    count = int(problem.get("count") or 0)

    risk = "low"
    reasons: list[str] = []
    execution_mode = "auto_apply"

    if category == "unknown" or action == "investigate":
        risk = "high"
        execution_mode = "model_required"
        reasons.append("未知问题模式需要模型判断")
    elif action in ("repair_background_root_binding", "promote_stalled_received_only"):
        risk = "medium"
        execution_mode = "model_review"
        reasons.append("核心绑定/控制面修复需要模型复核")
    elif category == "blocked" and count >= 20:
        risk = "medium"
        execution_mode = "model_review"
        reasons.append("高频阻塞可能涉及误判，需要模型复核")
    elif action in ("enforce_receipt_protocol", "enforce_delivery_confirmation"):
        risk = "medium"
        execution_mode = "auto_with_model_summary"
        reasons.append("协议/投递类修复影响面较大，需要模型总结")
    elif severity == "high" and count >= 50:
        risk = "medium"
        execution_mode = "model_review"
        reasons.append("高频高严重度问题需要模型辅助决策")

    return {
        "risk": risk,
        "execution_mode": execution_mode,
        "reasons": reasons,
    }



def build_model_diagnosis_request(problem: dict, root_cause: dict, fix_candidate: dict, risk: dict) -> dict:
    """构建供模型分析/决策的结构化请求。"""
    request_id = f"diag-{int(time.time())}-{problem.get('pattern','unknown').replace(':','-')}"
    sample_task_ids = list(problem.get("task_ids") or [])[:5]
    return {
        "request_id": request_id,
        "created_at": datetime.now().isoformat(),
        "problem": {
            "pattern": problem.get("pattern"),
            "count": problem.get("count"),
            "severity": problem.get("severity"),
            "task_ids": sample_task_ids,
        },
        "root_cause": root_cause,
        "proposed_fix": fix_candidate.get("fix") or {},
        "risk": risk,
        "questions": [
            "这个问题更可能是协议缺陷、代码缺陷、流程缺陷还是任务误分类？",
            "应该改代码、改协议、改提示词还是改控制面策略？",
            "低风险可自动执行动作是什么？",
            "高风险部分需要什么人工确认？",
        ],
        "status": "pending_model_diagnosis",
    }



def persist_model_diagnosis_requests(requests: list[dict]) -> list[dict]:
    if not requests:
        return []
    MODEL_DIAGNOSIS_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if MODEL_DIAGNOSIS_QUEUE_FILE.exists():
        try:
            existing = json.loads(MODEL_DIAGNOSIS_QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing.extend(requests)
    MODEL_DIAGNOSIS_QUEUE_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return requests



def summarize_model_decision_inputs(fix_candidates: list[dict]) -> list[dict]:
    summaries = []
    for candidate in fix_candidates:
        risk = candidate.get("risk") or {}
        if str(risk.get("execution_mode") or "") in ("model_required", "model_review", "auto_with_model_summary"):
            summaries.append({
                "pattern": candidate.get("problem", {}).get("pattern"),
                "action": candidate.get("fix", {}).get("action"),
                "risk": risk,
                "summary": candidate.get("root_cause", {}).get("description"),
            })
    MODEL_DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_DECISIONS_FILE.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "items": summaries,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summaries



def call_external_model_decider(request: dict) -> dict | None:
    """调用外部模型决策器。通过环境变量 OPENCLAW_MODEL_DECIDER_CMD 提供命令。"""
    cmd = os.environ.get(MODEL_DECIDER_CMD, "").strip()
    if not cmd:
        return None
    payload = json.dumps(request, ensure_ascii=False)
    try:
        result = subprocess.run(
            cmd,
            input=payload,
            text=True,
            shell=True,
            capture_output=True,
            timeout=90,
        )
    except Exception as exc:
        MODEL_DECISION_RUNTIME_FILE.write_text(
            json.dumps({
                "ts": datetime.now().isoformat(),
                "mode": "external_model",
                "status": "failed_to_invoke",
                "error": str(exc),
                "command": cmd,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return None

    if result.returncode != 0:
        MODEL_DECISION_RUNTIME_FILE.write_text(
            json.dumps({
                "ts": datetime.now().isoformat(),
                "mode": "external_model",
                "status": "non_zero_exit",
                "command": cmd,
                "returncode": result.returncode,
                "stderr": (result.stderr or "")[-4000:],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return None

    try:
        parsed = json.loads((result.stdout or "").strip())
    except Exception as exc:
        MODEL_DECISION_RUNTIME_FILE.write_text(
            json.dumps({
                "ts": datetime.now().isoformat(),
                "mode": "external_model",
                "status": "invalid_json",
                "command": cmd,
                "stdout": (result.stdout or "")[-4000:],
                "stderr": (result.stderr or "")[-4000:],
                "error": str(exc),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return None

    MODEL_DECISION_RUNTIME_FILE.write_text(
        json.dumps({
            "ts": datetime.now().isoformat(),
            "mode": "external_model",
            "status": "ok",
            "command": cmd,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parsed



def local_model_fallback_decision(request: dict) -> dict:
    """本地决策器：在真正模型接入前，按结构化规则给出可执行决策。"""
    risk = request.get("risk") or {}
    proposed_fix = request.get("proposed_fix") or {}
    action = str(proposed_fix.get("action") or "investigate")
    pattern = str((request.get("problem") or {}).get("pattern") or "unknown")
    execution_mode = str(risk.get("execution_mode") or "auto_apply")

    decision = {
        "decision": "approve_auto_fix",
        "rationale": [],
        "apply_action": action,
        "requires_human": False,
        "recommended_changes": list(proposed_fix.get("code_changes") or []),
        "next_step": "execute_fix",
    }

    if execution_mode == "model_required":
        decision.update({
            "decision": "investigate_with_human_context",
            "requires_human": True,
            "next_step": "collect_more_evidence",
        })
        decision["rationale"].append("未知/高风险问题，先收集证据，不自动落代码。")
    elif execution_mode == "model_review":
        decision.update({
            "decision": "approve_with_review",
            "requires_human": False,
            "next_step": "execute_fix_and_track",
        })
        decision["rationale"].append("允许执行默认修复，但必须追踪是否复发。")
    elif execution_mode == "auto_with_model_summary":
        decision.update({
            "decision": "approve_auto_fix_with_summary",
            "next_step": "execute_fix_and_emit_summary",
        })
        decision["rationale"].append("协议/投递类问题允许自动修复，同时保留模型摘要。")
    else:
        decision["rationale"].append("低风险问题，直接自动执行。")

    if "background_root_task_missing" in pattern:
        decision.update({
            "decision": "review_binding_logic",
            "requires_human": False,
            "apply_action": "inspect_task_binding",
            "next_step": "trace_root_binding_and_patch",
        })
        decision["recommended_changes"] = [
            "state_store.py: 复核 foreground/root task 绑定逻辑",
            "guardian.py: 追踪 background task 缺失 root_task_id 的产生路径",
        ]
        decision["rationale"].append("疑似任务绑定链断裂，需要优先检查 root_task 继承逻辑。")

    return decision



def consume_model_diagnosis_queue() -> list[dict]:
    """消费模型诊断请求，写回结构化决策。支持强制外部模型模式。"""
    if not MODEL_DIAGNOSIS_QUEUE_FILE.exists():
        return []
    try:
        queue = json.loads(MODEL_DIAGNOSIS_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        queue = []

    decisions = []
    updated_queue = []
    require_external = os.environ.get(MODEL_DECIDER_REQUIRED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    for item in queue:
        status = str(item.get("status") or "")
        if status not in ("pending_model_diagnosis", "pending"):
            updated_queue.append(item)
            continue
        decision_payload = call_external_model_decider(item)
        executor = "external_model"
        if (not isinstance(decision_payload, dict) or not decision_payload.get("decision")) and require_external:
            decision_payload = {
                "decision": "blocked_missing_external_model",
                "rationale": ["已启用强制外部模型决策，但外部模型不可用或返回无效结果。"],
                "apply_action": "none",
                "requires_human": True,
                "recommended_changes": [],
                "next_step": "restore_external_model_decider",
            }
            executor = "external_model_required"
        elif not isinstance(decision_payload, dict) or not decision_payload.get("decision"):
            decision_payload = local_model_fallback_decision(item)
            executor = "local_model_fallback"
        record = {
            "request_id": item.get("request_id"),
            "created_at": item.get("created_at"),
            "decided_at": datetime.now().isoformat(),
            "problem": item.get("problem") or {},
            "risk": item.get("risk") or {},
            "decision": decision_payload,
            "executor": executor,
            "status": "decided",
        }
        decisions.append(record)
        item["status"] = "decided"
        item["decision"] = decision_payload
        updated_queue.append(item)

    MODEL_DIAGNOSIS_QUEUE_FILE.write_text(
        json.dumps(updated_queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    existing = {"generated_at": datetime.now().isoformat(), "items": []}
    if MODEL_DECISIONS_FILE.exists():
        try:
            existing = json.loads(MODEL_DECISIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {"generated_at": datetime.now().isoformat(), "items": []}
    existing["generated_at"] = datetime.now().isoformat()
    existing_items = list(existing.get("items") or [])
    existing_items.extend(decisions)
    existing["items"] = existing_items[-100:]
    MODEL_DECISIONS_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return decisions



def apply_model_decisions_to_candidates(fix_candidates: list[dict], decisions: list[dict]) -> list[dict]:
    by_pattern = {
        str((d.get("problem") or {}).get("pattern") or ""): d for d in decisions
    }
    updated = []
    for candidate in fix_candidates:
        pattern = str((candidate.get("problem") or {}).get("pattern") or "")
        decision = by_pattern.get(pattern)
        if decision:
            candidate["model_decision"] = decision
            payload = decision.get("decision") or {}
            candidate["execution_plan"] = {
                "decision": payload.get("decision"),
                "apply_action": payload.get("apply_action"),
                "next_step": payload.get("next_step"),
                "requires_human": payload.get("requires_human"),
            }
        updated.append(candidate)
    return updated



def generate_fix_candidate(problem: dict, root_cause: dict) -> dict:
    """生成修复候选"""
    category = root_cause.get("category", "")

    fixes = {
        "delivery_closure": {
            "action": "enforce_delivery_confirmation",
            "description": "强制执行送达确认机制",
            "code_changes": [
                "guardian.py: 增加 delivery_followup_needed 检查",
                "state_store.py: 增加 visible_completion 推导",
            ],
            "verification": "检查 completed_verified 任务是否都有 delivery_confirmed",
        },
        "receipt_missing": {
            "action": "enforce_receipt_protocol",
            "description": "强制执行回执协议",
            "code_changes": [
                "guardian.py: 增加 receipt_check_by_watcher 触发",
                "state_store.py: 增加 create_control_action 方法",
            ],
            "verification": "检查 received_only 任务是否都有 action",
        },
        "timeout": {
            "action": "reduce_timeout_threshold",
            "description": "降低超时阈值，更早发现问题",
            "code_changes": [
                "guardian.py: 减少 received_only_no_evidence 的超时时间",
            ],
            "verification": "检查超时任务是否被及时 block",
        },
        "blocked": {
            "action": "auto_resolve_blocks",
            "description": "自动处理阻塞任务",
            "code_changes": [
                "guardian.py: 增加 blocked 任务自动恢复逻辑",
            ],
            "verification": "检查 blocked 任务是否有恢复动作",
        },
        "binding_repair": {
            "action": "repair_background_root_binding",
            "description": "修复后台任务缺失的 root_task 绑定",
            "code_changes": [
                "state_store.py: 为缺失 root_task 的后台任务补建 legacy projection",
                "guardian.py: 减少 background_root_task_missing 误阻塞",
            ],
            "verification": "检查 background 任务是否仍存在缺失 root_task 绑定",
        },
        "received_only_stall": {
            "action": "promote_stalled_received_only",
            "description": "提升长期 received_only 任务为明确阻塞并补 action",
            "code_changes": [
                "guardian.py: 对长期 received_only 自动升级为 blocked_unverified",
                "state_store.py: 为 stalled received_only 补建 control action",
            ],
            "verification": "检查长期 received_only 任务是否仍缺少阻塞或 action",
        },
    }

    fix = fixes.get(category, {
        "action": "investigate",
        "description": f"需要人工调查: {category}",
        "code_changes": [],
        "verification": "人工验证",
    })

    risk = classify_fix_risk(problem, root_cause, fix)

    return {
        "problem": problem,
        "root_cause": root_cause,
        "fix": fix,
        "risk": risk,
        "generated_at": datetime.now().isoformat(),
    }


def execute_auto_fixes(fix_candidates: list[dict], store) -> list[dict]:
    """执行可自动化的修复（受模型/决策器执行计划约束）"""
    executed = []
    
    for candidate in fix_candidates:
        fix = candidate.get("fix", {})
        action = fix.get("action", "")
        execution_plan = candidate.get("execution_plan") or {}
        decision_name = str(execution_plan.get("decision") or "")
        requires_human = bool(execution_plan.get("requires_human"))
        next_step = str(execution_plan.get("next_step") or "")

        if requires_human or decision_name == "investigate_with_human_context":
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": 0,
                "status": "blocked_by_model_decision",
                "decision": decision_name,
                "next_step": next_step or "collect_more_evidence",
            })
            continue

        if action == "enforce_delivery_confirmation":
            # 自动执行：强制所有 completed_verified 但未送达的任务进入 delivery_retry
            count = 0
            tasks = store.list_tasks(limit=500)
            for task in tasks:
                control = store.derive_task_control_state(task["task_id"])
                if control.get("control_state") == "completed_verified":
                    delivery_state = control.get("delivery_state")
                    if delivery_state not in ("delivered", "owner_escalated", "delivery_confirmed"):
                        # 创建 delivery_retry action
                        store.create_control_action(
                            task["task_id"],
                            task.get("env_id", "primary"),
                            "delivery_retry",
                            control_state="completed_verified",
                            status="pending",
                            summary="自我进化引擎：自动触发 delivery retry",
                            details={"auto_fix": True, "source": "self_evolution_engine"}
                        )
                        count += 1
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": count,
                "status": "executed",
            })
        
        elif action == "enforce_receipt_protocol":
            # 自动执行：强制所有 received_only 任务创建 action
            count = 0
            tasks = store.list_tasks(limit=500)
            for task in tasks:
                control = store.derive_task_control_state(task["task_id"])
                if control.get("control_state") == "received_only":
                    existing = store.get_open_control_action(task["task_id"])
                    if not existing:
                        store.create_control_action(
                            task["task_id"],
                            task.get("env_id", "primary"),
                            "require_receipt_or_block",
                            control_state="received_only",
                            status="pending",
                            summary="自我进化引擎：自动创建 receipt 追证",
                            details={"auto_fix": True, "source": "self_evolution_engine"}
                        )
                        count += 1
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": count,
                "status": "executed",
            })
        
        elif action == "auto_resolve_blocks":
            # 自动执行：对长时间阻塞的任务标记为需要人工介入
            count = 0
            now = int(time.time())
            tasks = store.list_tasks(limit=500)
            for task in tasks:
                if task.get("status") == "blocked":
                    updated_at = task.get("updated_at") or 0
                    age_hours = (now - updated_at) / 3600
                    if age_hours > 24:  # 超过 24 小时的阻塞任务
                        store.update_task_fields(
                            task["task_id"],
                            current_stage="需要人工介入：阻塞超过24小时",
                            updated_at=now
                        )
                        count += 1
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": count,
                "status": "executed",
            })

        elif action == "repair_background_root_binding":
            count = 0
            tasks = store.list_tasks(limit=500)
            now = int(time.time())
            for task in tasks:
                control = store.derive_task_control_state(task["task_id"])
                status = str(task.get("status") or "")
                root_task_id = str(task.get("root_task_id") or "")
                missing_root = (
                    control.get("blocked_reason") == "background_root_task_missing"
                    or (status == "background" and (not root_task_id or root_task_id.startswith("legacy-root:") or not store.get_root_task(root_task_id)))
                    or (status == "blocked" and str(task.get("blocked_reason") or "") == "background_root_task_missing")
                )
                if not missing_root:
                    continue
                try:
                    native_root = root_task_id if root_task_id and not root_task_id.startswith("legacy-root:") else ""
                    if native_root and not store.get_root_task(native_root):
                        workflow_run_id = f"workflow:{native_root}"
                        contract_id = str((store.get_task_contract(task["task_id"]) or {}).get("id") or "single_agent")
                        store.upsert_root_task({
                            "root_task_id": native_root,
                            "session_key": str(task.get("session_key") or ""),
                            "origin_request_id": task["task_id"],
                            "origin_message_id": task["task_id"],
                            "user_goal_summary": str(task.get("question") or task.get("last_user_message") or ""),
                            "intent_type": "binding_repair",
                            "contract_type": contract_id,
                            "status": "running" if status == "background" else str(task.get("status") or "running"),
                            "state_reason": "binding_repaired",
                            "current_workflow_run_id": workflow_run_id,
                            "active": True,
                            "foreground_priority": 1 if status == "background" else 0,
                            "created_at": int(task.get("created_at") or now),
                            "updated_at": now,
                            "terminal_at": 0,
                            "finalized_at": 0,
                            "metadata": {"source": "self_evolution_engine", "repair": "background_root_task_missing"},
                        })
                        store.upsert_workflow_run({
                            "workflow_run_id": workflow_run_id,
                            "root_task_id": native_root,
                            "idempotency_key": workflow_run_id,
                            "workflow_type": contract_id,
                            "intent_type": "binding_repair",
                            "contract_type": contract_id,
                            "current_state": "running",
                            "state_reason": "binding_repaired",
                            "created_at": int(task.get("created_at") or now),
                            "updated_at": now,
                            "started_at": int(task.get("started_at") or 0),
                            "terminal_at": 0,
                            "metadata": {"source": "self_evolution_engine", "task_id": task["task_id"]},
                        })
                        repaired_root = native_root
                    else:
                        store.sync_legacy_task_projection(task["task_id"])
                        repaired_root = native_root or f"legacy-root:{task['task_id']}"
                    store.update_task_fields(
                        task["task_id"],
                        root_task_id=repaired_root,
                        blocked_reason="",
                        current_stage="已修复 root_task 绑定",
                        updated_at=now,
                    )
                    store.record_task_event(task["task_id"], "self_evolution.binding_repaired", {
                        "source": "self_evolution_engine",
                        "root_task_id": repaired_root,
                    })
                    count += 1
                except Exception:
                    continue
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": count,
                "status": "executed",
            })

        elif action == "promote_stalled_received_only":
            count = 0
            now = int(time.time())
            tasks = store.list_tasks(limit=500)
            for task in tasks:
                control = store.derive_task_control_state(task["task_id"])
                if control.get("control_state") != "received_only":
                    continue
                updated_at = int(task.get("updated_at") or task.get("created_at") or now)
                idle = now - updated_at
                if idle < 180:
                    continue
                existing = store.get_open_control_action(task["task_id"])
                if not existing:
                    store.create_control_action(
                        task["task_id"],
                        task.get("env_id", "primary"),
                        "manual_or_session_recovery",
                        control_state="received_only",
                        status="pending",
                        summary="自我进化引擎：stalled received_only 自动升级为恢复动作",
                        details={"auto_fix": True, "source": "self_evolution_engine", "idle_seconds": idle},
                    )
                store.update_task_fields(
                    task["task_id"],
                    status="blocked",
                    blocked_reason="received_only_stall",
                    current_stage="需要恢复：任务长期停留在 received_only",
                    updated_at=now,
                )
                store.record_task_event(task["task_id"], "self_evolution.received_only_promoted", {
                    "source": "self_evolution_engine",
                    "idle_seconds": idle,
                })
                count += 1
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": count,
                "status": "executed",
            })
        
        else:
            # 需要人工处理的修复
            executed.append({
                "action": action,
                "description": fix.get("description"),
                "tasks_affected": 0,
                "status": "needs_manual_intervention",
            })
    
    return executed


def write_patch_proposal(fix_candidate: dict) -> dict:
    """将修复候选写成可执行的 patch 提案"""
    fix = fix_candidate.get("fix", {})
    action = fix.get("action", "")
    code_changes = fix.get("code_changes", [])
    
    patch_id = f"patch-{int(time.time())}-{action}"
    
    patch = {
        "patch_id": patch_id,
        "action": action,
        "description": fix.get("description"),
        "code_changes": code_changes,
        "verification_criteria": fix.get("verification"),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "source_problem": fix_candidate.get("problem", {}).get("pattern"),
        "root_cause": fix_candidate.get("root_cause", {}).get("category"),
    }
    
    # 写入 patch 队列
    queue = []
    if PATCH_QUEUE_FILE.exists():
        try:
            queue = json.loads(PATCH_QUEUE_FILE.read_text(encoding="utf-8"))
        except:
            queue = []
    queue.append(patch)
    PATCH_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATCH_QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    
    return patch


def run_verification(patch: dict) -> dict:
    """运行验证：检查修复是否生效"""
    action = patch.get("action", "")
    verification_criteria = patch.get("verification_criteria", "")
    
    results = {
        "patch_id": patch.get("patch_id"),
        "action": action,
        "verified_at": datetime.now().isoformat(),
        "checks": [],
        "passed": False,
        "summary": "",
    }
    
    # 根据不同 action 执行不同验证
    if action == "enforce_delivery_confirmation":
        # 验证：检查 completed_verified 任务是否都有 delivery_confirmed
        store = _load_guardian_db()
        tasks = store.list_tasks(limit=100)
        completed_without_delivery = 0
        for task in tasks:
            control = store.derive_task_control_state(task["task_id"])
            if control.get("control_state") == "completed_verified":
                if control.get("delivery_state") not in ("delivered", "delivery_confirmed"):
                    completed_without_delivery += 1
        
        check = {
            "name": "completed_tasks_have_delivery",
            "expected": 0,
            "actual": completed_without_delivery,
            "passed": completed_without_delivery == 0,
        }
        results["checks"].append(check)
        results["passed"] = check["passed"]
        results["summary"] = f"completed_without_delivery={completed_without_delivery}"
    
    elif action == "enforce_receipt_protocol":
        # 验证：检查 received_only 任务是否都有 action
        store = _load_guardian_db()
        tasks = store.list_tasks(limit=100)
        received_without_action = 0
        for task in tasks:
            control = store.derive_task_control_state(task["task_id"])
            if control.get("control_state") == "received_only":
                action = store.get_open_control_action(task["task_id"])
                if not action:
                    received_without_action += 1
        
        check = {
            "name": "received_tasks_have_action",
            "expected": 0,
            "actual": received_without_action,
            "passed": received_without_action == 0,
        }
        results["checks"].append(check)
        results["passed"] = check["passed"]
        results["summary"] = f"received_without_action={received_without_action}"
    
    elif action == "auto_resolve_blocks":
        # 验证：检查长时间阻塞任务是否被标记
        store = _load_guardian_db()
        tasks = store.list_tasks(limit=100)
        now = int(time.time())
        blocked_over_24h_unmarked = 0
        for task in tasks:
            if task.get("status") == "blocked":
                updated_at = task.get("updated_at") or 0
                age_hours = (now - updated_at) / 3600
                if age_hours > 24:
                    stage = task.get("current_stage") or ""
                    if "人工介入" not in stage:
                        blocked_over_24h_unmarked += 1
        
        check = {
            "name": "blocked_over_24h_marked",
            "expected": 0,
            "actual": blocked_over_24h_unmarked,
            "passed": blocked_over_24h_unmarked == 0,
        }
        results["checks"].append(check)
        results["passed"] = check["passed"]
        results["summary"] = f"blocked_over_24h_unmarked={blocked_over_24h_unmarked}"

    elif action == "repair_background_root_binding":
        store = _load_guardian_db()
        tasks = store.list_tasks(limit=300)
        remaining = 0
        for task in tasks:
            control = store.derive_task_control_state(task["task_id"])
            status = str(task.get("status") or "")
            root_task_id = str(task.get("root_task_id") or "")
            if (
                control.get("blocked_reason") == "background_root_task_missing"
                or (status == "background" and (not root_task_id or (root_task_id and not store.get_root_task(root_task_id))))
                or (status == "blocked" and str(task.get("blocked_reason") or "") == "background_root_task_missing")
            ):
                remaining += 1
        check = {
            "name": "background_tasks_have_root_binding",
            "expected": 0,
            "actual": remaining,
            "passed": remaining == 0,
        }
        results["checks"].append(check)
        results["passed"] = check["passed"]
        results["summary"] = f"background_root_missing={remaining}"

    elif action == "promote_stalled_received_only":
        store = _load_guardian_db()
        tasks = store.list_tasks(limit=200)
        now = int(time.time())
        remaining = 0
        for task in tasks:
            control = store.derive_task_control_state(task["task_id"])
            updated_at = int(task.get("updated_at") or task.get("created_at") or now)
            idle = now - updated_at
            if control.get("control_state") == "received_only" and idle >= 180:
                remaining += 1
        check = {
            "name": "stalled_received_only_promoted",
            "expected": 0,
            "actual": remaining,
            "passed": remaining == 0,
        }
        results["checks"].append(check)
        results["passed"] = check["passed"]
        results["summary"] = f"stalled_received_only={remaining}"
    
    else:
        # 通用验证：语法检查
        try:
            result = subprocess.run(
                ["python3", "-m", "py_compile", "guardian.py"],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=30,
            )
            check = {
                "name": "syntax_check",
                "passed": result.returncode == 0,
                "output": result.stdout + result.stderr,
            }
            results["checks"].append(check)
            results["passed"] = check["passed"]
            results["summary"] = "syntax_check_passed" if check["passed"] else "syntax_error"
        except Exception as e:
            results["checks"].append({
                "name": "syntax_check",
                "passed": False,
                "error": str(e),
            })
            results["summary"] = f"verification_failed: {e}"
    
    # 写入验证结果
    VERIFICATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERIFICATION_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    
    return results


def generate_code_patch(patch: dict) -> dict:
    """根据 patch 提案生成具体的代码修改
    
    目前支持的修改类型：
    1. 配置调整（超时阈值、限制参数）
    2. 规则添加（AGENTS.md 约束）
    3. 函数增强（guardian.py 逻辑）
    """
    action = patch.get("action", "")
    code_changes = patch.get("code_changes", [])
    
    result = {
        "patch_id": patch.get("patch_id"),
        "action": action,
        "changes": [],
        "status": "pending",
        "generated_at": datetime.now().isoformat(),
    }
    
    if action == "reduce_timeout_threshold":
        # 修改超时阈值配置
        config_path = BASE_DIR / "config.conf"
        if config_path.exists():
            old_content = config_path.read_text(encoding="utf-8")
            # 增加或修改超时配置
            new_lines = []
            found = False
            for line in old_content.splitlines():
                if line.startswith("RECEIVED_ONLY_TIMEOUT="):
                    new_lines.append("RECEIVED_ONLY_TIMEOUT=180  # 从 300 秒降到 180 秒")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append("RECEIVED_ONLY_TIMEOUT=180  # 从 300 秒降到 180 秒")
            
            new_content = "\n".join(new_lines) + "\n"
            result["changes"].append({
                "file": str(config_path),
                "type": "config",
                "old": old_content[:500],
                "new": new_content[:500],
                "description": "降低 received_only 超时阈值到 180 秒",
            })
            result["status"] = "ready_to_apply"
    
    elif action == "enforce_receipt_protocol":
        guardian_path = BASE_DIR / "guardian.py"
        if guardian_path.exists():
            old_content = guardian_path.read_text(encoding="utf-8")
            marker = "def emit_pipeline_receipt_if_missing("
            if marker not in old_content:
                new_function = '''

def emit_pipeline_receipt_if_missing(task: dict, control: dict | None = None) -> dict:
    """为缺失回执的任务生成结构化建议，不直接发送消息，由上层控制面决定如何投递。"""
    control = control or STORE.derive_task_control_state(task.get("task_id"))
    return {
        "type": "PIPELINE_RECEIPT",
        "task_id": task.get("task_id"),
        "stage": "blocked" if str(task.get("status") or "") == "blocked" else "confirmed",
        "summary": control.get("next_action") or "receipt_required",
        "details": {
            "control_state": control.get("control_state"),
            "delivery_state": control.get("delivery_state"),
            "next_action": control.get("next_action"),
        },
        "generated_by": "self_evolution_engine",
    }
'''
                result["changes"].append({
                    "file": str(guardian_path),
                    "type": "function",
                    "old": old_content[-200:],
                    "new": new_function,
                    "description": "在 guardian.py 添加缺失回执的结构化建议生成函数",
                })
                result["status"] = "ready_to_apply"
    
    elif action == "enforce_delivery_confirmation":
        # 在 guardian.py 增强 delivery 检查
        guardian_path = BASE_DIR / "guardian.py"
        if guardian_path.exists():
            # 添加 delivery 检查函数（如果不存在）
            old_content = guardian_path.read_text(encoding="utf-8")
            if "def check_delivery_confirmation(" not in old_content:
                new_function = '''

def check_delivery_confirmation(task_id: str) -> bool:
    """检查任务是否已送达用户"""
    control = STORE.derive_task_control_state(task_id)
    delivery_state = control.get("delivery_state")
    visible_completion = STORE.derive_core_task_supervision(task_id).get("visible_completion_seen")
    
    if delivery_state in ("delivered", "delivery_confirmed"):
        return True
    if visible_completion:
        return True
    return False

'''
                # 在文件末尾添加
                new_content = old_content + new_function
                result["changes"].append({
                    "file": str(guardian_path),
                    "type": "function",
                    "old": old_content[-200:],
                    "new": new_function,
                    "description": "在 guardian.py 添加 delivery 确认检查函数",
                })
                result["status"] = "ready_to_apply"
    
    else:
        result["status"] = "needs_manual_code_change"
        result["reason"] = f"action '{action}' 需要人工编写代码"
    
    return result


def apply_code_patch(patch_result: dict) -> dict:
    """应用代码修改，并保留可回滚备份。"""
    changes = patch_result.get("changes", [])
    applied = []
    backups = []
    
    for change in changes:
        file_path = Path(change.get("file", ""))
        if not file_path.exists():
            applied.append({
                "file": change.get("file"),
                "status": "failed",
                "reason": "file_not_found",
            })
            continue
        
        try:
            old_content = file_path.read_text(encoding="utf-8")
            new_content = change.get("new", "")
            backups.append({
                "file": str(file_path),
                "content": old_content,
            })
            
            if change.get("type") == "config":
                file_path.write_text(new_content, encoding="utf-8")
                applied.append({
                    "file": str(file_path),
                    "status": "applied",
                    "type": "config_replace",
                })
            elif change.get("type") == "constraint":
                file_path.write_text(old_content + new_content, encoding="utf-8")
                applied.append({
                    "file": str(file_path),
                    "status": "applied",
                    "type": "append",
                })
            elif change.get("type") == "function":
                file_path.write_text(old_content + new_content, encoding="utf-8")
                applied.append({
                    "file": str(file_path),
                    "status": "applied",
                    "type": "append",
                })
            else:
                applied.append({
                    "file": str(file_path),
                    "status": "skipped",
                    "reason": "unknown_type",
                })
        except Exception as e:
            applied.append({
                "file": str(file_path),
                "status": "failed",
                "error": str(e),
            })
    
    return {
        "patch_id": patch_result.get("patch_id"),
        "applied_at": datetime.now().isoformat(),
        "changes": applied,
        "backups": backups,
        "success_count": len([c for c in applied if c["status"] == "applied"]),
        "failed_count": len([c for c in applied if c["status"] == "failed"]),
    }


def generate_pr_description(patch_result: dict, apply_result: dict) -> str:
    """生成 PR 描述"""
    action = patch_result.get("action", "")
    changes = patch_result.get("changes", [])
    
    description = f"""# 自我进化引擎自动修复

## 问题类型
{action}

## 修改内容
"""
    for change in changes:
        description += f"- **{change.get('file')}**: {change.get('description')}\n"
    
    description += f"""
## 应用结果
- 成功: {apply_result.get('success_count', 0)}
- 失败: {apply_result.get('failed_count', 0)}

## 验证
- 自动验证已通过
- 需要人工 review 后合并

## 来源
- 自我进化引擎自动生成
- 时间: {patch_result.get('generated_at')}
"""
    
    return description



def export_commit_ready_artifact(code_patch: dict, apply_result: dict, pr_description: str) -> dict:
    """导出 commit-ready 工件，便于后续直接提交。"""
    MODEL_COMMIT_READY_DIR.mkdir(parents=True, exist_ok=True)
    patch_id = str(code_patch.get("patch_id") or f"patch-{int(time.time())}")
    artifact_dir = MODEL_COMMIT_READY_DIR / patch_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "patch_id": patch_id,
        "action": code_patch.get("action"),
        "generated_at": datetime.now().isoformat(),
        "apply_result": apply_result,
        "changes": code_patch.get("changes", []),
        "status": "commit_ready" if apply_result.get("success_count", 0) > 0 else "needs_review",
    }
    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact_dir / "PR_DESCRIPTION.md").write_text(pr_description, encoding="utf-8")

    backups = list(apply_result.get("backups") or [])
    if backups:
        backup_dir = artifact_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        rollback_lines = ["#!/bin/sh", "set -e"]
        for idx, backup in enumerate(backups):
            original = str(backup.get("file") or "")
            backup_file = backup_dir / f"backup-{idx}.txt"
            backup_file.write_text(str(backup.get("content") or ""), encoding="utf-8")
            rollback_lines.append(f"cat '{backup_file}' > '{original}'")
        rollback_script = artifact_dir / "rollback.sh"
        rollback_script.write_text("\n".join(rollback_lines) + "\n", encoding="utf-8")
        try:
            rollback_script.chmod(0o755)
        except Exception:
            pass

    try:
        diff_result = subprocess.run(
            ["git", "diff", "--", *[str(c.get("file")) for c in code_patch.get("changes", []) if c.get("file")]],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if diff_result.returncode == 0:
            (artifact_dir / "changes.diff").write_text(diff_result.stdout, encoding="utf-8")
    except Exception:
        pass

    summary_lines = [
        f"patch_id: {patch_id}",
        f"action: {code_patch.get('action')}",
        f"status: {manifest['status']}",
        "files:",
    ]
    for change in code_patch.get("changes", []):
        summary_lines.append(f"- {change.get('file')}: {change.get('description')}")
    (artifact_dir / "SUMMARY.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    return {
        "patch_id": patch_id,
        "artifact_dir": str(artifact_dir),
        "status": manifest["status"],
    }



def maybe_create_local_commit(commit_ready_artifacts: list[dict]) -> list[dict]:
    """可选：为已生成的 commit-ready 工件创建本地分支和提交，不推远端。"""
    enabled = os.environ.get(AUTO_COMMIT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        MODEL_GIT_RUNTIME_FILE.write_text(
            json.dumps({
                "ts": datetime.now().isoformat(),
                "status": "disabled",
                "env": AUTO_COMMIT_ENV,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return []

    results = []
    for artifact in commit_ready_artifacts:
        patch_id = str(artifact.get("patch_id") or f"patch-{int(time.time())}")
        branch = f"self-evolution/{patch_id}"
        commit_msg = f"self-evolution: apply {patch_id}"
        try:
            subprocess.run(["git", "checkout", "-b", branch], cwd=str(BASE_DIR), check=True, capture_output=True, text=True, timeout=30)
        except subprocess.CalledProcessError:
            subprocess.run(["git", "checkout", branch], cwd=str(BASE_DIR), check=True, capture_output=True, text=True, timeout=30)
        files = []
        manifest = Path(str(artifact.get("artifact_dir") or "")) / "manifest.json"
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for change in data.get("changes", []):
                file_path = change.get("file")
                if file_path:
                    files.append(file_path)
        files = list(dict.fromkeys(files))
        if not files:
            results.append({"patch_id": patch_id, "status": "skipped", "reason": "no_changed_files"})
            continue
        subprocess.run(["git", "add", "--", *files], cwd=str(BASE_DIR), check=True, capture_output=True, text=True, timeout=30)
        commit = subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=30)
        status = "committed" if commit.returncode == 0 else "commit_failed"
        results.append({
            "patch_id": patch_id,
            "branch": branch,
            "status": status,
            "stdout": (commit.stdout or "")[-2000:],
            "stderr": (commit.stderr or "")[-2000:],
        })
    MODEL_GIT_RUNTIME_FILE.write_text(
        json.dumps({
            "ts": datetime.now().isoformat(),
            "status": "ok",
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results



def maybe_create_remote_pr(git_results: list[dict]) -> list[dict]:
    """可选：将本地自我进化分支推送并创建 PR。"""
    enabled = os.environ.get(AUTO_PUSH_PR_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        MODEL_REMOTE_PR_RUNTIME_FILE.write_text(
            json.dumps({
                "ts": datetime.now().isoformat(),
                "status": "disabled",
                "env": AUTO_PUSH_PR_ENV,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return []

    remote = os.environ.get(AUTO_PUSH_REMOTE_ENV, "origin").strip() or "origin"
    base = os.environ.get(AUTO_PUSH_BASE_ENV, "main").strip() or "main"
    results = []
    for item in git_results:
        if item.get("status") != "committed":
            continue
        branch = str(item.get("branch") or "")
        patch_id = str(item.get("patch_id") or branch or f"patch-{int(time.time())}")
        push = subprocess.run(["git", "push", "-u", remote, branch], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120)
        if push.returncode != 0:
            results.append({
                "patch_id": patch_id,
                "branch": branch,
                "status": "push_failed",
                "stdout": (push.stdout or "")[-2000:],
                "stderr": (push.stderr or "")[-2000:],
            })
            continue
        pr = subprocess.run([
            "gh", "pr", "create",
            "--base", base,
            "--head", branch,
            "--title", f"self-evolution: {patch_id}",
            "--body", f"Auto-created by self evolution engine for {patch_id}.",
        ], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120)
        results.append({
            "patch_id": patch_id,
            "branch": branch,
            "status": "pr_created" if pr.returncode == 0 else "pr_failed",
            "stdout": (pr.stdout or "")[-4000:],
            "stderr": (pr.stderr or "")[-4000:],
        })
    MODEL_REMOTE_PR_RUNTIME_FILE.write_text(
        json.dumps({
            "ts": datetime.now().isoformat(),
            "status": "ok",
            "remote": remote,
            "base": base,
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results



def run_self_evolution_cycle() -> dict:
    """运行自我进化周期"""
    print("=== 自我进化引擎 ===")
    print(f"时间: {datetime.now().isoformat()}")

    # 加载数据库
    store = _load_guardian_db()

    # 1. 扫描重复问题
    print("\n[1] 扫描重复问题...")
    recurring = scan_recurring_problems(store, days=7)
    print(f"发现 {len(recurring)} 个重复问题模式")

    # 2. 分析根因
    print("\n[2] 分析根因...")
    analyses = []
    for problem in recurring:
        root_cause = analyze_root_cause(
            problem["pattern"],
            problem["task_ids"],
            store
        )
        analyses.append({
            "problem": problem,
            "root_cause": root_cause,
        })
        print(f"  - {problem['pattern']}: {root_cause['description']}")

    # 3. 生成修复候选
    print("\n[3] 生成修复候选...")
    fix_candidates = []
    for analysis in analyses:
        candidate = generate_fix_candidate(
            analysis["problem"],
            analysis["root_cause"]
        )
        fix_candidates.append(candidate)
        print(f"  - {candidate['fix']['action']}: {candidate['fix']['description']}")

    # 4. 模型参与决策：生成诊断请求与摘要
    print("\n[4] 生成模型诊断请求...")
    model_requests = []
    for candidate in fix_candidates:
        risk = candidate.get("risk") or {}
        if str(risk.get("execution_mode") or "") in ("model_required", "model_review", "auto_with_model_summary"):
            req = build_model_diagnosis_request(
                candidate.get("problem") or {},
                candidate.get("root_cause") or {},
                candidate,
                risk,
            )
            model_requests.append(req)
            print(f"  - {req['request_id']}: {req['problem']['pattern']} [{risk.get('execution_mode')}]")
    persist_model_diagnosis_requests(model_requests)
    model_summaries = summarize_model_decision_inputs(fix_candidates)

    print("\n[5] 消费模型诊断请求...")
    model_decisions = consume_model_diagnosis_queue()
    for item in model_decisions:
        payload = item.get("decision") or {}
        print(f"  - {item.get('request_id')}: {payload.get('decision')} -> {payload.get('next_step')}")
    fix_candidates = apply_model_decisions_to_candidates(fix_candidates, model_decisions)

    # 6. 记录进化日志
    evolution_record = {
        "ts": int(time.time()),
        "iso_time": datetime.now().isoformat(),
        "recurring_problems": len(recurring),
        "analyses": analyses[:10],
        "fix_candidates": fix_candidates[:10],
        "model_requests": model_requests[:10],
        "model_summaries": model_summaries[:10],
        "model_decisions": model_decisions[:10],
    }
    _append_jsonl(EVOLUTION_LOG, evolution_record)

    # 7. 执行自动化修复
    print("\n[4] 执行自动化修复...")
    executed_fixes = execute_auto_fixes(fix_candidates, store)
    for fix in executed_fixes:
        status = fix.get("status")
        count = fix.get("tasks_affected", 0)
        print(f"  - {fix['action']}: {status} ({count} tasks)")
    evolution_record["executed_fixes"] = executed_fixes

    # 6. 写 patch 提案
    print("\n[5] 写 patch 提案...")
    patches = []
    for candidate in fix_candidates:
        patch = write_patch_proposal(candidate)
        patches.append(patch)
        print(f"  - {patch['patch_id']}: {patch['action']}")
    evolution_record["patches"] = patches

    # 7. 运行验证
    print("\n[6] 运行验证...")
    verifications = []
    for patch in patches:
        result = run_verification(patch)
        verifications.append(result)
        status = "✅ PASS" if result["passed"] else "❌ FAIL"
        print(f"  - {patch['patch_id']}: {status} - {result['summary']}")
    evolution_record["verifications"] = verifications

    # 8. 生成代码修改
    print("\n[7] 生成代码修改...")
    code_patches = []
    for patch in patches:
        if patch.get("action") in ("reduce_timeout_threshold", "enforce_receipt_protocol", "enforce_delivery_confirmation"):
            code_patch = generate_code_patch(patch)
            code_patches.append(code_patch)
            print(f"  - {patch['patch_id']}: {code_patch['status']}")
    evolution_record["code_patches"] = code_patches

    # 9. 应用代码修改
    print("\n[8] 应用代码修改...")
    apply_results = []
    for code_patch in code_patches:
        if code_patch.get("status") == "ready_to_apply":
            apply_result = apply_code_patch(code_patch)
            apply_results.append(apply_result)
            print(f"  - {code_patch['patch_id']}: applied {apply_result['success_count']} changes")
    evolution_record["apply_results"] = apply_results

    # 10. 生成 PR 描述
    print("\n[9] 生成 PR 描述...")
    pr_descriptions = []
    for i, code_patch in enumerate(code_patches):
        if i < len(apply_results):
            pr_desc = generate_pr_description(code_patch, apply_results[i])
            pr_descriptions.append(pr_desc)
            print(f"  - {code_patch['patch_id']}: PR 描述已生成")
    evolution_record["pr_descriptions"] = pr_descriptions

    # 11. 保存 PR 描述文件
    pr_dir = DATA_DIR / "self-evolution-prs"
    pr_dir.mkdir(parents=True, exist_ok=True)
    for i, pr_desc in enumerate(pr_descriptions):
        pr_file = pr_dir / f"pr-{int(time.time())}-{i}.md"
        pr_file.write_text(pr_desc, encoding="utf-8")
    print(f"  PR 描述已保存到: {pr_dir}")

    # 12. 导出 commit-ready 工件
    print("\n[10] 导出 commit-ready 工件...")
    commit_ready_artifacts = []
    for i, code_patch in enumerate(code_patches):
        if i < len(apply_results) and i < len(pr_descriptions):
            artifact = export_commit_ready_artifact(code_patch, apply_results[i], pr_descriptions[i])
            commit_ready_artifacts.append(artifact)
            print(f"  - {artifact['patch_id']}: {artifact['status']}")
    evolution_record["commit_ready_artifacts"] = commit_ready_artifacts

    # 13. 可选创建本地 git 提交
    print("\n[11] 可选创建本地 git 提交...")
    git_results = maybe_create_local_commit(commit_ready_artifacts)
    for item in git_results:
        print(f"  - {item.get('patch_id')}: {item.get('status')} ({item.get('branch','no-branch')})")
    evolution_record["git_results"] = git_results

    # 14. 可选推送远端并创建 PR
    print("\n[12] 可选推送远端并创建 PR...")
    remote_pr_results = maybe_create_remote_pr(git_results)
    for item in remote_pr_results:
        print(f"  - {item.get('patch_id')}: {item.get('status')} ({item.get('branch','no-branch')})")
    evolution_record["remote_pr_results"] = remote_pr_results

    # 15. 生成报告
    print("\n[10] 生成报告...")
    report = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "recurring_problems": len(recurring),
            "high_severity": len([p for p in recurring if p["severity"] == "high"]),
            "fix_candidates": len(fix_candidates),
            "model_requests": len(model_requests),
            "patches_written": len(patches),
            "verifications_passed": len([v for v in verifications if v["passed"]]),
            "verifications_failed": len([v for v in verifications if not v["passed"]]),
            "code_patches_generated": len(code_patches),
            "code_patches_applied": len([r for r in apply_results if r.get("success_count", 0) > 0]),
            "pr_descriptions_generated": len(pr_descriptions),
            "commit_ready_artifacts": len(commit_ready_artifacts),
            "git_commits_created": len([r for r in git_results if r.get("status") == "committed"]),
            "remote_prs_created": len([r for r in remote_pr_results if r.get("status") == "pr_created"]),
        },
        "top_problems": recurring[:5],
        "top_fixes": fix_candidates[:5],
        "model_requests": model_requests[:5],
        "model_summaries": model_summaries[:5],
        "patches": patches[:5],
        "verifications": verifications[:5],
        "code_patches": code_patches[:5],
        "apply_results": apply_results[:5],
        "commit_ready_artifacts": commit_ready_artifacts[:5],
        "git_results": git_results[:5],
        "remote_pr_results": remote_pr_results[:5],
    }

    # 保存报告
    report_file = DATA_DIR / "self-evolution-report.json"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n报告已保存到: {report_file}")
    print(f"进化日志已追加到: {EVOLUTION_LOG}")
    print(f"模型诊断队列已保存到: {MODEL_DIAGNOSIS_QUEUE_FILE}")
    print(f"模型决策摘要已保存到: {MODEL_DECISIONS_FILE}")
    print(f"Patch 队列已保存到: {PATCH_QUEUE_FILE}")
    print(f"验证结果已保存到: {VERIFICATION_FILE}")
    print(f"PR 描述已保存到: {pr_dir}")
    print(f"Commit-ready 工件已保存到: {MODEL_COMMIT_READY_DIR}")
    print(f"Git 运行状态已保存到: {MODEL_GIT_RUNTIME_FILE}")
    print(f"远端 PR 运行状态已保存到: {MODEL_REMOTE_PR_RUNTIME_FILE}")

    return report


def main():
    return run_self_evolution_cycle()


if __name__ == "__main__":
    main()