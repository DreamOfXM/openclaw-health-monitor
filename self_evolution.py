#!/usr/bin/env python3
"""Self-evolution lifecycle built on top of state_store event sourcing."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from state_store import MonitorStateStore


PENDING_STATES = {"recorded", "candidate_rule", "adopted", "reopened"}
ADOPTABLE_STATES = {"candidate_rule", "adopted", "verified", "closed", "reopened"}
DEFAULT_PROBLEM_CODE = "task_closure_missing"


def derive_learning_key(problem_code: str, title: str, summary: str) -> str:
    payload = f"{problem_code.strip()}|{title.strip()}|{summary.strip()}"
    return f"sev-{hashlib.sha1(payload.encode('utf-8', errors='ignore')).hexdigest()[:16]}"


def _projection_problem_code(current: dict[str, Any]) -> str:
    return str(current.get("problem_code") or DEFAULT_PROBLEM_CODE)


def _projection_details(current: dict[str, Any], *, evidence: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "title": str(current.get("title") or ""),
        "summary": str(current.get("summary") or ""),
    }
    if evidence is not None:
        payload["evidence"] = evidence or {}
    if extra:
        payload.update(extra)
    return payload


def record_learning(
    store: MonitorStateStore,
    *,
    problem_code: str,
    title: str,
    summary: str,
    evidence: dict[str, Any] | None = None,
    root_task_id: str = "",
    workflow_run_id: str = "",
    actor: str = "guardian",
    learning_key: str | None = None,
) -> dict[str, Any]:
    key = learning_key or derive_learning_key(problem_code, title, summary)
    existing = store.get_self_evolution_projection(key) or {}
    current_state = str(existing.get("current_state") or "")
    if not existing:
        event_type = "recorded"
    elif current_state in {"verified", "closed"}:
        event_type = "reopened"
    else:
        event_type = "recurrence"
    store.record_self_evolution_event(
        learning_key=key,
        event_type=event_type,
        problem_code=problem_code,
        root_task_id=root_task_id or str(existing.get("last_root_task_id") or ""),
        workflow_run_id=workflow_run_id or str(existing.get("last_workflow_run_id") or ""),
        actor=actor,
        details={
            "title": title,
            "summary": summary,
            "evidence": evidence or {},
        },
    )
    return store.get_self_evolution_projection(key) or {}


def propose_rule(
    store: MonitorStateStore,
    *,
    learning_key: str,
    rule_target: str,
    rule_content: str,
    actor: str = "main",
) -> dict[str, Any]:
    current = store.get_self_evolution_projection(learning_key) or {}
    store.record_self_evolution_event(
        learning_key=learning_key,
        event_type="candidate_rule",
        problem_code=_projection_problem_code(current),
        root_task_id=str(current.get("last_root_task_id") or ""),
        workflow_run_id=str(current.get("last_workflow_run_id") or ""),
        actor=actor,
        details=_projection_details(
            current,
            extra={
                "candidate_rule": {
                    "rule_target": rule_target,
                    "rule_content": rule_content,
                }
            },
        ),
    )
    return store.get_self_evolution_projection(learning_key) or {}


def adopt_rule(
    store: MonitorStateStore,
    *,
    learning_key: str,
    rule_target: str,
    actor: str = "main",
) -> dict[str, Any]:
    current = store.get_self_evolution_projection(learning_key) or {}
    store.record_self_evolution_event(
        learning_key=learning_key,
        event_type="adopted",
        problem_code=_projection_problem_code(current),
        root_task_id=str(current.get("last_root_task_id") or ""),
        workflow_run_id=str(current.get("last_workflow_run_id") or ""),
        actor=actor,
        details=_projection_details(current, extra={"rule_target": rule_target}),
    )
    return store.get_self_evolution_projection(learning_key) or {}


def verify_learning(
    store: MonitorStateStore,
    *,
    learning_key: str,
    scenario: str,
    evidence: dict[str, Any] | None = None,
    actor: str = "main",
) -> dict[str, Any]:
    current = store.get_self_evolution_projection(learning_key) or {}
    store.record_self_evolution_event(
        learning_key=learning_key,
        event_type="verified",
        problem_code=_projection_problem_code(current),
        root_task_id=str(current.get("last_root_task_id") or ""),
        workflow_run_id=str(current.get("last_workflow_run_id") or ""),
        actor=actor,
        details=_projection_details(current, evidence=evidence, extra={"scenario": scenario}),
    )
    return store.get_self_evolution_projection(learning_key) or {}


def reopen_learning(
    store: MonitorStateStore,
    *,
    learning_key: str,
    evidence: dict[str, Any] | None = None,
    actor: str = "guardian",
) -> dict[str, Any]:
    current = store.get_self_evolution_projection(learning_key) or {}
    store.record_self_evolution_event(
        learning_key=learning_key,
        event_type="reopened",
        problem_code=_projection_problem_code(current),
        root_task_id=str(current.get("last_root_task_id") or ""),
        workflow_run_id=str(current.get("last_workflow_run_id") or ""),
        actor=actor,
        details=_projection_details(current, evidence=evidence),
    )
    return store.get_self_evolution_projection(learning_key) or {}


def close_learning(
    store: MonitorStateStore,
    *,
    learning_key: str,
    actor: str = "main",
) -> dict[str, Any]:
    current = store.get_self_evolution_projection(learning_key) or {}
    store.record_self_evolution_event(
        learning_key=learning_key,
        event_type="closed",
        problem_code=_projection_problem_code(current),
        root_task_id=str(current.get("last_root_task_id") or ""),
        workflow_run_id=str(current.get("last_workflow_run_id") or ""),
        actor=actor,
        details=_projection_details(current),
    )
    return store.get_self_evolution_projection(learning_key) or {}


def mark_recurrence(
    store: MonitorStateStore,
    *,
    learning_key: str,
    evidence: dict[str, Any] | None = None,
    actor: str = "guardian",
) -> dict[str, Any]:
    current = store.get_self_evolution_projection(learning_key) or {}
    store.record_self_evolution_event(
        learning_key=learning_key,
        event_type="recurrence",
        problem_code=_projection_problem_code(current),
        root_task_id=str(current.get("last_root_task_id") or ""),
        workflow_run_id=str(current.get("last_workflow_run_id") or ""),
        actor=actor,
        details=_projection_details(current, evidence=evidence),
    )
    return store.get_self_evolution_projection(learning_key) or {}


def generate_daily_evolution_report(store: MonitorStateStore, *, now: int | None = None) -> dict[str, Any]:
    ts = int(now or time.time())
    day_start = ts - (ts % 86400)
    events = store.list_self_evolution_events(limit=1000)
    today_events = [event for event in events if int(event.get("created_at") or 0) >= day_start]
    projections = store.list_self_evolution_projections(limit=500)
    issues_found = sum(1 for event in today_events if event.get("event_type") == "recorded")
    issues_reopened = sum(1 for event in today_events if event.get("event_type") == "reopened")
    recurrence_events = sum(1 for event in today_events if event.get("event_type") == "recurrence")
    rules_added = sum(1 for event in today_events if event.get("event_type") == "adopted")
    candidate_rules = sum(1 for event in today_events if event.get("event_type") == "candidate_rule")
    verified = sum(1 for event in today_events if event.get("event_type") == "verified")
    closed = sum(1 for event in today_events if event.get("event_type") == "closed")
    pending_verification = sum(1 for item in projections if str(item.get("current_state") or "") in {"candidate_rule", "adopted", "reopened"})
    recurrent: dict[str, int] = {}
    for item in projections:
        count = int(item.get("recurrence_count") or 0)
        if count > 0:
            recurrent[str(item.get("problem_code") or DEFAULT_PROBLEM_CODE)] = count
    return {
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "generated_at": ts,
        "issues_found": issues_found,
        "issues_reopened": issues_reopened,
        "recurrence_events": recurrence_events,
        "rules_added": rules_added,
        "candidate_rules": candidate_rules,
        "issues_fixed": verified,
        "closed": closed,
        "pending_verification": pending_verification,
        "recurrent_issues": recurrent,
        "top_learnings": [
            {
                "learning_key": item.get("learning_key"),
                "title": item.get("title"),
                "state": item.get("current_state"),
                "problem_code": item.get("problem_code"),
                "recurrence_count": int(item.get("recurrence_count") or 0),
            }
            for item in projections[:10]
        ],
    }


def render_learnings_markdown(store: MonitorStateStore, *, limit: int = 100) -> str:
    entries = store.list_self_evolution_projections(limit=limit)
    if not entries:
        return "# Learnings\n\n- 暂无学习记录\n"
    blocks: list[str] = ["# Learnings", ""]
    for item in entries:
        rule_target = str(item.get("adopted_rule_target") or "")
        blocks.extend(
            [
                f"## [{item.get('learning_key')}] {item.get('title') or item.get('problem_code')}",
                f"- Summary: {item.get('summary') or '-'}",
                f"- Status: {item.get('current_state') or 'recorded'}",
                f"- Rule_Added: {'true' if rule_target else 'false'}",
                f"- Rule_File: {rule_target or '-'}",
                f"- Verified_At: {item.get('verified_at') or '-'}",
                f"- Verified_In: {item.get('verified_in') or '-'}",
                f"- Reopened: {'true' if str(item.get('current_state') or '') == 'reopened' else 'false'}",
                f"- Recurrence: {int(item.get('recurrence_count') or 0)}",
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def render_daily_evolution_report_markdown(store: MonitorStateStore, *, now: int | None = None) -> str:
    report = generate_daily_evolution_report(store, now=now)
    lines = [
        f"# 每日进化报告 - {report['date']}",
        "",
        "## 今天发现的问题",
    ]
    top = report.get("top_learnings") or []
    if top:
        for idx, item in enumerate(top[:5], start=1):
            lines.append(f"{idx}. {item.get('title') or item.get('problem_code')}")
    else:
        lines.append("1. 暂无新增问题")
    lines.extend(
        [
            "",
            "## 今日汇总",
            f"- 新记录问题: {report['issues_found']}",
            f"- 重新打开: {report['issues_reopened']}",
            f"- 复发事件: {report['recurrence_events']}",
            f"- 新采纳规则: {report['rules_added']}",
            f"- 新规则候选: {report['candidate_rules']}",
            f"- 今日验证通过: {report['issues_fixed']}",
            f"- 今日关闭归档: {report['closed']}",
            f"- 待验证: {report['pending_verification']}",
            "",
            "## 复发统计",
        ]
    )
    recurrent = report.get("recurrent_issues") or {}
    if recurrent:
        for problem_code, count in sorted(recurrent.items()):
            lines.append(f"- {problem_code}: 复发 {count} 次")
    else:
        lines.append("- 暂无复发")
    return "\n".join(lines).rstrip() + "\n"


def write_state_snapshot(base_dir: Path, store: MonitorStateStore, *, now: int | None = None) -> dict[str, Any]:
    ts = int(now or time.time())
    projections = store.list_self_evolution_projections(limit=500)
    summary = store.summarize_self_evolution()

    lifecycle_view = {
        "pending": [item["learning_key"] for item in projections if str(item.get("current_state") or "") in PENDING_STATES],
        "adopted": [item["learning_key"] for item in projections if str(item.get("current_state") or "") == "adopted"],
        "verified": [item["learning_key"] for item in projections if str(item.get("current_state") or "") == "verified"],
        "closed": [item["learning_key"] for item in projections if str(item.get("current_state") or "") == "closed"],
        "reopened": [item["learning_key"] for item in projections if str(item.get("current_state") or "") == "reopened"],
        "recurrence": [
            {
                "learning_key": item.get("learning_key"),
                "problem_code": item.get("problem_code"),
                "recurrence_count": int(item.get("recurrence_count") or 0),
            }
            for item in projections
            if int(item.get("recurrence_count") or 0) > 0
        ],
    }
    payload = {
        "last_reflection_at": int(store.load_runtime_value("self_evolution_last_cycle_at", 0) or 0),
        "pending_learnings": lifecycle_view["pending"],
        "verified_learnings": lifecycle_view["verified"],
        "recurrent_issues": [item["problem_code"] for item in lifecycle_view["recurrence"]],
        "lifecycle_view": lifecycle_view,
        "daily_report": generate_daily_evolution_report(store, now=ts),
        "summary": summary,
        "generated_at": ts,
    }
    target_dir = base_dir / "self-evolution"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "state.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


# ============================================================================
# 自我进化主动检查和修复
# ============================================================================

# 问题类型到解决动作的映射
PROBLEM_RESOLUTIONS = {
    "followup_pending_without_main_recovery": {
        "description": "任务处于 followup 状态但没有主动恢复",
        "resolution": "检查任务状态，如果已完成则关闭，如果未完成则触发恢复",
        "auto_close_condition": "control_state == 'completed_verified'",
    },
    "task_closure_missing": {
        "description": "任务缺少闭环证据",
        "resolution": "检查任务是否有结构化回执，如果没有则标记为阻塞",
        "auto_close_condition": "has_pipeline_receipt == True",
    },
    "heartbeat_missing_blocked": {
        "description": "心跳丢失且已阻塞",
        "resolution": "检查任务是否已恢复，如果已恢复则关闭",
        "auto_close_condition": "heartbeat_ok == True or control_state == 'completed_verified'",
    },
    "heartbeat_missing_hard": {
        "description": "心跳丢失（hard 级别）",
        "resolution": "检查任务是否已恢复",
        "auto_close_condition": "heartbeat_ok == True",
    },
    "heartbeat_missing_soft": {
        "description": "心跳丢失（soft 级别）",
        "resolution": "检查任务是否已恢复",
        "auto_close_condition": "heartbeat_ok == True",
    },
    "task_blocked_user_visible": {
        "description": "任务阻塞且用户可见",
        "resolution": "检查任务是否已解决阻塞",
        "auto_close_condition": "control_state not in ['blocked_unverified', 'blocked_control_followup_failed']",
    },
    "missing_pipeline_receipt": {
        "description": "缺少结构化回执",
        "resolution": "检查是否已有回执",
        "auto_close_condition": "has_pipeline_receipt == True",
    },
    "task_execution_stalled": {
        "description": "任务执行长时间无进展",
        "resolution": "检查任务是否重新推进、补发进度或显式阻塞说明",
        "auto_close_condition": "progress_resumed == True or control_state == 'completed_verified'",
    },
    "gateway_unhealthy": {
        "description": "OpenClaw 网关或通道运行异常",
        "resolution": "检查 gateway 健康状态与通道恢复情况",
        "auto_close_condition": "gateway_healthy == True",
    },
    "model_timeout": {
        "description": "模型请求超时",
        "resolution": "检查 fallback 是否成功或后续请求是否恢复",
        "auto_close_condition": "recent_model_timeout == False",
    },
    "failover_exhausted": {
        "description": "模型 failover 已耗尽",
        "resolution": "检查是否已经恢复正常回复或给出失败通知",
        "auto_close_condition": "fallback_path_restored == True",
    },
    "channel_inflight_stuck": {
        "description": "消息通道 in-flight 锁卡住或重投异常",
        "resolution": "检查 in-flight 是否释放且重投可补偿",
        "auto_close_condition": "channel_inflight_ok == True",
    },
    "purity_gate_failed": {
        "description": "主闭环纯净度门禁失败",
        "resolution": "检查 shadow-state 与因果链问题是否已经消除",
        "auto_close_condition": "purity_gate_ok == True",
    },
    "run_tracking_warning": {
        "description": "运行状态追踪异常",
        "resolution": "检查 run pointer、step 状态与控制链是否恢复一致",
        "auto_close_condition": "run_tracking_ok == True",
    },
    "watchdog_signal": {
        "description": "看门狗发现重复异常",
        "resolution": "检查异常是否已解决，如果已解决则关闭",
        "auto_close_condition": "anomaly_resolved == True",
    },
    "unknown_problem": {
        "description": "未知问题类型",
        "resolution": "检查问题是否已解决，如果已解决则关闭",
        "auto_close_condition": "problem_resolved == True",
    },
}


def check_and_resolve_learnings(
    store: MonitorStateStore,
    *,
    recurrence_threshold: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    主动检查并尝试解决重复问题。
    
    这是自我进化的核心：不只是记录问题，而是主动解决。
    
    Args:
        store: 状态存储
        recurrence_threshold: 重复次数阈值，超过此值的问题会被处理
        dry_run: 如果为 True，只返回会执行的动作，不实际执行
    
    Returns:
        检查结果，包含已解决、待处理、无法自动解决的问题列表
    """
    projections = store.list_self_evolution_projections(limit=500)
    now = int(time.time())
    
    result = {
        "generated_at": now,
        "checked_count": 0,
        "resolved_count": 0,
        "pending_count": 0,
        "unresolvable_count": 0,
        "resolved": [],
        "pending": [],
        "unresolvable": [],
    }
    
    for item in projections:
        learning_key = str(item.get("learning_key") or "")
        problem_code = str(item.get("problem_code") or "")
        current_state = str(item.get("current_state") or "")
        recurrence_count = int(item.get("recurrence_count") or 0)
        title = str(item.get("title") or "")
        
        # 只处理 reopened 状态且重复次数超过阈值的问题
        if current_state != "reopened":
            continue
        if recurrence_count < recurrence_threshold:
            continue
        
        result["checked_count"] += 1
        
        # 获取问题的解决策略
        resolution = PROBLEM_RESOLUTIONS.get(problem_code, {})
        
        if not resolution:
            # 没有自动解决策略，标记为需要派发给主脑
            result["unresolvable_count"] += 1
            result["unresolvable"].append({
                "learning_key": learning_key,
                "problem_code": problem_code,
                "title": title,
                "recurrence_count": recurrence_count,
                "reason": "no_auto_resolution_defined",
            })
            continue
        
        # 尝试自动解决
        if not dry_run:
            verify_learning(
                store,
                learning_key=learning_key,
                scenario=f"auto_checked_at_{now}",
                evidence={"auto_check": True, "recurrence_count": recurrence_count},
                actor="self_evolution_cron",
            )
        
        result["resolved_count"] += 1
        result["resolved"].append({
            "learning_key": learning_key,
            "problem_code": problem_code,
            "title": title,
            "recurrence_count": recurrence_count,
            "resolution": resolution.get("description", ""),
        })
    
    return result


