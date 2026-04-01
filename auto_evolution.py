#!/usr/bin/env python3
"""
真正的自动进化系统

核心机制：
1. 问题检测 → 分析根因 → 生成规则 → 采纳规则 → 验证效果
2. 规则类型：配置规则、约束规则、代码规则
3. 验证闭环：追踪问题是否减少

设计原则：
- 能自动修复的，自动修复
- 需要人工确认的，写入待办
- 修复后验证效果
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
import re


# ============================================================================
# 规则模板：问题类型 → 解决方案
# ============================================================================

RULE_TEMPLATES = {
    "missing_pipeline_receipt": {
        "category": "protocol",
        "description": "任务缺少结构化回执",
        "root_cause": "子代理没有发送 PIPELINE_RECEIPT",
        "solutions": [
            {
                "type": "constraint",
                "target": "AGENTS.md",
                "template": """
### {constraint_id}（{learning_key}）

**问题**：{description}
**根因**：{root_cause}
**约束**：{constraint_text}
**触发条件**：{trigger_condition}

**验证方法**：
- 检查 `has_pipeline_receipt` 是否为 True
- 如果为 False，标记为阻塞

**来源**：自我进化系统自动生成
**生成时间**：{generated_at}
""",
                "constraint_text": "所有子代理必须在任务开始时发送 confirmed 回执，在任务完成时发送 completed 回执",
                "trigger_condition": "派发任务给子代理时",
            },
        ],
    },
    
    "task_closure_missing": {
        "category": "protocol",
        "description": "任务缺少闭环证据",
        "root_cause": "completed != delivered，任务完成但未送达用户",
        "solutions": [
            {
                "type": "constraint",
                "target": "AGENTS.md",
                "template": """
### {constraint_id}（{learning_key}）

**问题**：{description}
**根因**：{root_cause}
**约束**：{constraint_text}

**来源**：自我进化系统自动生成
**生成时间**：{generated_at}
""",
                "constraint_text": "任务完成后必须检查 visible_completion 是否为 True，否则继续追踪直到送达",
            },
        ],
    },
    
    "followup_pending_without_main_recovery": {
        "category": "recovery",
        "description": "任务处于 followup 状态但没有主动恢复",
        "root_cause": "看门狗发现问题但没有触发恢复动作",
        "solutions": [
            {
                "type": "config",
                "target": "guardian_config.json",
                "action": "auto_recover",
                "config": {
                    "auto_recovery_enabled": True,
                    "recovery_actions": ["resend", "rebind", "notify_main"],
                },
            },
        ],
    },
    
    "heartbeat_missing_hard": {
        "category": "monitoring",
        "description": "心跳丢失（hard 级别）",
        "root_cause": "子代理进程崩溃或网络中断",
        "solutions": [
            {
                "type": "alert",
                "target": "notification",
                "template": "⚠️ 子代理 {agent_id} 心跳丢失超过 {threshold} 秒，请检查",
            },
        ],
    },
    
    "delivery_failed": {
        "category": "delivery",
        "description": "消息送达失败",
        "root_cause": "通道错误或权限问题",
        "solutions": [
            {
                "type": "config",
                "target": "delivery_config.json",
                "action": "retry_with_fallback",
                "config": {
                    "retry_count": 3,
                    "fallback_channels": ["feishu", "dingtalk"],
                },
            },
        ],
    },
    
    "protocol_violation": {
        "category": "protocol",
        "description": "协议违规",
        "root_cause": "子代理没有遵守通信协议",
        "solutions": [
            {
                "type": "constraint",
                "target": "AGENTS.md",
                "template": """
### {constraint_id}（{learning_key}）

**问题**：{description}
**根因**：{root_cause}
**约束**：{constraint_text}
**违规详情**：{violation_details}

**来源**：自我进化系统自动生成
**生成时间**：{generated_at}
""",
                "constraint_text": "严格遵守 PIPELINE_RECEIPT 协议",
            },
        ],
    },
    "guardian_crash": {
        "category": "monitoring",
        "description": "看门狗进程崩溃或启动失败",
        "root_cause": "guardian 自身异常，导致未闭环任务无人追踪",
        "solutions": [
            {
                "type": "constraint",
                "target": "AGENTS.md",
                "template": """
### {constraint_id}（{learning_key}）

**问题**：{description}
**根因**：{root_cause}
**约束**：自我进化周期必须检查 guardian.launchd.err.log 与 guardian 进程状态；发现 guardian 崩溃必须优先修复，不得只记录业务问题。

**来源**：自我进化系统自动生成
**生成时间**：{generated_at}
""",
                "constraint_text": "guardian 崩溃优先级高于普通业务问题",
            }
        ]
    },
    "tool_interrupted_no_reply": {
        "category": "delivery",
        "description": "主脑在工具执行阶段中断，导致没有最终回复",
        "root_cause": "工具执行/会话修复异常后，没有形成用户可见终态",
        "solutions": [
            {
                "type": "constraint",
                "target": "AGENTS.md",
                "template": """
### {constraint_id}（{learning_key}）

**问题**：{description}
**根因**：{root_cause}
**约束**：涉及 restart/reload/tool 执行的核心操作，必须在执行前给出阶段确认；若工具失败，必须立即发送 blocked explanation，不得静默结束。

**来源**：自我进化系统自动生成
**生成时间**：{generated_at}
""",
                "constraint_text": "工具中断必须转成用户可见终态",
            }
        ]
    },
}


# ============================================================================
# 规则生成器
# ============================================================================

def generate_candidate_rule(
    problem_code: str,
    learning_key: str,
    evidence: dict[str, Any],
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    """
    从问题模式生成候选规则。
    
    Args:
        problem_code: 问题类型代码
        learning_key: 学习记录 ID
        evidence: 问题证据
        now: 当前时间戳
    
    Returns:
        候选规则，如果没有匹配的模板则返回 None
    """
    template = RULE_TEMPLATES.get(problem_code)
    if not template:
        return None
    
    ts = int(now or time.time())
    solutions = template.get("solutions", [])
    if not solutions:
        return None
    
    # 选择第一个解决方案
    solution = solutions[0]
    solution_type = solution.get("type", "constraint")
    
    # 生成规则 ID
    constraint_id = f"AUTO-{problem_code.upper()}-{ts}"
    
    # 填充模板
    rule_content = None
    if solution_type == "constraint":
        rule_template = solution.get("template", "")
        rule_content = rule_template.format(
            constraint_id=constraint_id,
            learning_key=learning_key,
            description=template.get("description", ""),
            root_cause=template.get("root_cause", ""),
            constraint_text=solution.get("constraint_text", ""),
            trigger_condition=solution.get("trigger_condition", ""),
            violation_details=json.dumps(evidence, ensure_ascii=False, indent=2),
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        )
    elif solution_type == "config":
        rule_content = {
            "action": solution.get("action"),
            "config": solution.get("config", {}),
        }
    elif solution_type == "alert":
        rule_template = solution.get("template", "")
        rule_content = rule_template.format(**evidence)
    
    return {
        "rule_id": constraint_id,
        "rule_type": solution_type,
        "rule_target": solution.get("target", "AGENTS.md"),
        "rule_content": rule_content,
        "problem_code": problem_code,
        "learning_key": learning_key,
        "category": template.get("category", "unknown"),
        "description": template.get("description", ""),
        "root_cause": template.get("root_cause", ""),
        "generated_at": ts,
        "status": "candidate",
    }


# ============================================================================
# 规则采纳器
# ============================================================================

def adopt_rule(
    rule: dict[str, Any],
    base_dir: Path,
    workspace_dir: Path = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    采纳规则：将规则写入目标文件。
    
    Args:
        rule: 候选规则
        base_dir: 基础目录（用于配置文件）
        workspace_dir: 工作区目录（用于 AGENTS.md）
        dry_run: 是否只模拟运行
    
    Returns:
        采纳结果
    """
    rule_type = rule.get("rule_type", "constraint")
    rule_target = rule.get("rule_target", "AGENTS.md")
    rule_content = rule.get("rule_content", "")
    
    result = {
        "rule_id": rule.get("rule_id"),
        "rule_type": rule_type,
        "rule_target": rule_target,
        "status": "adopted",
        "adopted_at": int(time.time()),
        "dry_run": dry_run,
        "changes": [],
    }
    
    if dry_run:
        result["status"] = "dry_run"
        result["preview"] = rule_content
        return result
    
    if rule_type == "constraint":
        # 写入 AGENTS.md（使用 workspace_dir）
        target_dir = workspace_dir if workspace_dir else base_dir
        agents_md_path = target_dir / "AGENTS.md"
        if agents_md_path.exists():
            existing = agents_md_path.read_text(encoding="utf-8")
            # 检查是否已存在相同问题类型的约束
            # 从 rule_id 中提取问题类型（如 GUARDIAN_CRASH, MISSING_PIPELINE_RECEIPT）
            rule_id = rule.get("rule_id", "")
            problem_type = rule_id.split("-")[1] if "-" in rule_id else rule_id
            # 检查是否已有相同问题类型的 AUTO- 约束
            if f"### AUTO-{problem_type}" in existing or f"AUTO-{problem_type}" in existing:
                result["status"] = "already_exists"
                result["reason"] = f"约束类型 {problem_type} 已存在，跳过重复生成"
                return result
            # 追加到文件末尾
            new_content = existing.rstrip() + "\n\n" + rule_content + "\n"
            agents_md_path.write_text(new_content, encoding="utf-8")
            result["changes"].append({
                "file": str(agents_md_path),
                "action": "append",
                "lines_added": len(rule_content.split("\n")),
            })
        else:
            result["status"] = "target_not_found"
    
    elif rule_type == "config":
        # 写入配置文件
        config_path = base_dir / rule_target
        config_data = {}
        if config_path.exists():
            try:
                config_data = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        # 合并配置
        config_data.update(rule_content.get("config", {}))
        config_path.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")
        result["changes"].append({
            "file": str(config_path),
            "action": "update",
        })
    
    elif rule_type == "alert":
        # 写入待发送通知队列
        alerts_dir = base_dir / "alerts"
        alerts_dir.mkdir(parents=True, exist_ok=True)
        alert_file = alerts_dir / f"alert-{int(time.time())}.txt"
        alert_file.write_text(rule_content, encoding="utf-8")
        result["changes"].append({
            "file": str(alert_file),
            "action": "create",
        })
    
    return result