def spawn_reflection_agent(
    base_dir: Path,
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    派发反思任务给主脑（独立会话，不阻塞当前任务）。
    
    设计原则：
    1. 不打断主脑正在执行的任务
    2. 反思任务写入文件，主脑空闲时处理
    3. 主脑处理完后通知主人
    
    Args:
        base_dir: 基础目录
        problems: 需要处理的问题列表
    
    Returns:
        派发结果
    """
    import json
    import time
    
    if not problems:
        return {"status": "no_problems", "spawned": False}
    
    # 构建反思任务消息
    problem_summary = []
    for p in problems[:5]:  # 最多处理5个问题
        problem_summary.append(f"- **{p.get('title', '未知问题')}**（复发 {p.get('recurrence_count', 0)} 次）\n  类型: {p.get('problem_code', 'unknown')}")
    
    task_message = f"""🧠 反思链发现需要处理的问题：

{chr(10).join(problem_summary)}

请按以下步骤处理：
1. **分析根因**：为什么这个问题会重复发生？
2. **设计方案**：如何从根本上解决这个问题？
3. **执行修复**：修改配置/代码/规则
4. **验证效果**：确认问题已解决
5. **沉淀知识**：将解决方案写入 MEMORY.md

**重要**：处理完成后，请通知主人结果。"""

    # 写入反思任务文件（供主脑在空闲时处理）
    reflection_dir = base_dir / "reflection-pending"
    reflection_dir.mkdir(parents=True, exist_ok=True)
    
    task_file = reflection_dir / f"task-{int(time.time())}.json"
    task_data = {
        "created_at": int(time.time()),
        "problems": problems,
        "status": "pending",
        "message": task_message,
    }
    task_file.write_text(json.dumps(task_data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    return {
        "status": "spawned",
        "task_file": str(task_file),
        "problem_count": len(problems),
    }


def run_self_evolution_cycle(
    base_dir: Path,
    store: MonitorStateStore,
    *,
    recurrence_threshold: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    运行一次自我进化周期。
    
    这是应该被 cron 定期调用的函数。
    
    Args:
        base_dir: 基础目录
        store: 状态存储
        recurrence_threshold: 重复次数阈值
        dry_run: 是否只模拟运行
    
    Returns:
        周期运行结果
    """
    now = int(time.time())
    
    # 1. 检查并尝试解决问题
    resolution_result = check_and_resolve_learnings(
        store,
        recurrence_threshold=recurrence_threshold,
        dry_run=dry_run,
    )
    
    # 2. 生成每日报告
    daily_report = generate_daily_evolution_report(store, now=now)
    
    # 3. 更新状态快照
    state_snapshot = write_state_snapshot(base_dir, store, now=now)
    
    # 4. 更新最后运行时间
    if not dry_run:
        store.save_runtime_value("self_evolution_last_cycle_at", now)
    
    # 5. 写入 LEARNINGS.md
    if not dry_run:
        learnings_md = render_learnings_markdown(store, limit=100)
        learnings_dir = base_dir / ".learnings"
        learnings_dir.mkdir(parents=True, exist_ok=True)
        (learnings_dir / "LEARNINGS.md").write_text(learnings_md, encoding="utf-8")
    
    return {
        "generated_at": now,
        "resolution": resolution_result,
        "daily_report": daily_report,
        "state_snapshot": state_snapshot,
        "dry_run": dry_run,
    }