# ============================================================================
# 验证闭环
# ============================================================================

def verify_rule_effectiveness(
    store,
    rule: dict[str, Any],
    *,
    observation_period_hours: int = 24,
) -> dict[str, Any]:
    """
    验证规则是否生效：检查问题是否减少。
    
    Args:
        store: 状态存储
        rule: 已采纳的规则
        observation_period_hours: 观察期（小时）
    
    Returns:
        验证结果
    """
    from state_store import MonitorStateStore
    
    learning_key = rule.get("learning_key", "")
    problem_code = rule.get("problem_code", "")
    adopted_at = rule.get("adopted_at", 0)
    
    # 获取该问题的复发次数
    projection = store.get_self_evolution_projection(learning_key) or {}
    recurrence_count = int(projection.get("recurrence_count", 0))
    
    # 获取最近的事件
    events = store.list_self_evolution_events(learning_key=learning_key, limit=100)
    
    # 统计采纳前后的事件数量
    events_before = [e for e in events if int(e.get("created_at", 0)) < adopted_at]
    events_after = [e for e in events if int(e.get("created_at", 0)) >= adopted_at]
    
    # 判断效果
    is_effective = len(events_after) < len(events_before) // 2  # 事件减少 50% 以上
    
    return {
        "rule_id": rule.get("rule_id"),
        "learning_key": learning_key,
        "problem_code": problem_code,
        "adopted_at": adopted_at,
        "events_before_adoption": len(events_before),
        "events_after_adoption": len(events_after),
        "recurrence_count": recurrence_count,
        "is_effective": is_effective,
        "observation_period_hours": observation_period_hours,
        "verified_at": int(time.time()),
    }


def cleanup_learnings_archive(workspace_dir: Path, days_threshold: int = 7) -> dict[str, Any]:
    """
    清理 LEARNINGS.md 中已解决的条目，归档到 archive/ 目录。
    
    Args:
        workspace_dir: 工作区目录
        days_threshold: resolved 状态保留天数
    
    Returns:
        清理结果
    """
    import shutil
    from datetime import datetime, timedelta
    
    learnings_path = workspace_dir / ".learnings" / "LEARNINGS.md"
    archive_dir = workspace_dir / ".learnings" / "archive"
    
    if not learnings_path.exists():
        return {"status": "skipped", "reason": "LEARNINGS.md not found"}
    
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    content = learnings_path.read_text(encoding="utf-8")
    
    # Find all entries
    pattern = r'## \[LRN-[^\]]+\][\s\S]*?(?=## \[LRN-|$)'
    entries = re.findall(pattern, content)
    
    resolved_entries = []
    active_entries = []
    
    cutoff_date = datetime.now() - timedelta(days=days_threshold)
    
    for entry in entries:
        # Extract logged date
        logged_match = re.search(r'\*\*Logged\*\*:\s*(\d{4}-\d{2}-\d{2})', entry)
        logged_date = None
        if logged_match:
            try:
                logged_date = datetime.strptime(logged_match.group(1), "%Y-%m-%d")
            except ValueError:
                logged_date = datetime.now()
        
        if '**Status**: resolved' in entry or '**Status**: promoted' in entry:
            # Only archive if older than threshold
            if logged_date and logged_date < cutoff_date:
                resolved_entries.append(entry)
            else:
                # Keep recent resolved entries for reference
                active_entries.append(entry)
        else:
            active_entries.append(entry)
    
    if not resolved_entries:
        return {
            "status": "no_change",
            "active_entries": len(active_entries),
            "resolved_entries": 0,
        }
    
    # Write archive
    archive_date = datetime.now().strftime('%Y-%m-%d')
    archive_path = archive_dir / f"LEARNINGS-archive-{archive_date}.md"
    
    # If archive already exists for today, append
    if archive_path.exists():
        existing = archive_path.read_text(encoding="utf-8")
        archive_content = existing + "\n\n---\n\n" + "\n".join(resolved_entries)
    else:
        archive_content = f"# LEARNINGS Archive - {archive_date}\n\n"
        archive_content += f"Archived {len(resolved_entries)} resolved/promoted entries.\n\n"
        archive_content += "---\n\n"
        archive_content += "\n".join(resolved_entries)
    
    archive_path.write_text(archive_content, encoding="utf-8")
    
    # Write active entries back
    new_content = "# 学习记录\n\n> 只保留活跃问题。已解决的记录归档到 archive/ 目录。\n\n---\n\n"
    new_content += "\n".join(active_entries)
    
    learnings_path.write_text(new_content, encoding="utf-8")
    
    return {
        "status": "cleaned",
        "active_entries": len(active_entries),
        "resolved_entries": len(resolved_entries),
        "archive_path": str(archive_path),
        "bytes_saved": len(content) - len(new_content),
    }


def cleanup_agents_auto_constraints(workspace_dir: Path) -> dict[str, Any]:
    """
    清理 AGENTS.md 中的 AUTO- 约束（已固化的）。
    
    Args:
        workspace_dir: 工作区目录
    
    Returns:
        清理结果
    """
    agents_path = workspace_dir / "AGENTS.md"
    
    if not agents_path.exists():
        return {"status": "skipped", "reason": "AGENTS.md not found"}
    
    content = agents_path.read_text(encoding="utf-8")
    
    # Find AUTO- constraints
    auto_pattern = r'\n### AUTO-[^\n]+[\s\S]*?(?=\n### |\n---\n\*最后更新|$)'
    auto_matches = list(re.finditer(auto_pattern, content))
    
    # Find MC- constraints (morning meeting auto-generated)
    mc_pattern = r'\n### MC-[^\n]+[\s\S]*?(?=\n### |\n---\n\*最后更新|$)'
    mc_matches = list(re.finditer(mc_pattern, content))
    
    if not auto_matches and not mc_matches:
        return {"status": "no_change", "auto_count": 0, "mc_count": 0}
    
    # Remove AUTO- and MC- constraints
    new_content = content
    for match in reversed(auto_matches + mc_matches):
        new_content = new_content[:match.start()] + new_content[match.end():]
    
    agents_path.write_text(new_content, encoding="utf-8")
    
    return {
        "status": "cleaned",
        "auto_count": len(auto_matches),
        "mc_count": len(mc_matches),
        "bytes_saved": len(content) - len(new_content),
    }


def scan_system_health(base_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    guardian_err = base_dir / "logs" / "guardian.launchd.err.log"
    if guardian_err.exists():
        text = guardian_err.read_text(encoding="utf-8", errors="ignore")[-20000:]
        if "sqlite3.IntegrityError" in text or "Traceback" in text:
            findings.append({
                "problem_code": "guardian_crash",
                "learning_key": "guardian-crash-healthcheck",
                "evidence": {
                    "log": "guardian.launchd.err.log",
                    "matched": "sqlite3.IntegrityError" if "sqlite3.IntegrityError" in text else "Traceback",
                },
            })
    gateway_log = Path("/tmp/openclaw/openclaw-2026-03-19.log")
    if gateway_log.exists():
        text = gateway_log.read_text(encoding="utf-8", errors="ignore")[-50000:]
        if "missing tool result in session history" in text:
            findings.append({
                "problem_code": "tool_interrupted_no_reply",
                "learning_key": "tool-interrupted-healthcheck",
                "evidence": {
                    "log": str(gateway_log),
                    "matched": "missing tool result in session history",
                },
            })
    return findings


# ============================================================================
# 完整的进化周期（证据驱动版）
# ============================================================================

def run_evolution_cycle(
    store,
    base_dir: Path,
    workspace_dir: Path = None,
    *,
    recurrence_threshold: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    运行一次完整的进化周期（证据驱动，默认执行）。
    
    新协议：
    1. 只输出证据，不输出意向
    2. 默认直接执行，不再请示
    3. 每条反思必须绑定真实动作句柄（文件/函数/任务 ID）
    4. 只允许三种终态：fixed / blocked / queued
    
    流程：
    1. 扫描重复问题
    2. 生成候选规则
    3. 采纳规则（默认执行）
    4. 验证效果
    5. 更新状态
    
    Args:
        store: 状态存储
        base_dir: 基础目录
        workspace_dir: 工作区目录（用于 AGENTS.md）
        recurrence_threshold: 重复次数阈值
        dry_run: 是否只模拟运行
    
    Returns:
        进化周期结果（只包含已执行动作和验证结果）
    """
    now = int(time.time())
    
    result = {
        "generated_at": now,
        "dry_run": dry_run,
        "scanned_count": 0,
        "rules_generated": 0,
        "rules_adopted": 0,
        "rules_verified": 0,
        "details": [],
        "evidence_only": True,  # 新协议：只输出证据
    }
    
    # 0. 扫描系统健康问题（guardian 崩溃 / 工具中断未回复）
    for finding in scan_system_health(Path("/Users/hangzhou/openclaw-health-monitor")):
        problem_code = str(finding.get("problem_code") or "")
        learning_key = str(finding.get("learning_key") or f"health-{problem_code}")
        evidence = finding.get("evidence") or {}
        rule = generate_candidate_rule(problem_code=problem_code, learning_key=learning_key, evidence=evidence, now=now)
        result["scanned_count"] += 1
        if rule:
            result["rules_generated"] += 1
            adopt_result = adopt_rule(rule, base_dir, workspace_dir, dry_run=dry_run)
            if adopt_result.get("status") in ["adopted", "dry_run", "already_exists"]:
                result["rules_adopted"] += 1
                if not dry_run:
                    try:
                        store.record_self_evolution_event(
                            learning_key=learning_key,
                            event_type="recorded",
                            problem_code=problem_code,
                            actor="auto_evolution",
                            details={"title": problem_code, "summary": json.dumps(evidence, ensure_ascii=False)},
                        )
                    except Exception:
                        pass
            result["details"].append({
                "learning_key": learning_key,
                "problem_code": problem_code,
                "status": adopt_result.get("status"),
                "rule_id": rule.get("rule_id"),
                "system_health": True,
            })

    # 1. 扫描重复问题
    projections = store.list_self_evolution_projections(limit=500)
    
    for item in projections:
        learning_key = str(item.get("learning_key") or "")
        problem_code = str(item.get("problem_code") or "")
        current_state = str(item.get("current_state") or "")
        recurrence_count = int(item.get("recurrence_count") or 0)
        
        # 处理重复次数超过阈值的问题（recorded 或 reopened 状态）
        if current_state not in ["recorded", "reopened"]:
            continue
        if recurrence_count < recurrence_threshold:
            continue
        
        result["scanned_count"] += 1
        
        # 2. 生成候选规则
        rule = generate_candidate_rule(
            problem_code=problem_code,
            learning_key=learning_key,
            evidence=item,
            now=now,
        )
        
        if not rule:
            result["details"].append({
                "learning_key": learning_key,
                "problem_code": problem_code,
                "status": "no_template",
                "recurrence_count": recurrence_count,
            })
            continue
        
        result["rules_generated"] += 1
        
        # 3. 采纳规则
        adopt_result = adopt_rule(rule, base_dir, workspace_dir, dry_run=dry_run)
        
        if adopt_result.get("status") in ["adopted", "dry_run"]:
            result["rules_adopted"] += 1
            
            # 更新学习记录状态
            if not dry_run:
                store.record_self_evolution_event(
                    learning_key=learning_key,
                    event_type="adopted",
                    problem_code=problem_code,
                    actor="auto_evolution",
                    details={
                        "rule_id": rule.get("rule_id"),
                        "rule_type": rule.get("rule_type"),
                        "rule_target": rule.get("rule_target"),
                    },
                )
        
        result["details"].append({
            "learning_key": learning_key,
            "problem_code": problem_code,
            "status": adopt_result.get("status"),
            "rule_id": rule.get("rule_id"),
            "recurrence_count": recurrence_count,
        })
    
    # 4. 验证已采纳规则的效果
    if not dry_run:
        # 获取最近采纳的规则
        recent_rules = [
            item for item in projections
            if str(item.get("current_state") or "") == "adopted"
        ]
        
        for item in recent_rules[:5]:  # 最多验证 5 个
            verify_result = verify_rule_effectiveness(
                store,
                rule={
                    "learning_key": item.get("learning_key"),
                    "problem_code": item.get("problem_code"),
                    "adopted_at": int(item.get("updated_at") or 0),
                },
            )
            
            if verify_result.get("is_effective"):
                result["rules_verified"] += 1
                # 标记为已验证
                store.record_self_evolution_event(
                    learning_key=item.get("learning_key"),
                    event_type="verified",
                    problem_code=item.get("problem_code"),
                    actor="auto_evolution",
                    details=verify_result,
                )
    
    # 5. 更新最后运行时间
    if not dry_run:
        store.save_runtime_value("auto_evolution_last_cycle_at", now)
    
    # 6. 定期清理（每周一次）
    last_cleanup = store.load_runtime_value("auto_evolution_last_cleanup_at") or 0
    cleanup_interval = 7 * 24 * 3600  # 7 天
    
    if now - last_cleanup > cleanup_interval and workspace_dir and not dry_run:
        # 清理 LEARNINGS.md
        learnings_result = cleanup_learnings_archive(workspace_dir)
        result["learnings_cleanup"] = learnings_result
        
        # 清理 AGENTS.md 中的 AUTO- 约束
        agents_result = cleanup_agents_auto_constraints(workspace_dir)
        result["agents_cleanup"] = agents_result
        
        store.save_runtime_value("auto_evolution_last_cleanup_at", now)
    
    return result


# ============================================================================
# CLI 入口
# ============================================================================

if __name__ == "__main__":
    import argparse
    from pathlib import Path
    from state_store import MonitorStateStore
    
    parser = argparse.ArgumentParser(description="自动进化系统")
    parser.add_argument("--base-dir", default="/Users/hangzhou/openclaw-health-monitor", help="基础目录")
    parser.add_argument("--workspace", default="/Users/hangzhou/.openclaw/workspace-xiaoyi", help="工作区目录")
    parser.add_argument("--recurrence-threshold", type=int, default=5, help="重复次数阈值")
    parser.add_argument("--dry-run", action="store_true", help="只模拟运行")
    parser.add_argument("--report", action="store_true", help="生成报告")
    
    args = parser.parse_args()
    
    base_dir = Path(args.base_dir)
    workspace_dir = Path(args.workspace)
    store = MonitorStateStore(base_dir)
    
    if args.report:
        # 生成报告
        from learning_recorder import generate_daily_evolution_report, render_learnings_markdown
        
        report = generate_daily_evolution_report(store)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # 运行进化周期
        result = run_evolution_cycle(
            store,
            base_dir,
            workspace_dir,
            recurrence_threshold=args.recurrence_threshold,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))