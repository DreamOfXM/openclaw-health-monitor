#!/usr/bin/env python3
"""
OpenClaw 健康监控仪表盘
Web 界面展示系统状态和健康信息
"""

import os
import sys
import json
import time
import socket
import re
import subprocess
import threading
import resource
import html
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request, redirect

from typing import Any, Optional

from monitor_config import load_config as load_shared_config, save_local_config_value, sanitize_config_for_ui
from promotion_controller import PromotionController
from snapshot_manager import SnapshotManager
from state_store import MonitorStateStore

BASE_DIR = Path(__file__).parent
OPENCLAW_HOME = Path.home() / ".openclaw"
CHANGE_LOG_DIR = BASE_DIR / "change-logs"
CONFIG_FILE = BASE_DIR / "config.conf"
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.conf"
STORE = MonitorStateStore(BASE_DIR)
SNAPSHOTS = SnapshotManager(BASE_DIR, OPENCLAW_HOME)
GUARDIAN_PID_FILE = BASE_DIR / "logs" / "guardian.pid"
DESKTOP_RUNTIME = BASE_DIR / "desktop_runtime.sh"
OFFICIAL_MANAGER = BASE_DIR / "manage_official_openclaw.sh"


def raise_nofile_limit(target: int = 65536) -> None:
    """Best-effort bump of RLIMIT_NOFILE for the dashboard server."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = min(max(soft, target), hard)
        if desired > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except Exception:
        pass


def get_change_log_path() -> Path:
    """获取今天的日志文件路径"""
    today = datetime.now().strftime("%Y-%m-%d")
    return CHANGE_LOG_DIR / f"{today}.json"


def record_change(change_type: str, message: str, details: Optional[dict] = None):
    """记录变更"""
    STORE.record_change(change_type, message, details)
    log_file = get_change_log_path()
    CHANGE_LOG_DIR.mkdir(exist_ok=True)
    
    logs = []
    if log_file.exists():
        with open(log_file) as f:
            logs = json.load(f)
    
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": change_type,
        "message": message,
        "details": details or {}
    }
    logs.append(entry)
    
    with open(log_file, "w") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


def get_recent_changes(days: int = 7) -> list:
    """获取最近变更"""
    db_changes = STORE.list_recent_changes(days=days, limit=100)
    if db_changes:
        return list(reversed(db_changes))

    all_changes = []
    today = datetime.now()
    
    for i in range(days):
        date = today.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        log_file = CHANGE_LOG_DIR / f"{date_str}.json"
        
        if log_file.exists():
            with open(log_file) as f:
                logs = json.load(f)
                for entry in logs:
                    entry["date"] = date_str
                    all_changes.append(entry)
    
    return all_changes[-100:]  # 最近100条


def get_recent_anomalies(limit: int = 8, days: int = 7) -> list:
    """Return recent anomaly and pipeline-related events for quick triage."""
    changes = get_recent_changes(days)
    anomalies = [item for item in reversed(changes) if item.get("type") in {"anomaly", "pipeline"}]
    return anomalies[:limit]


def build_incident_summary(events: list[dict]) -> dict:
    """Build an operator-friendly summary from recent events."""
    summary = {
        "headline": "最近未发现明显异常",
        "status": "ok",
        "focus": "继续观察即可",
        "action": "暂无需要立即处理的动作",
        "last_stage": "-",
        "last_question": "-",
    }
    if not events:
        return summary

    latest = events[0]
    latest_details = latest.get("details", {}) or {}
    summary["last_stage"] = latest_details.get("marker", "-")
    summary["last_question"] = latest_details.get("question", "-")

    anomaly = next((item for item in events if item.get("type") == "anomaly"), None)
    pipeline = next((item for item in events if item.get("type") == "pipeline"), None)
    if pipeline:
        summary["last_stage"] = (pipeline.get("details", {}) or {}).get("marker", summary["last_stage"])

    if not anomaly:
        summary["headline"] = "最近有阶段进度，未见异常告警"
        summary["status"] = "watch"
        summary["focus"] = f"最后进度阶段: {summary['last_stage']}"
        summary["action"] = "如果长时间停留在同一阶段，再检查 Gateway 日志"
        return summary

    details = anomaly.get("details", {}) or {}
    question = details.get("question") or "未知问题"
    duration = details.get("duration")
    marker = details.get("marker")
    message = anomaly.get("message", "检测到任务异常")
    summary["last_question"] = question

    if "没有可见回复" in message:
        summary["headline"] = "检测到任务完成但用户没有收到回复"
        summary["status"] = "error"
        summary["focus"] = f"问题: {question}"
        summary["action"] = "优先检查网关回包链路和 replies/queuedFinal 日志"
    elif "阶段长时间无进展" in message:
        summary["headline"] = "检测到任务卡在某个阶段"
        summary["status"] = "error"
        summary["focus"] = f"阶段: {marker or '-'} | 问题: {question}"
        summary["action"] = "优先检查该阶段前后的 PIPELINE_PROGRESS 和子代理执行日志"
    elif "长时间无最终结果" in message:
        summary["headline"] = "检测到任务长时间没有最终结果"
        summary["status"] = "error"
        summary["focus"] = f"问题: {question}"
        summary["action"] = "优先检查 dispatching/dispatch complete 是否成对出现"
    elif "WebSocket 异常关闭" in message:
        summary["headline"] = "检测到 Gateway WebSocket 异常关闭"
        summary["status"] = "error"
        summary["focus"] = "网关链路异常中断"
        summary["action"] = "优先检查 Gateway 进程状态和最近错误日志"
    else:
        summary["headline"] = message
        summary["status"] = "watch"
        summary["focus"] = f"问题: {question}"
        summary["action"] = "建议先查看最近异常详情和运行日志"

    if duration is not None:
        summary["focus"] += f" | 耗时: {duration}秒"
    if marker and summary["last_stage"] == "-":
        summary["last_stage"] = marker
    return summary


def get_task_registry_payload(limit: int = 8) -> dict:
    """Return managed task registry data for dashboard/API consumers."""
    config = load_config()
    enabled = bool(config.get("ENABLE_TASK_REGISTRY", True))
    selected_env = env_spec(active_env_id(config), config)
    env_id = selected_env["id"]
    tasks = STORE.list_tasks(limit=limit) if enabled else []

    def normalize_question(text: str) -> str:
        raw = (text or "").strip()
        lower = raw.lower()
        if not raw:
            return "未知任务"
        if "dispatching to agent" in lower:
            return "未知任务"
        if "received message from " in lower:
            return "未知任务"
        if " dm from " in lower or "feishu[default] dm from " in lower:
            if ": " in raw:
                raw = raw.split(": ", 1)[1]
            raw = raw.split('","_meta"', 1)[0]
            raw = raw.strip()
        return raw or "未知任务"

    def summarize_task(task: dict | None) -> dict | None:
        if not task:
            return None
        ts = int(task.get("last_progress_at") or 0)
        latest_receipt = task.get("latest_receipt") or {}
        timeline = STORE.list_task_events(task["task_id"], limit=6)
        control = STORE.derive_task_control_state(task["task_id"])
        control_actions = STORE.list_task_control_actions(
            task_id=task["task_id"],
            statuses=["pending", "sent", "blocked"],
            limit=5,
        )
        question = normalize_question(task.get("question", ""))
        last_user_message = normalize_question(task.get("last_user_message", ""))
        if question == "未知任务":
            fallback = STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
            question = normalize_question(fallback)
        if last_user_message == "未知任务":
            fallback = STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
            last_user_message = normalize_question(fallback)
        return {
            **task,
            "question": question,
            "last_user_message": last_user_message,
            "last_progress_label": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-",
            "receipt_summary": {
                "agent": latest_receipt.get("agent", "-"),
                "phase": latest_receipt.get("phase", "-"),
                "action": latest_receipt.get("action", "-"),
                "evidence": latest_receipt.get("evidence", "-"),
            },
            "control": control,
            "control_actions": control_actions,
            "timeline": [
                {
                    "event_type": item.get("event_type", ""),
                    "created_at": item.get("created_at", 0),
                    "created_label": datetime.fromtimestamp(int(item.get("created_at", 0))).strftime("%m-%d %H:%M:%S")
                    if item.get("created_at")
                    else "-",
                    "payload": item.get("payload", {}),
                }
                for item in reversed(timeline)
            ],
        }

    for task in tasks:
        task["question"] = normalize_question(task.get("question", ""))
        task["last_user_message"] = normalize_question(task.get("last_user_message", ""))
        if task["question"] == "未知任务":
            task["question"] = normalize_question(STORE.get_task_question_candidate(task["task_id"]) or "未知任务")
        if task["last_user_message"] == "未知任务":
            task["last_user_message"] = normalize_question(
                STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
            )
        ts = int(task.get("last_progress_at") or 0)
        task["last_progress_label"] = (
            datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-"
        )
        task["control"] = STORE.derive_task_control_state(task["task_id"])
        task["control_actions"] = STORE.list_task_control_actions(
            task_id=task["task_id"],
            statuses=["pending", "sent", "blocked"],
            limit=5,
        )
    active = [task for task in tasks if task.get("status") in {"running", "blocked", "background"}]
    current = summarize_task(STORE.get_current_task(env_id=env_id)) if enabled else None
    summary = STORE.summarize_tasks(env_id=env_id) if enabled else {"total": 0}
    control_queue = (
        STORE.list_task_control_actions(env_id=env_id, statuses=["pending", "sent", "blocked"], limit=12)
        if enabled
        else []
    )
    session_resolution = (
        STORE.derive_session_resolution(str((current or {}).get("session_key") or ""))
        if enabled and (current or active or tasks)
        else {}
    )
    return {
        "enabled": enabled,
        "summary": summary,
        "control_queue": control_queue,
        "session_resolution": session_resolution,
        "current": current or (summarize_task(active[0]) if active else (summarize_task(tasks[0]) if tasks else None)),
        "tasks": tasks,
    }


def load_agent_catalog(spec: dict) -> dict[str, dict]:
    """Load agent display metadata from the environment config when available."""
    config_path = spec["home"] / "openclaw.json"
    catalog: dict[str, dict] = {}
    if not config_path.exists():
        return catalog
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return catalog
    for item in ((payload.get("agents") or {}).get("list") or []):
        agent_id = str(item.get("id") or "").strip()
        if not agent_id:
            continue
        identity = item.get("identity") or {}
        catalog[agent_id] = {
            "name": str(identity.get("name") or agent_id),
            "emoji": str(identity.get("emoji") or ""),
        }
    return catalog


def _extract_text_items(content: list[dict]) -> list[str]:
    texts: list[str] = []
    for item in content or []:
        if item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                texts.append(text)
    return texts


def _extract_task_hint_from_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if "[Subagent Task]:" in raw:
        chunk = raw.split("[Subagent Task]:", 1)[1].strip()
        for line in chunk.splitlines():
            line = line.strip()
            if line and not line.startswith("执行要求") and not line.startswith("主人原始需求"):
                return line[:120]
    if "主人需求：" in raw:
        chunk = raw.split("主人需求：", 1)[1].strip()
        first = chunk.splitlines()[0].strip()
        return first[:120]
    if "\ntask:" in raw:
        chunk = raw.split("\ntask:", 1)[1].strip()
        first = chunk.splitlines()[0].strip()
        return first[:120]
    if raw.startswith("task:"):
        return raw.split("task:", 1)[1].strip()[:120]
    return ""


def _summarize_session_message(entry: dict) -> tuple[str, str]:
    message = entry.get("message") or {}
    role = str(message.get("role") or "")
    content = message.get("content") or []

    for item in content:
        if item.get("type") != "toolCall":
            continue
        name = str(item.get("name") or "")
        args = item.get("arguments") or {}
        if name == "sessions_spawn":
            agent_id = args.get("agentId") or "?"
            label = str(args.get("label") or "").strip()
            suffix = f" · {label}" if label else ""
            return "正在派发", f"启动子代理 {agent_id}{suffix}"
        if name == "sessions_send":
            return "正在回传", "向上游回传结构化进度或回执"
        if name == "exec":
            return "正在执行", "执行命令或本地检查"

    if role == "toolResult":
        tool_name = str(message.get("toolName") or "")
        details = message.get("details") or {}
        status = str(details.get("status") or "").strip()
        if tool_name == "sessions_spawn":
            if status == "accepted":
                child = str(details.get("childSessionKey") or "").strip()
                return "子任务已启动", child or "下游子代理已接受任务"
            if status == "forbidden":
                return "派发受限", str(details.get("error") or "sessions_spawn 被拒绝")[:160]
        if tool_name == "sessions_send":
            if status == "forbidden":
                return "回执受限", str(details.get("error") or "sessions_send 被拒绝")[:160]
            return "已回传", status or "结构化消息已回传"
        if tool_name == "exec":
            exit_code = details.get("exitCode")
            return "命令完成", f"exit={exit_code}" if exit_code is not None else "命令执行完成"

    texts = _extract_text_items(content)
    if texts:
        text = texts[-1]
        if text == "ANNOUNCE_SKIP":
            return "等待下游", "当前阶段已继续下发，等待后续回执"
        if text == "NO_REPLY":
            return "静默等待", "收到内部更新，但当前无需对外回复"
        if "OpenClaw runtime context" in text:
            return "收到内部结果", "子代理或运行时回传了内部完成事件"
        snippet = re.sub(r"\s+", " ", text)
        return "正在处理", snippet[:160]

    if role == "user":
        for text in texts:
            hint = _extract_task_hint_from_text(text)
            if hint:
                return "收到任务", hint
    return "活动中", "最近会话有更新"


def summarize_agent_session(session_path: Path, agent_id: str, meta: dict) -> dict | None:
    """Summarize the latest visible activity from an agent session file."""
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()[-24:]
    except Exception:
        return None

    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    if not entries:
        return None

    task_hint = ""
    for entry in reversed(entries):
        message = entry.get("message") or {}
        content = message.get("content") or []
        for text in _extract_text_items(content):
            task_hint = _extract_task_hint_from_text(text)
            if task_hint:
                break
        if task_hint:
            break

    state_label = "活动中"
    detail = "最近会话有更新"
    for entry in reversed(entries):
        state_label, detail = _summarize_session_message(entry)
        if detail:
            break

    updated_at = int(session_path.stat().st_mtime)
    display_name = meta.get("name") or agent_id
    emoji = meta.get("emoji") or ""
    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "emoji": emoji,
        "session_file": session_path.name,
        "updated_at": updated_at,
        "updated_label": datetime.fromtimestamp(updated_at).strftime("%m-%d %H:%M:%S"),
        "state_label": state_label,
        "detail": detail,
        "task_hint": task_hint or "-",
    }


def get_active_agent_activity(spec: dict, config: dict) -> dict:
    """Return recent active agent sessions for the current environment."""
    agents_dir = spec["home"] / "agents"
    if not agents_dir.exists():
        return {"summary": {"active_agents": 0, "recent_sessions": 0}, "agents": []}

    lookback_seconds = int(config.get("AGENT_ACTIVITY_LOOKBACK_SECONDS", 1800))
    scan_limit = int(config.get("AGENT_ACTIVITY_SCAN_LIMIT", 12))
    cutoff = time.time() - max(60, lookback_seconds)
    catalog = load_agent_catalog(spec)
    agent_entries: list[dict] = []

    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        sessions_dir = agent_dir / "sessions"
        if not sessions_dir.exists():
            continue
        candidates = [
            path
            for path in sessions_dir.glob("*.jsonl")
            if path.is_file() and path.stat().st_mtime >= cutoff
        ]
        if not candidates:
            continue
        latest = max(candidates, key=lambda item: item.stat().st_mtime)
        summary = summarize_agent_session(latest, agent_dir.name, catalog.get(agent_dir.name, {}))
        if summary:
            agent_entries.append(summary)

    agent_entries.sort(key=lambda item: item["updated_at"], reverse=True)
    agent_entries = agent_entries[:scan_limit]
    return {
        "summary": {
            "active_agents": len({item["agent_id"] for item in agent_entries}),
            "recent_sessions": len(agent_entries),
            "lookback_seconds": lookback_seconds,
        },
        "agents": agent_entries,
    }


def format_change_details(change: dict) -> str:
    """Render compact change details for the dashboard."""
    details = change.get("details", {}) or {}
    change_type = change.get("type")
    if change_type == "pipeline":
        return f"阶段: {details.get('marker', '-')} | 时间: {details.get('timestamp', '-')}"
    if change_type == "anomaly":
        question = details.get("question", "-")
        duration = details.get("duration", "-")
        marker = details.get("marker")
        marker_text = f" | 阶段: {marker}" if marker else ""
        return f"问题: {question} | 耗时: {duration}秒{marker_text} | 时间: {details.get('timestamp', '-')}"
    if change_type == "restart":
        return f"PID: {details.get('old_pid', '-')} -> {details.get('new_pid', '-')}"
    if change_type == "recover":
        return f"快照: {details.get('snapshot', '-')}"
    if change_type == "snapshot":
        return f"快照: {details.get('snapshot', '-')}"
    if change_type == "version":
        if details.get("from") and details.get("to"):
            return f"{details.get('from')} -> {details.get('to')}"
        return details.get("commit", "-")
    return json.dumps(details, ensure_ascii=False) if details else "-"


def parse_mem_value_to_gb(raw: str) -> float:
    """Convert macOS memory strings like 2735M/32G/512K to GB."""
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    match = re.match(r"([\d.]+)\s*([KMGT])", raw, re.IGNORECASE)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2).upper()
    factors = {
        "K": 1 / (1024 * 1024),
        "M": 1 / 1024,
        "G": 1,
        "T": 1024,
    }
    return round(value * factors.get(unit, 0), 2)


def summarize_memory_usage(metrics: dict, top_processes: list[dict]) -> dict:
    """Build a memory attribution summary for operators."""
    mem_used = float(metrics.get("mem_used", 0) or 0)
    mem_total = float(metrics.get("mem_total", 0) or 0)
    wired = float(metrics.get("mem_wired", 0) or 0)
    compressed = float(metrics.get("mem_compressed", 0) or 0)
    top_process_sum = round(sum(float(p.get("mem_mb", 0) or 0) for p in top_processes) / 1024, 2)
    unattributed = round(max(mem_used - top_process_sum, 0), 2)
    system_other = round(max(unattributed - wired - compressed, 0), 2)
    process_coverage = round((top_process_sum / mem_used * 100), 1) if mem_used > 0 else 0.0

    items = [
        {"name": "Top 15 进程", "value_gb": top_process_sum, "kind": "process", "note": f"覆盖已用内存 {process_coverage}%"},
    ]
    if wired > 0:
        items.append({"name": "Kernel / Wired", "value_gb": wired, "kind": "system", "note": "内核与驱动占用"})
    if compressed > 0:
        items.append({"name": "Compressed", "value_gb": compressed, "kind": "system", "note": "压缩内存"})
    if system_other > 0:
        items.append({"name": "Other System", "value_gb": system_other, "kind": "system", "note": "缓存、共享内存等未归属项"})

    return {
        "top15_gb": top_process_sum,
        "unattributed_gb": unattributed,
        "process_coverage_percent": process_coverage,
        "items": items,
        "summary": f"{top_process_sum:.1f}G 进程 + {unattributed:.1f}G 系统/缓存 = {mem_used:.1f}G",
        "note": "系统项包含内核、压缩内存和无法直接归属到单进程的缓存/共享内存。",
        "total_gb": mem_total,
    }


def list_snapshots(limit: int = 20) -> list[dict]:
    """列出最近的配置快照。"""
    snapshots = []
    for snapshot_dir in SNAPSHOTS.list_snapshots()[:limit]:
        manifest_file = snapshot_dir / "manifest.json"
        item = {
            "name": snapshot_dir.name,
            "created_at": "",
            "label": "",
            "file_count": 0,
        }
        if manifest_file.exists():
            try:
                with open(manifest_file) as handle:
                    manifest = json.load(handle)
                item["created_at"] = manifest.get("created_at", "")
                item["label"] = manifest.get("label", "")
                item["file_count"] = len(manifest.get("files", []))
            except Exception:
                pass
        snapshots.append(item)
    return snapshots


def load_config() -> dict:
    """加载配置"""
    return load_shared_config(BASE_DIR)


def active_env_id(config: Optional[dict] = None) -> str:
    if config is None:
        cfg = load_config()
    else:
        cfg = config
    selected = str(cfg.get("ACTIVE_OPENCLAW_ENV", "primary")).strip() or "primary"
    return selected if selected in {"primary", "official"} else "primary"


def get_env_specs(config: Optional[dict] = None) -> dict[str, dict]:
    cfg = config or load_config()
    primary_port = int(cfg.get("GATEWAY_PORT", 18789))
    official_port = int(cfg.get("OPENCLAW_OFFICIAL_PORT", 19001))
    primary_home = Path(str(cfg.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))))
    primary_code = Path(str(cfg.get("OPENCLAW_CODE", str(Path.home() / "openclaw-workspace" / "openclaw"))))
    official_state = Path(str(cfg.get("OPENCLAW_OFFICIAL_STATE", str(Path.home() / ".openclaw-official"))))
    official_code = Path(str(cfg.get("OPENCLAW_OFFICIAL_CODE", str(Path.home() / "openclaw-workspace" / "openclaw-official"))))
    return {
        "primary": {
            "id": "primary",
            "name": "当前主用版",
            "description": "当前日常使用的 OpenClaw 环境",
            "home": primary_home,
            "code": primary_code,
            "port": primary_port,
            "kind": "primary",
        },
        "official": {
            "id": "official",
            "name": "官方验证版",
            "description": "用于并行验证官方最新版的隔离环境",
            "home": official_state,
            "code": official_code,
            "port": official_port,
            "kind": "official",
        },
    }


def create_config_snapshots(label: str) -> list[Path]:
    snapshots: list[Path] = []
    cfg = load_config()
    for env_id, spec in get_env_specs(cfg).items():
        snapshot_dir = SnapshotManager(BASE_DIR, Path(spec["home"])).create_snapshot(f"{label}-{env_id}")
        if snapshot_dir is not None:
            snapshots.append(snapshot_dir)
    return snapshots


def env_spec(env_id: Optional[str], config: Optional[dict] = None) -> dict:
    specs = get_env_specs(config)
    return specs.get(env_id or active_env_id(config), specs["primary"])


def env_gateway_log(spec: dict) -> Path:
    return Path(spec["home"]) / "logs" / "gateway.log"


def env_gateway_err_log(spec: dict) -> Path:
    return Path(spec["home"]) / "logs" / "gateway.err.log"


def env_dashboard_url(spec: dict) -> str:
    token = ""
    config_path = Path(spec["home"]) / "openclaw.json"
    try:
        token = (
            json.loads(config_path.read_text(encoding="utf-8"))
            .get("gateway", {})
            .get("auth", {})
            .get("token", "")
        )
    except Exception:
        token = ""
    base = f"http://127.0.0.1:{spec['port']}/"
    gateway_url = f"ws://127.0.0.1:{spec['port']}"
    params = []
    if token:
        params.append(f"token={urllib.parse.quote(token)}")
    params.append(f"gatewayUrl={urllib.parse.quote(gateway_url, safe='')}")
    return f"{base}#{'&'.join(params)}" if params else base


def env_has_control_ui_assets(spec: dict) -> bool:
    config_path = Path(spec["home"]) / "openclaw.json"
    root_path: Optional[str] = None
    try:
        root_path = (
            json.loads(config_path.read_text(encoding="utf-8"))
            .get("gateway", {})
            .get("controlUi", {})
            .get("root")
        )
    except Exception:
        root_path = None

    if root_path:
        root = Path(root_path).expanduser()
        if not root.is_absolute():
            root = Path(spec["code"]) / root
    else:
        root = Path(spec["code"]) / "dist" / "control-ui"
    return (root / "index.html").exists()


def env_open_link(spec: dict) -> str:
    return f"/open-dashboard/{spec['id']}"


def env_token_prefix(spec: dict) -> str:
    config_path = Path(spec["home"]) / "openclaw.json"
    try:
        token = (
            json.loads(config_path.read_text(encoding="utf-8"))
            .get("gateway", {})
            .get("auth", {})
            .get("token", "")
        )
    except Exception:
        token = ""
    token = str(token or "")
    return token[:8] if token else ""


def detect_environment_inconsistencies(environments: list[dict], active_env: str) -> list[dict]:
    issues: list[dict] = []
    running = [item for item in environments if item.get("running")]
    if len(running) > 1:
        issues.append(
            {
                "severity": "error",
                "code": "dual_listener",
                "title": "检测到双环境同时监听",
                "detail": "single-active-environment 约束被破坏，当前存在两个 gateway listener。",
            }
        )
    for item in environments:
        if item.get("active") and not item.get("running"):
            issues.append(
                {
                    "severity": "warning",
                    "code": "active_env_not_running",
                    "title": f"{item.get('id')} 已激活但未监听",
                    "detail": "ACTIVE_OPENCLAW_ENV 与实际 listener 不一致。",
                }
            )
        if active_env == "official" and item.get("id") == "primary" and item.get("running"):
            issues.append(
                {
                    "severity": "error",
                    "code": "primary_running_while_official_active",
                    "title": "Official 激活时 Primary 仍在监听",
                    "detail": "这会导致状态漂移和消息误投。",
                }
            )
        if active_env == "primary" and item.get("id") == "official" and item.get("running"):
            issues.append(
                {
                    "severity": "error",
                    "code": "official_running_while_primary_active",
                    "title": "Primary 激活时 Official 仍在监听",
                    "detail": "这会破坏单活环境运行基线。",
                }
            )
    dedup: dict[str, dict] = {}
    for issue in issues:
        dedup[str(issue.get("code") or len(dedup))] = issue
    return list(dedup.values())


def build_model_failure_summary(errors: list[dict], recent_events: list[dict]) -> dict:
    categories = {
        "auth_failure": ["401", "oauth", "auth", "re-authenticate", "token refresh failed"],
        "empty_response": ["empty response", "空响应", "no content", "response was empty"],
        "fallback_exhausted": ["all models failed", "fallback exhausted", "model_pool_failed"],
        "delivery_failed": ["websocket", "ws closed", "delivery", "连接断开", "1006"],
        "control_followup_failed": ["control_followup_failed", "守护控制面催办失败", "followup failed"],
        "no_visible_reply": ["no visible reply", "无回复", "没回复"],
    }
    observed: dict[str, dict[str, Any]] = {}
    for item in recent_events or []:
        text = " ".join(
            [
                str(item.get("message") or ""),
                json.dumps(item.get("details") or {}, ensure_ascii=False),
            ]
        ).lower()
        for category, needles in categories.items():
            if any(needle in text for needle in needles):
                observed.setdefault(category, {"count": 0, "sample": str(item.get("message") or "")})
                observed[category]["count"] += 1
    for item in errors or []:
        text = str(item.get("message") or "").lower()
        for category, needles in categories.items():
            if any(needle in text for needle in needles):
                observed.setdefault(category, {"count": 0, "sample": str(item.get("message") or "")})
                observed[category]["count"] += 1
    priority = [
        "auth_failure",
        "fallback_exhausted",
        "delivery_failed",
        "control_followup_failed",
        "empty_response",
        "no_visible_reply",
    ]
    primary = next((key for key in priority if key in observed), "")
    labels = {
        "auth_failure": "认证失败",
        "empty_response": "空响应",
        "fallback_exhausted": "回退耗尽",
        "delivery_failed": "交付失败",
        "control_followup_failed": "控制追问失败",
        "no_visible_reply": "无可见回复",
    }
    return {
        "headline": labels.get(primary, "最近没有明显模型失败"),
        "primary_type": primary or "ok",
        "items": [
            {
                "type": key,
                "label": labels.get(key, key),
                "count": int(value.get("count") or 0),
                "sample": str(value.get("sample") or ""),
            }
            for key, value in observed.items()
        ],
    }


def build_context_lifecycle_readiness(config: Optional[dict] = None) -> dict:
    cfg = config or load_config()
    spec = env_spec(active_env_id(cfg), cfg)
    payload = {"ready": False, "checks": [], "target_env": spec["id"]}
    config_path = Path(spec["home"]) / "openclaw.json"
    openclaw_payload: dict[str, Any] = {}
    try:
        openclaw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        openclaw_payload = {}
    session_cfg = openclaw_payload.get("session") or {}
    memory_cfg = openclaw_payload.get("memory") or {}
    checks = [
        {
            "name": "memory_flush",
            "ok": bool(memory_cfg.get("memoryFlush") or session_cfg.get("memoryFlush") or session_cfg.get("memoryFlushAt")),
            "detail": "memory flush 已配置" if (memory_cfg.get("memoryFlush") or session_cfg.get("memoryFlush") or session_cfg.get("memoryFlushAt")) else "缺少 memory flush 策略",
        },
        {
            "name": "context_pruning",
            "ok": bool(memory_cfg.get("contextPruning") or session_cfg.get("contextPruning")),
            "detail": "context pruning 已配置" if (memory_cfg.get("contextPruning") or session_cfg.get("contextPruning")) else "缺少 context pruning 策略",
        },
        {
            "name": "daily_or_idle_reset",
            "ok": bool(session_cfg.get("dailyReset") or session_cfg.get("idleReset") or session_cfg.get("resetOnExit")),
            "detail": "存在 daily/idle/reset 策略" if (session_cfg.get("dailyReset") or session_cfg.get("idleReset") or session_cfg.get("resetOnExit")) else "缺少 daily / idle reset 策略",
        },
        {
            "name": "session_maintenance",
            "ok": bool(session_cfg.get("maintenance") or session_cfg.get("maintenanceBudget") or session_cfg.get("sessionMaintenance")),
            "detail": "session maintenance 已配置" if (session_cfg.get("maintenance") or session_cfg.get("maintenanceBudget") or session_cfg.get("sessionMaintenance")) else "缺少 session maintenance 策略",
        },
    ]
    payload["checks"] = checks
    payload["ready"] = all(bool(item.get("ok")) for item in checks)
    payload["headline"] = "长期运行基线已达标" if payload["ready"] else "长期运行基线未达标"
    return payload


def build_shared_state_snapshot(config: Optional[dict] = None) -> dict:
    cfg = config or load_config()
    selected_env = env_spec(active_env_id(cfg), cfg)
    environments = list_openclaw_environments(cfg)
    task_registry = get_task_registry_payload(limit=20)
    control_plane = get_control_plane_overview(selected_env["id"])
    learning_center = get_learning_center_payload(limit=20)
    metrics = get_system_metrics()
    recent_events = get_recent_anomalies(limit=12, days=7)
    return {
        "generated_at": int(time.time()),
        "active_environment": selected_env["id"],
        "environment_integrity": detect_environment_inconsistencies(environments, selected_env["id"]),
        "current_task_facts": {
            "summary": task_registry.get("summary") or {},
            "current": task_registry.get("current"),
            "session_resolution": task_registry.get("session_resolution") or {},
        },
        "task_registry_snapshot": task_registry,
        "control_action_queue": task_registry.get("control_queue") or [],
        "runtime_health": {
            "metrics": metrics,
            "gateway_healthy": check_gateway_health_for_env(selected_env),
            "gateway_process": get_gateway_process_for_env(selected_env),
            "guardian_process": get_guardian_process_info(),
            "recent_events": recent_events,
        },
        "learning_backlog": {
            "summary": learning_center.get("summary") or {},
            "suggestions": learning_center.get("suggestions") or [],
            "learnings": learning_center.get("learnings") or [],
            "reflections": learning_center.get("reflections") or [],
        },
        "control_plane": control_plane,
    }


def read_git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def read_git_target_head(repo: Path, ref: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", ref],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def check_gateway_health_for_env(spec: dict) -> bool:
    if spec.get("id") == "primary":
        return check_gateway_health()
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--noproxy",
                "*",
                "-fsS",
                f"http://127.0.0.1:{spec['port']}/health",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout or "{}")
        return bool(payload.get("ok"))
    except Exception:
        return False


def get_gateway_process_for_env(spec: dict) -> Optional[dict]:
    pid = get_listener_pid(int(spec["port"]))
    if pid is None:
        return None
    return get_process_info_by_pid(pid)


def list_openclaw_environments(config: Optional[dict] = None) -> list[dict]:
    cfg = config or load_config()
    current = active_env_id(cfg)
    official_ref = str(cfg.get("OPENCLAW_OFFICIAL_REF", "origin/main"))
    official_schedule_plist = Path.home() / "Library" / "LaunchAgents" / "ai.openclaw.official-update.plist"
    environments = []
    for item in get_env_specs(cfg).values():
        listener_pid = get_listener_pid(int(item["port"]))
        running = listener_pid is not None
        active = item["id"] == current
        git_head = read_git_head(Path(item["code"]))
        target_head = read_git_target_head(Path(item["code"]), official_ref) if item["id"] == "official" else git_head
        control_ui_ready = env_has_control_ui_assets(item)
        environments.append(
            {
                "id": item["id"],
                "name": item["name"],
                "description": item["description"],
                "port": item["port"],
                "code": str(item["code"]),
                "home": str(item["home"]),
                "git_head": git_head,
                "target_head": target_head,
                "listener_pid": int(listener_pid) if listener_pid is not None else None,
                "token_prefix": env_token_prefix(item),
                "running": running,
                "healthy": check_gateway_health_for_env(item) if running else False,
                "control_ui_ready": control_ui_ready,
                "dashboard_url": env_dashboard_url(item),
                "dashboard_open_link": env_open_link(item) if active and running and control_ui_ready else "",
                "active": active,
                "auto_update_enabled": official_schedule_plist.exists() if item["id"] == "official" else False,
                "update_hour": cfg.get("OPENCLAW_OFFICIAL_UPDATE_HOUR", 4) if item["id"] == "official" else None,
                "update_minute": cfg.get("OPENCLAW_OFFICIAL_UPDATE_MINUTE", 30) if item["id"] == "official" else None,
            }
        )
    return environments


def build_environment_promotion_summary(environments: list[dict], task_registry: dict) -> dict:
    primary = next((item for item in environments if item["id"] == "primary"), {})
    official = next((item for item in environments if item["id"] == "official"), {})
    current = task_registry.get("current") or {}
    current_control = current.get("control") or {}
    blocked_tasks = int((task_registry.get("summary") or {}).get("blocked", 0) or 0)

    summary = {
        "candidate_env": official.get("id"),
        "safe_to_promote": False,
        "headline": "官方验证版尚未达到切换条件",
        "reasons": [],
        "recommended_action": "先保持当前主用版，继续验证官方版。",
    }

    if not official:
        summary["reasons"].append("未找到官方验证版环境。")
        return summary
    if not official.get("running"):
        summary["reasons"].append("官方验证版未运行。")
    if official.get("running") and not official.get("healthy"):
        summary["reasons"].append("官方验证版未通过健康检查。")
    if blocked_tasks:
        summary["reasons"].append(f"当前存在 {blocked_tasks} 个阻塞任务。")
    if current and current_control.get("control_state") in {"blocked_unverified", "blocked_control_followup_failed", "dev_blocked", "test_blocked", "analysis_blocked"}:
        summary["reasons"].append("当前活动任务仍处于阻塞态。")

    if official.get("running") and official.get("healthy") and not summary["reasons"]:
        summary["safe_to_promote"] = True
        summary["headline"] = "官方验证版已满足切换条件"
        summary["recommended_action"] = "可将官方验证版切换为当前主用版。"
        if official.get("git_head") and primary.get("git_head") and official.get("git_head") != primary.get("git_head"):
            summary["reasons"].append(
                f"将从 {primary.get('git_head')} 切到 {official.get('git_head')}"
            )
    return summary


def get_learning_center_payload(limit: int = 10) -> dict:
    learnings = STORE.list_learnings(limit=limit)
    reflections = STORE.list_reflection_runs(limit=6)
    suggestions: list[dict[str, str]] = []
    for item in learnings[:5]:
        status = str(item.get("status") or "")
        occurrences = int(item.get("occurrences") or 0)
        category = str(item.get("category") or "misc")
        title = str(item.get("title") or "未命名 learning")
        if status == "pending":
            action = "继续观察并收集重复证据"
        elif status == "reviewed":
            action = "可考虑提升为 contract / rule"
        else:
            action = "已升级，关注实际效果"
        suggestions.append(
            {
                "title": title,
                "category": category,
                "status": status,
                "occurrences": str(occurrences),
                "action": action,
            }
        )
    return {
        "summary": STORE.summarize_learnings(),
        "suggestions": suggestions,
        "learnings": learnings,
        "reflections": [
            {
                **item,
                "created_label": datetime.fromtimestamp(int(item.get("created_at") or 0)).strftime("%m-%d %H:%M:%S")
                if item.get("created_at")
                else "-",
            }
            for item in reflections
        ],
    }


def get_control_plane_overview(env_id: str) -> dict:
    return STORE.summarize_control_plane(env_id=env_id)


def backup_change_logs():
    """备份旧日志"""
    CHANGE_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    
    for f in CHANGE_LOG_DIR.glob("*.json"):
        if f.stem < today:
            backup_dir = CHANGE_LOG_DIR / "backups"
            backup_dir.mkdir(exist_ok=True)
            f.rename(backup_dir / f"{f.stem}.json")
OPENCLAW_CODE = Path.home() / "openclaw-workspace" / "openclaw"
GATEWAY_LOG = OPENCLAW_HOME / "logs" / "gateway.log"
GATEWAY_ERR_LOG = OPENCLAW_HOME / "logs" / "gateway.err.log"

app = Flask(__name__)

# ========== 数据收集函数 ==========

def get_process_info(name: str) -> Optional[dict]:
    """获取进程信息"""
    try:
        result = subprocess.run(
            f'ps aux | grep -i "{name}" | grep -v grep',
            shell=True, capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            parts = result.stdout.strip().split()
            return {
                "pid": int(parts[1]),
                "cpu": float(parts[2]),
                "mem": float(parts[3]),
                "cmd": " ".join(parts[10:])[:100]
            }
    except:
        pass
    return None


def get_process_info_by_pid(pid: int) -> Optional[dict]:
    """通过 PID 获取进程信息，避免模糊 grep 带来的误判。"""
    try:
        result = subprocess.run(
            f"ps -p {pid} -o pid=,%cpu=,%mem=,command=",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        line = result.stdout.strip()
        if not line:
            return None
        parts = line.split(None, 3)
        if len(parts) < 4:
            return None
        return {
            "pid": int(parts[0]),
            "cpu": float(parts[1]),
            "mem": float(parts[2]),
            "cmd": parts[3][:100],
        }
    except Exception:
        return None


def load_pid_file(pid_file: Path) -> Optional[int]:
    """读取 PID 文件并确认目标进程仍然存活。"""
    try:
        raw = pid_file.read_text().strip()
        if not raw:
            return None
        pid = int(raw)
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def get_guardian_process_info() -> Optional[dict]:
    """优先通过 guardian.pid 获取状态，找不到再回退到精确命令匹配。"""
    pid = load_pid_file(GUARDIAN_PID_FILE)
    if pid is not None:
        info = get_process_info_by_pid(pid)
        if info and "guardian.py" in info.get("cmd", ""):
            return info
    return get_process_info(r"[g]uardian\.py")


def get_listener_pid(port: int = 18789) -> Optional[int]:
    """返回监听指定端口的 PID。"""
    try:
        result = subprocess.run(
            f"lsof -ti tcp:{port} -sTCP:LISTEN",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            return int(result.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


def get_top_processes(limit: int = 15) -> list:
    """获取内存占用最高的进程"""
    processes = []
    try:
        result = subprocess.run(
            "ps aux -m | head -" + str(limit + 1),
            shell=True, capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")[1:]  # 跳过标题行
        for line in lines:
            parts = line.split()
            if len(parts) >= 11:
                try:
                    # RSS 是实际物理内存使用（KB）
                    rss_kb = int(parts[5])
                    rss_mb = rss_kb / 1024
                    processes.append({
                        "pid": int(parts[1]),
                        "user": parts[0],
                        "cpu": float(parts[2]),
                        "mem": float(parts[3]),
                        "mem_mb": rss_mb,
                        "cmd": " ".join(parts[10:])[:80]
                    })
                except:
                    pass
    except:
        pass
    return processes


def check_gateway_health() -> bool:
    """检查 Gateway 健康"""
    try:
        result = subprocess.run(
            "openclaw gateway health",
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = f"{result.stdout}\n{result.stderr}".lower()
        return result.returncode == 0 and "gateway target" not in output
    except Exception:
        return False


def get_system_metrics() -> dict:
    """获取系统指标"""
    cpu = 0.0
    mem_used = 0
    mem_total = 32
    mem_wired = 0.0
    mem_compressed = 0.0
    
    try:
        result = subprocess.run("top -l 1 -n 0", shell=True, capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\n"):
            if "CPU usage" in line:
                try:
                    user_match = re.search(r'([\d.]+)%\s*user', line)
                    sys_match = re.search(r'([\d.]+)%\s*sys', line)
                    if user_match:
                        cpu = float(user_match.group(1))
                    if sys_match:
                        cpu += float(sys_match.group(1))
                except:
                    pass
            if "PhysMem" in line:
                try:
                    used_match = re.search(r'([\d.]+[KMGT])\s*used', line)
                    unused_match = re.search(r'([\d.]+[KMGT])\s*unused', line)
                    wired_match = re.search(r'([\d.]+[KMGT])\s*wired', line)
                    compressor_match = re.search(r'([\d.]+[KMGT])\s*compressor', line)
                    
                    if used_match:
                        mem_used = round(parse_mem_value_to_gb(used_match.group(1)), 2)
                    if wired_match:
                        mem_wired = round(parse_mem_value_to_gb(wired_match.group(1)), 2)
                    if compressor_match:
                        mem_compressed = round(parse_mem_value_to_gb(compressor_match.group(1)), 2)
                    if wired_match:
                        mem_used = max(mem_used, mem_wired)
                    if unused_match and mem_used:
                        mem_total = round(mem_used + parse_mem_value_to_gb(unused_match.group(1)), 2)
                except:
                    pass
    except:
        pass
    
    return {
        "cpu": round(cpu, 1),
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_wired": mem_wired,
        "mem_compressed": mem_compressed,
    }


def analyze_sessions(minutes: int = 5, spec: Optional[dict] = None) -> dict:
    """分析会话 - 每5分钟统计"""
    env = spec or env_spec(None)
    gateway_log = env_gateway_log(env)
    if not gateway_log.exists():
        return {"total": 0, "slow": 0, "stuck": 0, "sessions": []}
    
    sessions = []
    dispatch_time = {}
    lines = []
    
    try:
        with open(gateway_log) as f:
            lines = f.readlines()[-8000:]
        
        # 先收集所有问题 - 支持多种格式
        questions = {}
        for line in lines:
            try:
                ts = None
                ts_str = ""
                msg = None
                
                # 格式1: "message in group xxx: 问题"
                if "message in" in line.lower() and ": " in line and "did not mention" not in line.lower():
                    idx = line.find("message in")
                    msg_start = line.find(": ", idx)
                    if msg_start > 0:
                        msg = line[msg_start+2:].strip()[:50]
                
                # 格式2: "DM from xxx: 问题"  
                elif "dm from" in line.lower() and ": " in line:
                    idx = line.lower().find("dm from")
                    msg_start = line.find(": ", idx)
                    if msg_start > 0:
                        msg = line[msg_start+2:].strip()[:50]
                
                # 格式3: 飞书发送的消息 "助手名: 内容"
                elif "feishu[default]:" in line.lower() and ": " in line:
                    idx = line.lower().find("feishu[default]:")
                    if idx >= 0:
                        msg_start = line.find(": ", idx + 15)
                        if msg_start > 0:
                            msg = line[msg_start+2:].strip()[:50]
                
                if msg and len(msg) > 2:
                    if "+08:00" in line:
                        ts_str = line[:25]
                        ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                    elif "Z" in line[:25]:
                        ts_str = line[:24]
                        ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                        ts += 8 * 3600
                    if ts:
                        ts_key = int(ts)
                        questions[ts_key] = {"msg": msg[:30], "time": ts_str[11:19] if ts_str else ""}
            except:
                pass
        
        # 分析会话
        for line in lines:
            try:
                ts = None
                ts_str = ""
                if "+08:00" in line:
                    ts_str = line[:25]
                    ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                elif "Z" in line[:25]:
                    ts_str = line[:24]
                    ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                    ts += 8 * 3600
                
                if ts is None:
                    continue
                
                if "dispatching to agent" in line.lower():
                    dispatch_time["dispatch"] = ts
                    # 扩大搜索范围到前后10秒
                    ts_key = int(ts)
                    found = False
                    for i in range(-10, 15):
                        if ts_key + i in questions:
                            dispatch_time["question"] = questions[ts_key + i]["msg"]
                            dispatch_time["question_time"] = questions[ts_key + i]["time"]
                            found = True
                            break
                    if not found:
                        dispatch_time["question"] = "未知"
                            
                elif "dispatch complete" in line.lower() and "dispatch" in dispatch_time:
                    duration = ts - dispatch_time["dispatch"]
                    question = dispatch_time.get("question", "无法获取问题内容")
                    question_time = dispatch_time.get("question_time", "")
                    
                    # 提取回复数量
                    replies = 0
                    import re
                    m = re.search(r'replies=(\d+)', line)
                    if m:
                        replies = int(m.group(1))
                    
                    # 分析慢响应原因
                    reason = "正常"
                    if duration > 120:
                        reason = "严重卡顿"
                    elif duration > 30:
                        reason = "响应慢"
                    
                    # 检查是否有错误关键词
                    line_lower = line.lower()
                    if any(k in line_lower for k in ["timeout", "timed out", "error", "fail"]):
                        if "timeout" in line_lower:
                            reason = "LLM超时" if duration > 30 else "正常(有超时)"
                        elif "error" in line_lower or "fail" in line_lower:
                            reason = "处理出错"
                    
                    sessions.append({
                        "time": question_time,
                        "duration": int(duration),
                        "question": question,
                        "replies": replies,
                        "status": "❌" if duration > 120 else ("⚠️" if duration > 30 else "✅"),
                        "reason": reason
                    })
                    dispatch_time = {}
            except:
                pass
    except:
        pass
    
    sessions = sessions[-20:]
    slow = sum(1 for s in sessions if s["duration"] > 30)
    stuck = sum(1 for s in sessions if s["duration"] > 120)
    
    # 分析慢响应原因 - 检查整个日志
    slow_reasons = {}
    for line in lines:
        lower = line.lower()
        if "timeout" in lower or "timed out" in lower:
            slow_reasons["LLM超时"] = slow_reasons.get("LLM超时", 0) + 1
        elif "error" in lower and ("llm" in lower or "model" in lower):
            slow_reasons["模型错误"] = slow_reasons.get("模型错误", 0) + 1
        elif "fail" in lower and ("api" in lower or "key" in lower):
            slow_reasons["API错误"] = slow_reasons.get("API错误", 0) + 1
        elif "400" in line or "401" in line or "403" in line or "500" in line:
            slow_reasons["HTTP错误"] = slow_reasons.get("HTTP错误", 0) + 1
    
    return {"total": len(sessions), "slow": slow, "stuck": stuck, "sessions": sessions, "reasons": slow_reasons}


def get_error_logs(count: int = 20, spec: Optional[dict] = None) -> list:
    """获取错误日志"""
    errors = []
    env = spec or env_spec(None)

    for log_file in [env_gateway_err_log(env), env_gateway_log(env)]:
        if not log_file.exists():
            continue
        try:
            with open(log_file) as f:
                lines = f.readlines()[-500:]
            
            for line in lines:
                lower = line.lower()
                if any(kw in lower for kw in ["error", "fail", "exception", "crash"]):
                    try:
                        ts = line[:19]
                        msg = line[20:].strip()[:150]
                        errors.append({"time": ts[11:19], "message": msg})
                    except:
                        pass
            break
        except:
            pass
    
    return errors[:count]


def get_version(spec: Optional[dict] = None) -> str:
    """获取版本"""
    env = spec or env_spec(None)
    try:
        result = subprocess.run(
            ["git", "-C", str(env["code"]), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except:
        pass
    return "unknown"


def get_diagnoses(metrics: dict, sessions: dict, processes: list) -> list:
    """获取诊断建议"""
    diagnoses = []
    
    # 内存
    if metrics["mem_total"] > 0:
        mem_percent = metrics["mem_used"] / metrics["mem_total"] * 100
        if mem_percent > 85:
            diagnoses.append({
                "level": "error",
                "title": "内存使用率过高",
                "message": f"当前 {mem_percent:.0f}%，建议重启 Gateway 释放内存",
                "action": "重启 Gateway"
            })
        elif mem_percent > 70:
            diagnoses.append({
                "level": "warning",
                "title": "内存使用率偏高",
                "message": f"当前 {mem_percent:.0f}%，注意监控",
                "action": None
            })
    
    # CPU
    if metrics["cpu"] > 90:
        diagnoses.append({
            "level": "error",
            "title": "CPU 使用率过高",
            "message": f"当前 {metrics['cpu']}%，检查是否有异常进程",
            "action": None
        })
    
    # 慢会话
    if sessions.get("stuck", 0) > 0:
        diagnoses.append({
            "level": "error",
            "title": f"存在 {sessions['stuck']} 个严重卡顿会话",
            "message": "会话响应超过2分钟，检查 LLM 响应或网络",
            "action": None
        })
    elif sessions.get("slow", 0) > 2:
        diagnoses.append({
            "level": "warning",
            "title": "响应缓慢",
            "message": f"过去30分钟有 {sessions['slow']} 个慢响应会话",
            "action": None
        })
    
    # 进程
    gateway_running = any("gateway" in p.get("cmd", "").lower() for p in processes if p)
    if not gateway_running:
        diagnoses.append({
            "level": "error",
            "title": "Gateway 进程未运行",
            "message": "进程已退出，需要立即处理",
            "action": "启动 Gateway"
        })
    
    # 正常
    if not diagnoses:
        diagnoses.append({
            "level": "success",
            "title": "系统运行正常",
            "message": "所有指标正常",
            "action": None
        })
    
    return diagnoses


def save_config(key: str, value: str) -> bool:
    """保存配置"""
    create_config_snapshots("before-config-change")
    return save_local_config_value(BASE_DIR, key, value)
    """加载告警历史"""
    alerts_file = BASE_DIR / "alerts.json"
    if alerts_file.exists():
        with open(alerts_file) as f:
            data = json.load(f)
            return [
                {"type": k, "time": datetime.fromtimestamp(v["last_alert"]).strftime("%H:%M:%S"), "count": v.get("count", 1)}
                for k, v in data.items()
            ]
    return []


def load_versions() -> dict:
    """加载版本历史"""
    versions_file = BASE_DIR / "versions.json"
    if versions_file.exists():
        with open(versions_file) as f:
            return json.load(f)
    return {"current": None, "history": []}


def run_script(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:
        return -1, "", str(exc)


def wait_for_env_listener(env_id: str, timeout: float = 15.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            spec = get_env_specs(load_config()).get(env_id)
        except Exception:
            spec = None
        if spec and get_listener_pid(int(spec["port"])) is not None:
            return True
        time.sleep(interval)
    return False


def switch_openclaw_environment(target_env: str) -> tuple[bool, str]:
    if target_env not in {"primary", "official"}:
        return False, "未知环境"

    previous_env = active_env_id(load_config())
    if not save_config("ACTIVE_OPENCLAW_ENV", target_env):
        return False, "保存 ACTIVE_OPENCLAW_ENV 失败"
    STORE.save_runtime_value("active_openclaw_env", {"env_id": target_env, "updated_at": int(time.time())})

    # First move the guardian target, then quiesce both environments so the old
    # environment is not immediately restarted during the switch window.
    run_script([str(OFFICIAL_MANAGER), "stop"], timeout=60)
    run_script([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=60)
    time.sleep(2)

    if target_env == "official":
        code, stdout, stderr = run_script([str(OFFICIAL_MANAGER), "start"], timeout=300)
        if code != 0:
            if previous_env != target_env:
                save_config("ACTIVE_OPENCLAW_ENV", previous_env)
                STORE.save_runtime_value("active_openclaw_env", {"env_id": previous_env, "updated_at": int(time.time())})
            return False, (stderr or stdout or "官方验证版启动失败").strip()
        if not wait_for_env_listener("official"):
            if previous_env != target_env:
                save_config("ACTIVE_OPENCLAW_ENV", previous_env)
                STORE.save_runtime_value("active_openclaw_env", {"env_id": previous_env, "updated_at": int(time.time())})
            return False, "官方验证版切换失败：Gateway 未成功启动"
        return True, stdout.strip() or "已切换到官方验证版"

    code, stdout, stderr = run_script([str(DESKTOP_RUNTIME), "start", "gateway"], timeout=180)
    if code != 0:
        if previous_env != target_env:
            save_config("ACTIVE_OPENCLAW_ENV", previous_env)
            STORE.save_runtime_value("active_openclaw_env", {"env_id": previous_env, "updated_at": int(time.time())})
        return False, (stderr or stdout or "主用版启动失败").strip()
    if not wait_for_env_listener("primary"):
        if previous_env != target_env:
            save_config("ACTIVE_OPENCLAW_ENV", previous_env)
            STORE.save_runtime_value("active_openclaw_env", {"env_id": previous_env, "updated_at": int(time.time())})
        return False, "当前主用版切换失败：Gateway 未成功启动"
    return True, stdout.strip() or "已切换到当前主用版"


def restart_active_openclaw_environment() -> tuple[bool, str, Optional[str], Optional[str], str]:
    cfg = load_config()
    target_env = active_env_id(cfg)
    spec = env_spec(target_env, cfg)
    old_pid = get_listener_pid(int(spec["port"]))
    old_pid_str = str(old_pid) if old_pid is not None else None

    run_script([str(OFFICIAL_MANAGER), "stop"], timeout=120)
    run_script([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=120)
    time.sleep(2)

    if target_env == "official":
        code, stdout, stderr = run_script([str(OFFICIAL_MANAGER), "start"], timeout=300)
    else:
        code, stdout, stderr = run_script([str(DESKTOP_RUNTIME), "start", "gateway"], timeout=180)

    if code != 0:
        return False, (stderr or stdout or "Gateway 重启失败").strip(), old_pid_str, None, target_env
    if not wait_for_env_listener(target_env):
        return False, f"{spec['name']} 重启失败：Gateway 未成功启动", old_pid_str, None, target_env

    new_pid = get_listener_pid(int(spec["port"]))
    new_pid_str = str(new_pid) if new_pid is not None else None
    if new_pid_str:
        return True, stdout.strip() or "Gateway 已重启", old_pid_str, new_pid_str, target_env
    return False, "Gateway 启动失败", old_pid_str, None, target_env


def execute_official_promotion() -> dict:
    config = load_config()
    environments = list_openclaw_environments(config)
    task_registry = get_task_registry_payload(limit=8)
    controller = PromotionController(BASE_DIR, STORE, config)
    result = controller.run(environments, task_registry)
    status = result.get("status", "unknown")
    details = {
        "status": status,
        "primary_git_head": (result.get("preflight") or {}).get("primary_git_head", ""),
        "official_git_head": (result.get("preflight") or {}).get("official_git_head", ""),
    }
    backups = result.get("backups") or {}
    if backups:
        details["snapshots"] = backups
    if status == "promoted":
        record_change("version", "官方验证版晋升为当前主用版", details)
    elif status == "rolled_back":
        details["error"] = result.get("error", "")
        record_change("recover", "官方验证版晋升失败，已回滚主用版", details)
    elif status == "failed_preflight":
        details["checks"] = result.get("preflight", {}).get("checks", [])
        record_change("version", "官方验证版晋升前检查未通过", details)
    return result


def manage_official_environment(action: str) -> tuple[bool, str]:
    allowed = {
        "prepare": "准备官方验证版",
        "start": "启动官方验证版",
        "stop": "停止官方验证版",
        "update": "更新官方验证版",
        "install-schedule": "安装官方自动更新",
        "schedule-status": "查看官方自动更新状态",
    }
    if action not in allowed:
        return False, "未知操作"
    timeout = 300 if action in {"prepare", "update", "start"} else 120
    code, stdout, stderr = run_script([str(OFFICIAL_MANAGER), action], timeout=timeout)
    message = (stdout or stderr or allowed[action]).strip()
    return code == 0, message


# ========== API 端点 ==========

@app.route("/")
def index():
    """主页"""
    html = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenClaw 健康监控中心</title>
    <style>
        :root {
            --bg: #07111a;
            --bg-elevated: #0d1722;
            --bg-panel: rgba(12, 21, 32, 0.92);
            --bg-panel-soft: rgba(18, 28, 42, 0.82);
            --bg-row: rgba(255, 255, 255, 0.035);
            --border: rgba(148, 163, 184, 0.18);
            --border-strong: rgba(148, 163, 184, 0.3);
            --text: #e8eef5;
            --text-muted: #9fb0c3;
            --text-soft: #71839a;
            --ok: #3ecf8e;
            --warn: #f4b740;
            --danger: #ef6b6b;
            --info: #58a6ff;
            --shadow-lg: 0 24px 60px rgba(0, 0, 0, 0.35);
            --shadow-md: 0 14px 34px rgba(0, 0, 0, 0.24);
            --radius-sm: 12px;
            --radius-md: 18px;
            --radius-lg: 24px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
            min-height: 100vh;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(88, 166, 255, 0.16), transparent 34%),
                radial-gradient(circle at top right, rgba(62, 207, 142, 0.1), transparent 28%),
                linear-gradient(180deg, #0a1420 0%, #07111a 42%, #091521 100%);
        }
        body::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image: linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
            background-size: 36px 36px;
            mask-image: linear-gradient(180deg, rgba(0,0,0,0.35), transparent 85%);
        }
        .container {
            position: relative;
            width: min(100%, 1720px);
            margin: 0 auto;
            padding: 18px clamp(14px, 2vw, 28px) 28px;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 18px;
            padding: 20px 22px;
            margin-bottom: 18px;
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            background: linear-gradient(135deg, rgba(18, 29, 43, 0.95), rgba(10, 18, 29, 0.96));
            box-shadow: var(--shadow-lg);
            backdrop-filter: blur(18px);
        }
        .header-copy { display: grid; gap: 6px; }
        .eyebrow {
            font-size: 11px;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: var(--text-soft);
        }
        h1 {
            font-size: clamp(20px, 1.7vw, 24px);
            line-height: 1.05;
            letter-spacing: -0.04em;
        }
        .refresh-info { font-size: 14px; color: var(--text-muted); max-width: 720px; }
        .actions { display: flex; flex-wrap: wrap; gap: 10px; }
        .btn,
        .config-btn,
        .diagnose-action,
        .list-expander {
            appearance: none;
            border: 1px solid var(--border);
            border-radius: 999px;
            background: rgba(255,255,255,0.06);
            color: var(--text);
            cursor: pointer;
            transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
        }
        .btn,
        .config-btn,
        .diagnose-action { padding: 10px 16px; font-size: 13px; font-weight: 600; }
        .btn:hover,
        .config-btn:hover,
        .diagnose-action:hover,
        .list-expander:hover {
            transform: translateY(-1px);
            border-color: rgba(88, 166, 255, 0.45);
            background: rgba(88, 166, 255, 0.12);
            box-shadow: 0 10px 22px rgba(27, 54, 87, 0.28);
        }
        .btn-primary,
        .config-btn,
        input:checked + .slider {
            background: linear-gradient(135deg, #4f8cff, #2d74ff);
            border-color: rgba(88, 166, 255, 0.55);
            box-shadow: 0 14px 28px rgba(45, 116, 255, 0.28);
        }
        .btn-current {
            background: linear-gradient(135deg, #32c787, #179b63);
            border-color: rgba(62, 207, 142, 0.5);
            color: #f2fff8;
            box-shadow: 0 14px 28px rgba(23, 155, 99, 0.28);
            cursor: default;
        }
        .btn-current:disabled { opacity: 1; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(180px, 1fr));
            gap: 14px;
            margin: 0 0 18px;
        }
        .card,
        .section,
        .panel-shell,
        .memory-box,
        .incident-card,
        .workflow-box,
        .workflow-collapsible,
        .promotion-stage,
        .env-card,
        .agent-card,
        .control-queue-item,
        .diagnose-item,
        .event-item,
        .event-empty,
        .memory-item {
            background: var(--bg-panel);
            border: 1px solid var(--border);
            box-shadow: var(--shadow-md);
            backdrop-filter: blur(18px);
        }
        .card {
            position: relative;
            overflow: hidden;
            min-height: 136px;
            padding: 18px;
            border-radius: var(--radius-md);
        }
        .card::before {
            content: "";
            position: absolute;
            inset: 0 0 auto 0;
            height: 3px;
            background: linear-gradient(90deg, rgba(88,166,255,0.95), rgba(62,207,142,0.7));
        }
        .card h3,
        .memory-box-title,
        .workflow-title,
        .promotion-stage-title,
        .incident-label,
        .event-meta,
        .agent-meta,
        .control-queue-meta,
        .env-meta,
        .config-value,
        th {
            font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
            font-size: 11px;
            letter-spacing: 0.03em;
            color: var(--text-soft);
        }
        .card .value,
        .memory-box-main,
        .incident-main,
        .workflow-main,
        .env-title,
        .promotion-stage-main {
            font-size: clamp(15px, 1.25vw, 18px);
            font-weight: 700;
            line-height: 1.08;
            letter-spacing: -0.04em;
        }
        .card .sub,
        .memory-box-sub,
        .incident-sub,
        .workflow-sub,
        .event-details,
        .agent-detail,
        .diagnose-msg { font-size: 12px; line-height: 1.55; color: var(--text-muted); }
        .progress { height: 6px; background: rgba(255,255,255,0.08); border-radius: 999px; margin-top: 12px; overflow: hidden; }
        .progress-bar { height: 100%; border-radius: 999px; transition: width 0.3s; }
        .good { color: var(--ok); background-color: var(--ok); }
        .warning { color: var(--warn); background-color: var(--warn); }
        .error { color: var(--danger); background-color: var(--danger); }
        .status-ok { color: var(--ok); background: transparent; }
        .status-warning { color: var(--warn); background: transparent; }
        .status-error { color: var(--danger); background: transparent; }
        .section {
            margin: 0;
            padding: 18px 18px 20px;
            border-radius: var(--radius-lg);
        }
        .section h2 {
            font-size: 15px;
            margin-bottom: 14px;
            padding-bottom: 12px;
            border-bottom: 1px solid rgba(255,255,255,0.07);
            letter-spacing: -0.02em;
        }
        .section-lead { display: none; }
        .dashboard-layout,
        .dashboard-stack { display: grid; gap: 18px; }
        .hero-supergrid {
            display: grid;
            grid-template-columns: minmax(0, 1.28fr) minmax(360px, 0.72fr);
            gap: 18px;
            align-items: stretch;
            margin-bottom: 18px;
        }
        .hero-surface,
        .hero-side {
            position: relative;
            display: grid;
            gap: 14px;
            padding: 18px;
            border-radius: var(--radius-lg);
            border: 1px solid var(--border-strong);
            background: linear-gradient(180deg, rgba(15, 25, 38, 0.96), rgba(9, 17, 26, 0.98));
            box-shadow: var(--shadow-lg);
            overflow: hidden;
        }
        .hero-surface::before,
        .hero-side::before {
            content: "";
            position: absolute;
            inset: 0 0 auto 0;
            height: 2px;
            background: linear-gradient(90deg, rgba(88,166,255,0.95), rgba(62,207,142,0.65));
        }
        .hero-headline {
            display: grid;
            gap: 8px;
            padding-bottom: 14px;
            border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .hero-label {
            font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
            font-size: 10px;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: var(--text-soft);
        }
        .hero-title {
            font-size: clamp(15px, 1.2vw, 18px);
            line-height: 1;
            letter-spacing: -0.05em;
            font-weight: 800;
        }
        .hero-subtitle { display: none; }
        .hero-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.05fr) minmax(280px, 0.95fr);
            gap: 14px;
            align-items: start;
        }
        .hero-block {
            padding: 16px 18px;
            border-radius: var(--radius-md);
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
        }
        .hero-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 6px;
        }
        .hero-kpi-strip {
            display: grid;
            grid-template-columns: repeat(4, minmax(180px, 1fr));
            gap: 14px;
            margin: 0 0 18px;
        }
        .hero-meta-grid,
        .operations-zone,
        .incident-zone,
        .promotion-zone,
        .evidence-zone,
        .maintenance-zone { display: grid; gap: 18px; }
        .hero-meta-grid { grid-template-columns: 1fr; gap: 12px; }
        .operations-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.05fr) minmax(320px, 0.95fr);
            gap: 14px;
            align-items: start;
        }
        .incident-grid-wide,
        .promotion-grid,
        .evidence-grid,
        .maintenance-grid {
            display: grid;
            gap: 14px;
        }
        .incident-grid-wide { grid-template-columns: minmax(0, 1.08fr) minmax(320px, 0.92fr); }
        .promotion-grid { grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr); }
        .evidence-grid { grid-template-columns: minmax(0, 1.04fr) minmax(0, 0.96fr); }
        .maintenance-grid { grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr); }
        .operations-layout {
            display: grid;
            grid-template-columns: minmax(280px, 0.95fr) minmax(320px, 1.05fr);
            gap: 14px;
            align-items: start;
        }
        .panel-shell { padding: 14px; border-radius: var(--radius-md); }
        .memory-summary,
        .row,
        .env-grid,
        .promotion-stage-grid {
            display: grid;
            gap: 12px;
        }
        .memory-summary { grid-template-columns: minmax(0, 1.08fr) minmax(280px, 0.92fr); margin-bottom: 14px; }
        .row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .env-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); margin-top: 12px; }
        .promotion-stage-grid { grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); }
        .memory-box,
        .workflow-box,
        .workflow-collapsible,
        .env-card,
        .promotion-stage,
        .incident-card { padding: 16px 18px; border-radius: var(--radius-md); }
        .incident-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
        .incident-card:first-child {
            background: linear-gradient(135deg, rgba(79, 140, 255, 0.14), rgba(12, 21, 32, 0.96));
            border-color: rgba(88, 166, 255, 0.32);
        }
        .incident-card.error,
        .event-item.anomaly,
        .promotion-stage.fail { border-color: rgba(239, 107, 107, 0.4); background: linear-gradient(135deg, rgba(239, 107, 107, 0.12), rgba(12, 21, 32, 0.95)); }
        .incident-card.watch,
        .event-item.warning { border-color: rgba(244, 183, 64, 0.38); background: linear-gradient(135deg, rgba(244, 183, 64, 0.1), rgba(12, 21, 32, 0.95)); }
        .event-item.pipeline,
        .event-item.info,
        .promotion-stage.active { border-color: rgba(88, 166, 255, 0.34); background: linear-gradient(135deg, rgba(88, 166, 255, 0.12), rgba(12, 21, 32, 0.95)); }
        .promotion-stage.ok,
        .env-card.active,
        .agent-card.processing { border-color: rgba(62, 207, 142, 0.34); background: linear-gradient(135deg, rgba(62, 207, 142, 0.12), rgba(12, 21, 32, 0.95)); }
        .promotion-stage.pending { background: var(--bg-panel-soft); }
        .event-list,
        .memory-items,
        .agent-grid,
        .workflow-board,
        .control-queue { display: grid; gap: 10px; }
        .event-item,
        .memory-item,
        .control-queue-item,
        .diagnose-item,
        .workflow-step {
            padding: 12px 14px;
            border-radius: var(--radius-sm);
        }
        .event-header,
        .env-title-row,
        .agent-card-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: flex-start;
        }
        .event-time,
        .agent-state-pill,
        .env-pill,
        .phase-pill {
            white-space: nowrap;
            padding: 5px 9px;
            border-radius: 999px;
            font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
            font-size: 11px;
            color: #d7e0ea;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.06);
        }
        .env-pill.active { background: rgba(62, 207, 142, 0.14); color: #dffcef; border-color: rgba(62, 207, 142, 0.26); }
        .agent-card.idle { opacity: 0.8; background: var(--bg-panel-soft); }
        .agent-name,
        .event-title,
        .control-queue-title,
        .agent-task,
        .diagnose-title { font-size: 15px; font-weight: 700; color: var(--text); }
        .agent-task { margin-bottom: 8px; }
        .agent-file,
        .memory-item-note { margin-top: 8px; font-size: 11px; color: var(--text-soft); }
        .memory-item,
        .diagnose-item { display: flex; justify-content: space-between; gap: 14px; align-items: center; }
        .memory-item-value { white-space: nowrap; font-size: 14px; font-weight: 700; }
        .env-actions,
        .promotion-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
        .env-link { color: #9ec7ff; text-decoration: none; font-size: 12px; }
        .env-link:hover { text-decoration: underline; }
        .env-link.disabled { color: var(--text-soft); pointer-events: none; cursor: not-allowed; text-decoration: none; }
        .workflow-collapsible { overflow: hidden; }
        .workflow-collapsible summary { list-style: none; cursor: pointer; padding: 16px 18px; }
        .workflow-collapsible summary::-webkit-details-marker { display: none; }
        .workflow-collapsible[open] summary { border-bottom: 1px solid rgba(255,255,255,0.08); }
        .workflow-collapsible-body { padding: 14px 18px 18px; }
        .workflow-step { background: var(--bg-row); border: 1px solid rgba(255,255,255,0.04); }
        .workflow-step strong { color: var(--text); }
        .phase-strip { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .phase-pill.completed { background: rgba(62, 207, 142, 0.16); color: #dffcef; }
        .phase-pill.running { background: rgba(88, 166, 255, 0.16); color: #e3f0ff; }
        .phase-pill.blocked { background: rgba(239, 107, 107, 0.16); color: #ffe3e3; }
        .phase-pill.pending { background: rgba(255,255,255,0.08); color: #c2cfdb; }
        .switch { position: relative; display: inline-block; width: 46px; height: 26px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
            position: absolute;
            inset: 0;
            cursor: pointer;
            background-color: rgba(255,255,255,0.14);
            transition: .3s;
            border-radius: 999px;
            border: 1px solid rgba(255,255,255,0.08);
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 3px;
            bottom: 3px;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }
        input:checked + .slider:before { transform: translateX(20px); }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            overflow: hidden;
            border-radius: var(--radius-sm);
        }
        th, td {
            padding: 12px 10px;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.06);
            vertical-align: top;
        }
        td { color: var(--text-muted); }
        tbody tr:hover { background: rgba(255,255,255,0.03); }
        .status-strip {
            display: grid;
            grid-template-columns: 1.2fr 1fr;
            gap: 12px;
            padding: 14px 18px;
            margin-bottom: 18px;
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
            background: rgba(12, 21, 32, 0.9);
            box-shadow: var(--shadow-md);
        }
        .status-group,
        .status-summary { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
        .status-pill,
        .env-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            border-radius: 999px;
            border: 1px solid rgba(255,255,255,0.07);
            background: rgba(255,255,255,0.04);
            color: var(--text-muted);
            font-size: 12px;
            font-weight: 600;
        }
        .status-pill strong,
        .env-chip strong { color: var(--text); font-weight: 700; }
        .env-chip.active { background: rgba(88,166,255,0.14); border-color: rgba(88,166,255,0.22); }
        .workspace-shell {
            display: grid;
            grid-template-columns: 220px minmax(0, 1fr);
            gap: 18px;
            align-items: start;
        }
        .sidebar-nav {
            position: sticky;
            top: 18px;
            display: grid;
            gap: 8px;
            padding: 14px;
            border-radius: var(--radius-lg);
            border: 1px solid var(--border);
            background: rgba(12, 21, 32, 0.92);
            box-shadow: var(--shadow-md);
        }
        .sidebar-label {
            font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
            font-size: 11px;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: var(--text-soft);
            padding: 4px 6px 10px;
        }
        .tab {
            width: 100%;
            padding: 12px 14px;
            border: 1px solid transparent;
            border-radius: 14px;
            background: transparent;
            color: var(--text-soft);
            cursor: pointer;
            font-size: 14px;
            font-weight: 700;
            text-align: left;
            transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
        }
        .tab:hover { background: rgba(255,255,255,0.04); border-color: rgba(255,255,255,0.05); }
        .tab.active { color: var(--text); background: rgba(88, 166, 255, 0.14); border-color: rgba(88, 166, 255, 0.2); box-shadow: inset 0 0 0 1px rgba(88, 166, 255, 0.12); }
        .content-panel {
            display: grid;
            gap: 18px;
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .page-stack { display: grid; gap: 18px; }
        .overview-grid,
        .environment-page,
        .tasks-page,
        .agents-page,
        .diagnostics-page,
        .release-page,
        .recovery-page,
        .learning-page,
        .settings-page { display: grid; gap: 18px; }
        .overview-summary-grid,
        .environment-page,
        .tasks-page,
        .diagnostics-page,
        .release-page,
        .learning-page,
        .settings-page { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .recovery-page { grid-template-columns: 1.05fr 0.95fr; }
        .agents-page { grid-template-columns: minmax(300px, 0.84fr) minmax(0, 1.16fr); }
        .section-compact h2 { font-size: 17px; margin-bottom: 10px; }
        .metric-card-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .metric-card {
            padding: 14px 16px;
            border-radius: var(--radius-md);
            border: 1px solid rgba(255,255,255,0.07);
            background: rgba(255,255,255,0.035);
        }
        .metric-label,
        .data-label {
            font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
            font-size: 11px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--text-soft);
            margin-bottom: 8px;
        }
        .metric-main { font-size: 22px; font-weight: 800; letter-spacing: -0.04em; }
        .metric-sub { margin-top: 6px; font-size: 13px; color: var(--text-muted); line-height: 1.6; }
        .summary-list,
        .warning-list { display: grid; gap: 10px; }
        .summary-item,
        .warning-item {
            padding: 12px 14px;
            border-radius: var(--radius-sm);
            border: 1px solid rgba(255,255,255,0.06);
            background: rgba(255,255,255,0.03);
        }
        .summary-item { font-size: 12px; line-height: 1.55; }
        .summary-item strong,
        .warning-item strong { font-size: 12px; }
        .warning-item { background: linear-gradient(135deg, rgba(244, 183, 64, 0.1), rgba(12, 21, 32, 0.96)); }
        .summary-item strong,
        .warning-item strong { color: var(--text); }
        .dual-column { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
        .agent-seats { display: grid; gap: 10px; }
        .agent-seat {
            padding: 14px 15px;
            border-radius: var(--radius-md);
            border: 1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.03);
            cursor: pointer;
            transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
        }
        .agent-seat:hover { transform: translateY(-1px); }
        .agent-seat.active {
            border-color: rgba(88,166,255,0.3);
            background: linear-gradient(135deg, rgba(88,166,255,0.12), rgba(12, 21, 32, 0.94));
            box-shadow: inset 0 0 0 1px rgba(88,166,255,0.14);
        }
        .agent-seat.processing {
            border-color: rgba(62,207,142,0.26);
            background: linear-gradient(135deg, rgba(62,207,142,0.12), rgba(12,21,32,0.94));
        }
        .agent-seat-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
        .agent-seat-sub { margin-top: 8px; font-size: 12px; color: var(--text-muted); line-height: 1.6; }
        .focus-stage {
            display: grid;
            gap: 12px;
            padding: 18px;
            border-radius: var(--radius-lg);
            border: 1px solid rgba(88,166,255,0.18);
            background: linear-gradient(180deg, rgba(12, 23, 36, 0.96), rgba(8, 15, 24, 0.98));
            box-shadow: var(--shadow-md);
        }
        .focus-quote {
            padding: 14px 16px;
            border-radius: var(--radius-md);
            background: rgba(0,0,0,0.28);
            border: 1px solid rgba(255,255,255,0.06);
            color: var(--text);
            font-size: 14px;
            line-height: 1.7;
        }
        .focus-feed { display: grid; gap: 10px; }
        .feed-item {
            padding: 12px 14px;
            border-radius: var(--radius-sm);
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.05);
        }
        .settings-grid { display: grid; gap: 14px; }
        .event-empty {
            padding: 18px;
            border-radius: var(--radius-md);
            border-style: dashed;
            color: var(--text-soft);
            font-size: 13px;
        }
        .toast-container { position: fixed; top: 20px; right: 20px; z-index: 9999; }
        .toast {
            padding: 15px 20px;
            margin-bottom: 10px;
            border-radius: var(--radius-sm);
            color: #fff;
            font-size: 14px;
            animation: slideIn 0.3s;
            max-width: 420px;
            box-shadow: var(--shadow-lg);
        }
        .toast.error { background: #c43d3d; }
        .toast.warning { background: #b68419; }
        .toast.success { background: #158a58; }
        .toast.info { background: #2d74ff; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
        @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
        .live { animation: pulse 2s infinite; }
        .footer-bar { text-align: center; padding: 22px 0 4px; color: var(--text-soft); font-size: 12px; }
        @media (max-width: 1500px) {
            .status-strip,
            .workspace-shell,
            .hero-supergrid,
            .incident-grid-wide,
            .promotion-grid,
            .evidence-grid,
            .maintenance-grid,
            .operations-grid,
            .operations-layout,
            .memory-summary,
            .incident-grid,
            .overview-summary-grid,
            .environment-page,
            .tasks-page,
            .agents-page,
            .diagnostics-page,
            .release-page,
            .recovery-page,
            .learning-page,
            .settings-page,
            .dual-column { grid-template-columns: 1fr; }
        }
        @media (max-width: 1260px) {
            .hero-supergrid,
            .hero-grid,
            .operations-grid,
            .row { grid-template-columns: 1fr; }
            .stats-grid,
            .hero-kpi-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .workspace-shell { grid-template-columns: 1fr; }
            .sidebar-nav { position: static; grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .sidebar-label { grid-column: 1 / -1; }
        }
        @media (max-width: 900px) {
            .stats-grid,
            .hero-kpi-strip,
            .env-grid,
            .promotion-stage-grid,
            .incident-grid,
            .row,
            .memory-summary,
            .operations-layout,
            .hero-supergrid,
            .hero-grid,
            .incident-grid-wide,
            .promotion-grid,
            .evidence-grid,
            .maintenance-grid,
            .operations-grid,
            .sidebar-nav,
            .metric-card-grid,
            .overview-summary-grid,
            .environment-page,
            .tasks-page,
            .agents-page,
            .diagnostics-page,
            .release-page,
            .recovery-page,
            .learning-page,
            .settings-page,
            .dual-column { grid-template-columns: 1fr; }
            .container { padding-inline: 12px; }
            header { flex-direction: column; align-items: flex-start; }
            .tab { font-size: 13px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-copy">
                <div class="eyebrow">Health Guardian Control Plane</div>
                <h1>OpenClaw 健康守护者</h1>
                <div class="refresh-info">把双环境守护、活跃代理、版本晋升和异常定位收束到一条清晰的控制视线里。</div>
            </div>
            <div class="actions">
                <button class="btn" onclick="location.reload()">🔄 刷新</button>
                <button class="btn btn-primary" onclick="restartGateway()">🔁 重启 Gateway</button>
                <button class="btn" style="background:#dc2626" onclick="emergencyRecover()">🚨 急救</button>
            </div>
        </header>
        
        <div class="status-strip">
            <div class="status-group">
                <div id="global-active-env" class="env-chip active"><strong>当前环境</strong> 加载中</div>
                <div id="global-primary-env" class="env-chip"><strong>Primary</strong> --</div>
                <div id="global-official-env" class="env-chip"><strong>Official</strong> --</div>
            </div>
            <div class="status-summary">
                <div id="global-gateway-status" class="status-pill"><strong>Gateway</strong> --</div>
                <div id="global-guardian-status" class="status-pill"><strong>Guardian</strong> --</div>
                <div id="global-task-stats" class="status-pill"><strong>任务</strong> --</div>
                <div id="global-alert-stats" class="status-pill"><strong>告警</strong> --</div>
            </div>
        </div>

        <div class="workspace-shell">
            <div class="sidebar-nav">
                <div class="sidebar-label">Control Areas</div>
                <button class="tab active" onclick="switchTab('overview', event)">总览</button>
                <button class="tab" onclick="switchTab('environments', event)">环境切换</button>
                <button class="tab" onclick="switchTab('tasks', event)">任务处理</button>
                <button class="tab" onclick="switchTab('agents', event)">代理活动</button>
                <button class="tab" onclick="switchTab('diagnostics', event)">异常排查</button>
                <button class="tab" onclick="switchTab('release', event)">升级发布</button>
                <button class="tab" onclick="switchTab('recovery', event)">快照恢复</button>
                <button class="tab" onclick="switchTab('learning', event)">学习优化</button>
                <button class="tab" onclick="switchTab('settings', event)">系统设置</button>
            </div>

            <div class="content-panel">
                <div id="tab-overview" class="tab-content active">
                    <div class="page-stack">
                        <div class="hero-supergrid">
                            <div class="hero-surface">
                                <div class="hero-headline">
                                    <div class="hero-label" title="系统摘要">总览</div>
                                    <div class="hero-title">当前状态与下一步</div>
                                    <div class="hero-subtitle"></div>
                                </div>
                                <div class="hero-grid">
                                    <div class="hero-block">
                                        <div class="hero-label" title="当前最重要状态">当前状态</div>
                                        <div id="incident-summary" class="incident-grid"></div>
                                    </div>
                                    <div class="hero-meta-grid">
                                        <div class="hero-block">
                                            <div class="hero-label" title="当前激活环境">环境</div>
                                            <div id="overview-environment-quick" class="summary-list"></div>
                                        </div>
                                        <div class="hero-block">
                                        <div class="hero-label" title="建议优先动作">操作</div>
                                            <div id="overview-next-action" class="summary-list"></div>
                                            <div class="hero-actions">
                                                <button class="btn" onclick="location.reload()">刷新</button>
                                                <button class="btn btn-primary" onclick="restartGateway()">重启 Gateway</button>
                                                <button class="btn" style="background:#dc2626" onclick="emergencyRecover()">急救恢复</button>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div class="hero-side">
                                <div class="hero-label" title="发布与晋升摘要">发布</div>
                                <div id="overview-release-quick" class="summary-list"></div>
                                <div id="overview-warning-list" class="warning-list"></div>
                            </div>
                        </div>

                        <div class="hero-kpi-strip">
                            <div class="card">
                                <h3>CPU 使用率</h3>
                                <div class="value" id="cpu">--</div>
                                <div class="sub" id="cpu-sub">--</div>
                                <div class="progress"><div class="progress-bar" id="cpu-bar"></div></div>
                            </div>
                            <div class="card">
                                <h3>内存使用</h3>
                                <div class="value" id="mem">--</div>
                                <div class="sub" id="mem-sub">--</div>
                                <div class="progress"><div class="progress-bar" id="mem-bar"></div></div>
                            </div>
                            <div class="card">
                                <h3>会话统计</h3>
                                <div class="value" id="sessions">--</div>
                                <div class="sub" id="sessions-sub">--</div>
                                <div id="slow-reasons" style="font-size:11px;color:#888;margin-top:5px"></div>
                            </div>
                            <div class="card">
                                <h3>服务状态</h3>
                                <div class="value" id="gateway-status">--</div>
                                <div class="sub" id="process-pid">--</div>
                            </div>
                        </div>

                        <div class="overview-summary-grid">
                            <div class="section section-compact">
                                <h2>环境摘要</h2>
                                <div id="overview-environment-cards" class="summary-list"></div>
                            </div>
                            <div class="section section-compact">
                                <h2>任务摘要</h2>
                                <div id="overview-task-cards" class="summary-list"></div>
                            </div>
                            <div class="section section-compact">
                                <h2>最近异常</h2>
                                <div id="overview-recent-events" class="summary-list"></div>
                            </div>
                            <div class="section section-compact">
                                <h2>模型 / 控制失败摘要</h2>
                                <div id="overview-failure-summary" class="summary-list"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-environments" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>环境切换</h2>
                            <div class="section-lead"></div>
                            <div class="environment-page">
                                <div class="dashboard-stack">
                                    <div id="environment-summary" class="memory-box"></div>
                                    <div id="environment-alerts" class="warning-list"></div>
                                </div>
                                <div class="dashboard-stack">
                                    <div id="environment-workflow" class="workflow-board"></div>
                                </div>
                            </div>
                            <div id="environment-cards" class="env-grid"></div>
                        </div>
                    </div>
                </div>

                <div id="tab-tasks" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>任务处理</h2>
                            <div class="section-lead"></div>
                            <div class="tasks-page">
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>当前任务</h2>
                                        <div id="task-registry-summary" class="memory-box" style="margin-bottom:14px;"></div>
                                        <div id="task-registry-list" class="event-list"></div>
                                    </div>
                                </div>
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>控制面状态</h2>
                                        <div id="control-plane-summary" class="memory-box"></div>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>会话裁决</h2>
                                        <div id="session-resolution" class="memory-box"></div>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>待处理控制动作</h2>
                                        <div id="control-queue-board" class="event-list"></div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-agents" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>代理活动</h2>
                            <div class="section-lead"></div>
                            <div class="agents-page">
                                <div class="dashboard-stack">
                                    <div id="active-agents-summary" class="memory-box agent-summary-box"></div>
                                    <div id="active-agents-list" class="agent-seats"></div>
                                </div>
                                <div class="dashboard-stack">
                                    <div id="agent-activity-focus" class="focus-stage"></div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-diagnostics" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>异常排查</h2>
                            <div class="section-lead"></div>
                            <div class="diagnostics-page">
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>诊断建议</h2>
                                        <div id="diagnoses"></div>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>最近异常</h2>
                                        <div id="recent-events" class="event-list"></div>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>会话分析</h2>
                                        <table>
                                            <thead><tr><th>时间</th><th>问题</th><th>回复</th><th>耗时</th><th>状态</th></tr></thead>
                                            <tbody id="slow-sessions"></tbody>
                                        </table>
                                    </div>
                                </div>
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>内存归因</h2>
                                        <div id="memory-attribution" class="memory-summary"></div>
                                        <div id="memory-items" class="memory-items"></div>
                                        <table>
                                            <thead><tr><th>PID</th><th>用户</th><th>CPU %</th><th>内存</th><th>进程</th></tr></thead>
                                            <tbody id="top-processes"></tbody>
                                        </table>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>错误日志</h2>
                                        <table>
                                            <thead><tr><th>时间</th><th>错误信息</th></tr></thead>
                                            <tbody id="error-logs"></tbody>
                                        </table>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>运行进程</h2>
                                        <table>
                                            <thead><tr><th>进程</th><th>PID</th><th>CPU %</th><th>内存 %</th></tr></thead>
                                            <tbody id="processes"></tbody>
                                        </table>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-release" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>升级发布</h2>
                            <div class="section-lead"></div>
                            <div class="release-page">
                                <div class="dashboard-stack">
                                    <div id="promotion-summary" class="memory-box"></div>
                                    <div id="promotion-status-board" class="promotion-board"></div>
                                </div>
                                <div class="dashboard-stack">
                                    <div id="release-summary-list" class="summary-list"></div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-recovery" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>快照恢复</h2>
                            <div class="section-lead"></div>
                            <div class="recovery-page">
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>配置快照</h2>
                                        <div style="margin-bottom: 15px;">
                                            <button class="btn" onclick="loadSnapshots()">刷新</button>
                                            <button class="btn btn-primary" onclick="captureSnapshot()">创建快照</button>
                                        </div>
                                        <table>
                                            <thead><tr><th>名称</th><th>标签</th><th>创建时间</th><th>文件数</th><th>操作</th></tr></thead>
                                            <tbody id="snapshot-logs"></tbody>
                                        </table>
                                    </div>
                                </div>
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>变更日志</h2>
                                        <div style="margin-bottom: 15px;">
                                            <button class="btn" onclick="loadChanges()">刷新</button>
                                            <button class="btn" style="background:#dc2626" onclick="emergencyRecover()">执行急救恢复</button>
                                        </div>
                                        <table>
                                            <thead><tr><th>日期</th><th>时间</th><th>类型</th><th>摘要</th><th>详情</th></tr></thead>
                                            <tbody id="change-logs"></tbody>
                                        </table>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-learning" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>学习优化</h2>
                            <div class="section-lead"></div>
                            <div class="learning-page">
                                <div class="dashboard-stack">
                                    <div id="learning-summary" class="memory-box"></div>
                                    <div class="panel-shell">
                                        <h2>当前学习项</h2>
                                        <div id="learning-list" class="event-list"></div>
                                    </div>
                                </div>
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>改进建议</h2>
                                        <div id="learning-suggestions" class="event-list"></div>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>最近反思</h2>
                                        <div id="learning-reflections" class="event-list"></div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="tab-settings" class="tab-content">
                    <div class="page-stack">
                        <div class="section">
                            <h2>系统设置</h2>
                            <div class="section-lead"></div>
                            <div class="settings-page">
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>配置管理</h2>
                                        <table>
                                            <tr>
                                                <td width="200">自动更新</td>
                                                <td>
                                                    <label class="switch">
                                                        <input type="checkbox" id="auto-update-toggle" onchange="toggleAutoUpdate(this)">
                                                        <span class="slider"></span>
                                                    </label>
                                                    <span id="auto-update-status" class="config-value"></span>
                                                </td>
                                            </tr>
                                            <tr>
                                                <td>当前版本</td>
                                                <td id="current-version">--</td>
                                            </tr>
                                            <tr>
                                                <td>版本历史</td>
                                                <td id="version-history">--</td>
                                            </tr>
                                            <tr>
                                                <td>钉钉通知</td>
                                                <td>
                                                    <button class="config-btn" id="dingtalk-btn" onclick="configureWebhook('DINGTALK')">配置</button>
                                                    <span id="dingtalk-status" class="config-value"></span>
                                                </td>
                                            </tr>
                                            <tr>
                                                <td>飞书通知</td>
                                                <td>
                                                    <button class="config-btn" id="feishu-btn" onclick="configureWebhook('FEISHU')">配置</button>
                                                    <span id="feishu-status" class="config-value"></span>
                                                </td>
                                            </tr>
                                        </table>
                                    </div>
                                </div>
                                <div class="dashboard-stack">
                                    <div class="panel-shell">
                                        <h2>上下文运行基线</h2>
                                        <div id="context-readiness" class="summary-list"></div>
                                    </div>
                                    <div class="panel-shell">
                                        <h2>系统信息</h2>
                                        <div id="system-info-summary" class="summary-list"></div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <footer class="footer-bar">
                    <span class="live">●</span> 自动刷新中 | <span id="last-update">--</span>
                </footer>
            </div>
        </div>
    </div>
    
    <script>
        let currentData = null;
        let promotionActionInFlight = false;
        let selectedAgentId = null;

        function makeExpandableList(items, renderItem, emptyHtml, options = {}) {
            const limit = options.limit || 4;
            const buttonLabel = options.buttonLabel || '展开更多';
            const collapseLabel = options.collapseLabel || '收起';
            if (!items || !items.length) return emptyHtml;
            const visible = items.slice(0, limit).map(renderItem).join('');
            const hiddenItems = items.slice(limit).map(renderItem).join('');
            if (items.length <= limit) return visible;
            const hiddenId = `expand-${Math.random().toString(36).slice(2, 10)}`;
            return `
                ${visible}
                <div id="${hiddenId}" class="is-hidden">${hiddenItems}</div>
                <button class="list-expander" data-expanded="false" onclick="toggleExpandableList('${hiddenId}', this, '${buttonLabel}', '${collapseLabel}')">${buttonLabel}（${items.length - limit}）</button>
            `;
        }

        function toggleExpandableList(hiddenId, button, expandLabel, collapseLabel) {
            const hidden = document.getElementById(hiddenId);
            if (!hidden) return;
            const expanded = button.dataset.expanded === 'true';
            hidden.classList.toggle('is-hidden', expanded);
            button.dataset.expanded = expanded ? 'false' : 'true';
            button.textContent = expanded ? expandLabel : collapseLabel;
        }

        function formatPromotionTime(ts) {
            if (!ts) return '暂无';
            try {
                return new Date(ts * 1000).toLocaleString();
            } catch (_) {
                return String(ts);
            }
        }

        function renderSimpleList(targetId, items, emptyText) {
            const el = document.getElementById(targetId);
            if (!el) return;
            if (!items || !items.length) {
                el.innerHTML = `<div class="summary-item">${emptyText}</div>`;
                return;
            }
            el.innerHTML = items.map(item => `
                <div class="summary-item">
                    <strong>${item.title}</strong><br/>
                    <span>${item.body}</span>
                </div>
            `).join('');
        }

        function chooseAgentFocus(activeAgents) {
            if (!activeAgents || !activeAgents.length) return null;
            const current = activeAgents.find(item => item.agent_id === selectedAgentId);
            if (current) return current;
            const processingKeywords = ['正在', '启动', '派发', '等待', '回执受限', '处理中'];
            const processing = activeAgents.find(item => processingKeywords.some(keyword => (item.state_label || '').includes(keyword)));
            const chosen = processing || activeAgents[0];
            selectedAgentId = chosen.agent_id;
            return chosen;
        }

        function renderAgentFocus(activeAgents) {
            const focusEl = document.getElementById('agent-activity-focus');
            if (!focusEl) return;
            const current = chooseAgentFocus(activeAgents);
            if (!current) {
                focusEl.innerHTML = '<div class="focus-quote">暂无代理活动，最近没有新的协作输出。</div>';
                return;
            }
            const feed = activeAgents.slice(0, 4).map(item => `
                <div class="feed-item">
                    <div class="event-header">
                        <div class="event-title">${item.display_name || item.agent_id}</div>
                        <div class="event-time">${item.updated_label || '-'}</div>
                    </div>
                    <div class="event-details">${item.task_hint || '未抽取到任务提示'}<br/>${item.detail || '暂无额外输出'}</div>
                </div>
            `).join('');
            focusEl.innerHTML = `
                <div class="hero-label">当前焦点代理</div>
                        <div class="hero-title" style="font-size:18px;">${current.emoji ? current.emoji + ' ' : ''}${current.display_name || current.agent_id}</div>
                <div class="hero-subtitle">${current.agent_id || '-'} · ${current.state_label || '活动中'} · ${current.updated_label || '-'}</div>
                <div class="focus-quote">${current.task_hint || '未抽取到当前任务'}${current.detail ? `<br/><br/>${current.detail}` : ''}</div>
                <div class="focus-feed">${feed}</div>
            `;
        }

        function renderPromotionStage(statusClass, title, main, sub) {
            return `
                <div class="promotion-stage ${statusClass}">
                    <div class="promotion-stage-title">${title}</div>
                    <div class="promotion-stage-main">${main}</div>
                    <div class="promotion-stage-sub">${sub}</div>
                </div>
            `;
        }

        function renderPromotionRun(promotionSummary, run) {
            const boardEl = document.getElementById('promotion-status-board');
            if (!boardEl) return;
            const status = run && run.status ? run.status : 'idle';
            const checks = (run && run.preflight && run.preflight.checks) || [];
            const failedChecks = checks.filter(item => !item.ok).map(item => item.detail);
            const snapshots = run && run.backups ? Object.entries(run.backups).map(([k, v]) => `${k}: ${v}`).join(' | ') : '尚未创建';
            const verifyChecks = (run && run.verification && run.verification.checks) || [];
            const verifySummary = verifyChecks.length
                ? verifyChecks.map(item => `${item.name}:${item.ok ? 'OK' : 'FAIL'}`).join(' | ')
                : '尚未执行';
            const rollbackSummary = run && run.rollback
                ? `已恢复 ${run.rollback.primary_snapshot || '-'}${run.rollback.primary_head ? ` · HEAD ${run.rollback.primary_head.slice(0, 8)}` : ''}`
                : '未触发';
            const headline = run && run.status
                ? ({
                    preflight: '正在做晋升前检查',
                    backup: '已通过前检，正在建立回滚点',
                    cutover: '已完成同步，正在切换主用版',
                    promoted: '晋升完成，主用版已切到验证通过的官方版本',
                    rolled_back: '晋升失败，已自动回滚主用版',
                    failed_preflight: '晋升前检查未通过',
                }[run.status] || `当前状态：${run.status}`)
                : (promotionSummary && promotionSummary.safe_to_promote ? '官方验证版已满足晋升条件，可以开始发布到主用版' : '还没有发生正式晋升，当前显示的是准备状态');
            const updatedAt = run && run.updated_at ? formatPromotionTime(run.updated_at) : '暂无运行记录';
            const actionLabel = promotionActionInFlight ? '晋升执行中...' : '将官方验证版升级为主用版';
            boardEl.innerHTML = `
                <div class="memory-box">
                    <div class="memory-box-title">晋升执行流</div>
                    <div class="memory-box-main">${headline}</div>
                    <div class="memory-box-sub">最近更新：${updatedAt}</div>
                    <div class="promotion-actions">
                        <button class="btn btn-primary" ${promotionActionInFlight ? 'disabled' : ''} onclick="promoteOfficialToPrimary()">${actionLabel}</button>
                        <button class="btn" onclick="loadData()">刷新流程状态</button>
                    </div>
                </div>
                <div class="promotion-stage-grid">
                    ${renderPromotionStage(status === 'failed_preflight' ? 'fail' : (status !== 'idle' ? 'ok' : 'pending'), 'Preflight', checks.length ? `${checks.filter(item => item.ok).length}/${checks.length} 通过` : '待检查', failedChecks.length ? failedChecks.join(' | ') : '会检查 official 健康、阻塞任务、候选版本差异。')}
                    ${renderPromotionStage(status === 'backup' ? 'active' : (run && run.backups ? 'ok' : 'pending'), 'Backup', run && run.backups ? '回滚点已建立' : '待执行', snapshots)}
                    ${renderPromotionStage(status === 'cutover' ? 'active' : ((run && run.cutover) || status === 'promoted' || status === 'rolled_back' ? 'ok' : 'pending'), 'Cutover', run && run.cutover ? '主用版已切换启动链路' : '待执行', run && run.cutover ? (run.cutover.message || '已开始切换主用环境') : '会停止 official，启动 primary，并将守护目标切回 primary。')}
                    ${renderPromotionStage(status === 'promoted' ? 'ok' : (status === 'rolled_back' ? 'fail' : (verifyChecks.length ? 'active' : 'pending')), 'Verify', status === 'promoted' ? '自动验活通过' : (status === 'rolled_back' ? '验证失败' : '待执行'), verifySummary)}
                    ${renderPromotionStage(status === 'rolled_back' ? 'fail' : 'pending', 'Rollback', status === 'rolled_back' ? '已自动回滚' : '未触发', status === 'rolled_back' ? `${run.error || '未知错误'} | ${rollbackSummary}` : '只有在 cutover 或 verify 失败时才会触发。')}
                </div>
            `;
        }

        async function promoteOfficialToPrimary() {
            if (promotionActionInFlight) return;
            const confirmed = confirm('这会把已验证通过的 official 晋升为新的 primary，并在失败时自动回滚。现在开始吗？');
            if (!confirmed) return;
            promotionActionInFlight = true;
            const boardEl = document.getElementById('promotion-status-board');
            if (boardEl) {
                boardEl.innerHTML = '<div class="memory-box"><div class="memory-box-title">晋升执行流</div><div class="memory-box-main">正在执行 official -> primary 晋升...</div><div class="memory-box-sub">请等待备份、同步、切换与自动验活完成。</div></div>';
            }
            try {
                const res = await fetch('/api/environments/promote', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({source_env: 'official', target_env: 'primary'})
                });
                const data = await res.json();
                alert(data.message || (data.success ? '晋升完成' : '晋升失败'));
            } catch (err) {
                alert('晋升请求失败: ' + err);
            } finally {
                promotionActionInFlight = false;
                await loadData();
            }
        }
        
        async function loadData() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    document.getElementById('cpu').textContent = 'Error: ' + res.status;
                    return;
                }
                const data = await res.json();
                currentData = data;
                
                // CPU
                document.getElementById('cpu').textContent = data.metrics.cpu + '%';
                document.getElementById('cpu-sub').textContent = data.metrics.cpu > 90 ? '过高' : '正常';
                const cpuBar = document.getElementById('cpu-bar');
                cpuBar.style.width = Math.min(data.metrics.cpu, 100) + '%';
                cpuBar.className = 'progress-bar ' + (data.metrics.cpu > 90 ? 'error' : data.metrics.cpu > 70 ? 'warning' : 'good');
                
                // 内存
                document.getElementById('mem').textContent = data.metrics.mem_used + 'G';
                const memPercent = (data.metrics.mem_used / data.metrics.mem_total * 100).toFixed(0);
                const memSummary = data.memory_summary || {};
                document.getElementById('mem-sub').textContent = `${memPercent}% | Top15覆盖 ${memSummary.process_coverage_percent || 0}%`;
                const memBar = document.getElementById('mem-bar');
                memBar.style.width = memPercent + '%';
                memBar.className = 'progress-bar ' + (memPercent > 85 ? 'error' : memPercent > 70 ? 'warning' : 'good');
                
                // 会话
                document.getElementById('sessions').textContent = data.sessions.total;
                document.getElementById('sessions-sub').textContent = `慢: ${data.sessions.slow} | 卡: ${data.sessions.stuck}`;
                
                // 慢响应原因统计
                const reasonsEl = document.getElementById('slow-reasons');
                const reasons = data.sessions.reasons || {};
                if (Object.keys(reasons).length > 0) {
                    const reasonText = Object.entries(reasons).map(([k,v]) => `${k}:${v}`).join(' | ');
                    reasonsEl.textContent = reasonText;
                } else {
                    reasonsEl.textContent = '';
                }
                
                // Gateway 状态
                const statusEl = document.getElementById('gateway-status');
                if (data.gateway_healthy) {
                    statusEl.innerHTML = '<span class="status-ok">● 运行中</span>';
                } else {
                    statusEl.innerHTML = '<span class="status-error">● 异常</span>';
                }
                document.getElementById('process-pid').textContent = data.gateway_process ? 'PID: ' + data.gateway_process.pid : '未运行';

                // 版本环境
                const envSummaryEl = document.getElementById('environment-summary');
                const envWorkflowEl = document.getElementById('environment-workflow');
                const promotionSummaryEl = document.getElementById('promotion-summary');
                const envCardsEl = document.getElementById('environment-cards');
                const envs = data.environments || [];
                const activeEnv = envs.find(item => item.active) || null;
                const primaryEnv = envs.find(item => item.id === 'primary') || null;
                const officialEnv = envs.find(item => item.id === 'official') || null;
                const promotionSummary = data.promotion_summary || {};
                const promotionLastRun = data.promotion_last_run || {};
                const environmentIntegrity = data.environment_integrity || [];
                const modelFailureSummary = data.model_failure_summary || {};
                const contextReadiness = data.context_readiness || {};
                const taskSummary = (data.task_registry || {}).summary || {};
                const dualRunning = envs.filter(item => item.running).length > 1;
                const activeEnvChip = document.getElementById('global-active-env');
                const primaryChip = document.getElementById('global-primary-env');
                const officialChip = document.getElementById('global-official-env');
                const gatewayStatusPill = document.getElementById('global-gateway-status');
                const guardianStatusPill = document.getElementById('global-guardian-status');
                const taskStatsPill = document.getElementById('global-task-stats');
                const alertStatsPill = document.getElementById('global-alert-stats');
                if (activeEnvChip) activeEnvChip.innerHTML = `<strong>当前环境</strong> ${activeEnv ? activeEnv.name : '未识别'}`;
                if (primaryChip) {
                    primaryChip.classList.toggle('active', !!(primaryEnv && primaryEnv.active));
                    primaryChip.innerHTML = `<strong>Primary</strong> ${primaryEnv ? (primaryEnv.running ? (primaryEnv.healthy ? '健康' : '异常') : '未运行') : '--'}`;
                }
                if (officialChip) {
                    officialChip.classList.toggle('active', !!(officialEnv && officialEnv.active));
                    officialChip.innerHTML = `<strong>Official</strong> ${officialEnv ? (officialEnv.running ? (officialEnv.healthy ? '健康' : '异常') : '未运行') : '--'}`;
                }
                if (gatewayStatusPill) gatewayStatusPill.innerHTML = `<strong>Gateway</strong> ${data.gateway_healthy ? '运行中' : '异常'}`;
                if (guardianStatusPill) guardianStatusPill.innerHTML = `<strong>Guardian</strong> ${data.guardian_process ? '运行中' : '未检测到'}`;
                if (taskStatsPill) taskStatsPill.innerHTML = `<strong>任务</strong> 运行中 ${taskSummary.running || 0} / 阻塞 ${taskSummary.blocked || 0}`;
                if (alertStatsPill) alertStatsPill.innerHTML = `<strong>告警</strong> ${dualRunning ? '双环境同时运行' : `${(data.recent_events || []).filter(item => item.type === 'anomaly').length} 条异常`}`;
                envSummaryEl.innerHTML = activeEnv ? `
                    <div class="memory-box-title">当前守护目标</div>
                    <div class="memory-box-main">${activeEnv.name} · ${activeEnv.healthy ? '健康' : (activeEnv.running ? '异常' : '未运行')}</div>
                    <div class="memory-box-sub">env=${activeEnv.id} · 端口 ${activeEnv.port} · 版本 ${activeEnv.git_head}</div>
                    <div class="memory-box-sub" style="margin-top:6px;">代码目录: ${activeEnv.code}<br/>状态目录: ${activeEnv.home}</div>
                ` : `
                    <div class="memory-box-title">当前守护目标</div>
                    <div class="memory-box-main">未识别</div>
                    <div class="memory-box-sub">请检查版本环境配置。</div>
                `;
                envWorkflowEl.innerHTML = officialEnv ? `
                    <details class="workflow-collapsible">
                        <summary>
                            <div class="workflow-title">更新与验证流程</div>
                            <div class="workflow-main">${officialEnv.auto_update_enabled ? '官方验证版会自动更新' : '官方验证版尚未启用自动更新'}</div>
                            <div class="workflow-sub">自动更新只作用于官方验证版，不会直接覆盖当前主用版。每天 ${String(officialEnv.update_hour ?? 4).padStart(2, '0')}:${String(officialEnv.update_minute ?? 30).padStart(2, '0')} 检查并更新官方验证版代码。</div>
                        </summary>
                        <div class="workflow-collapsible-body">
                            <div class="workflow-steps">
                                <div class="workflow-step"><strong>1. 更新官方验证版</strong><br/>点击“更新官方验证版”，守护者会拉最新代码、同步隔离配置并重新构建。</div>
                                <div class="workflow-step"><strong>2. 启动并验证</strong><br/>启动官方验证版后，只让它跑在隔离端口上，先看 Dashboard、任务注册表和活跃代理是否正常。</div>
                                <div class="workflow-step"><strong>3. 切换为当前使用环境</strong><br/>验证通过后，点击“切换到这里”，守护目标就会切到官方验证版；原主用版会停止，不会双开。</div>
                                <div class="workflow-step"><strong>4. 长期稳定后再回收旧主用版</strong><br/>当前页面不会自动覆盖旧主用版代码，避免你在没验证完之前误升级生产环境。</div>
                            </div>
                        </div>
                    </details>
                    <div class="workflow-box">
                        <div class="workflow-title">当前判断</div>
                        <div class="workflow-main">${activeEnv && activeEnv.id === 'official' ? '你当前正在使用官方验证版' : '你当前仍在使用主用版'}</div>
                        <div class="workflow-sub">
                            主用版：${primaryEnv ? `${primaryEnv.running ? '运行中' : '已停止'} · ${primaryEnv.git_head}` : '-'}<br/>
                            官方验证版：${officialEnv.running ? (officialEnv.healthy ? '运行中且健康' : '运行中但异常') : '未运行'} · ${officialEnv.git_head}${officialEnv.target_head && officialEnv.target_head !== officialEnv.git_head ? ` · 可更新到 ${officialEnv.target_head}` : ''}<br/>
                            当前页面的按钮已经区分“更新”“启动”“切换使用”，不需要再去 README 里猜流程。
                        </div>
                    </div>
                ` : '';
                promotionSummaryEl.innerHTML = `
                    <div class="memory-box-title">版本晋升摘要</div>
                    <div class="memory-box-main">${promotionSummary.headline || '暂无版本晋升判断'}</div>
                    <div class="memory-box-sub">${promotionSummary.recommended_action || '-'}</div>
                    <div class="memory-box-sub" style="margin-top:6px;">${(promotionSummary.reasons || []).length ? (promotionSummary.reasons || []).join(' | ') : '当前没有额外阻断条件。'}</div>
                `;
                renderPromotionRun(promotionSummary, promotionLastRun);
                const incident = data.incident_summary || {};
                renderSimpleList('overview-environment-quick', activeEnv ? [
                    {title: `${activeEnv.name}`, body: `env=${activeEnv.id} · port=${activeEnv.port} · ${activeEnv.healthy ? '健康' : (activeEnv.running ? '异常' : '未运行')}`},
                    {title: '模式', body: dualRunning ? '双运行，需处理。' : '单活运行。'}
                ] : [], '当前没有识别到活动环境');
                renderSimpleList('overview-next-action', [
                    {title: incident.action || '继续观察', body: incident.focus || '无明显异常。'},
                    {title: promotionSummary.recommended_action || '暂无发布动作', body: promotionSummary.headline || '暂无新的版本晋升任务。'}
                ], '暂无动作');
                renderSimpleList('overview-release-quick', [
                    {title: promotionSummary.headline || '暂无版本晋升判断', body: promotionSummary.recommended_action || '继续观察。'},
                    {title: activeEnv && activeEnv.id === 'official' ? '当前使用验证版' : '当前使用主用版', body: dualRunning ? '发现双运行风险。' : '未发现双运行。'}
                ], '暂无发布摘要');
                const envAlerts = environmentIntegrity.length ? environmentIntegrity.map(item => ({title: item.title, body: item.detail})) : [];
                renderSimpleList('environment-alerts', envAlerts, '当前没有检测到环境不一致告警。');
                envCardsEl.innerHTML = envs.map(item => {
                    const switchBtn = `<button class="btn ${item.active ? 'btn-current' : 'btn-primary'}" ${item.active ? 'disabled' : ''} onclick="switchEnvironment('${item.id}')">${item.active ? '当前使用中' : '切换到这里'}</button>`;
                    let dashboardLink = '';
                    if (item.dashboard_open_link) {
                        dashboardLink = `<a class="env-link" href="${item.dashboard_open_link}" target="_blank" rel="noopener">打开 Dashboard</a>`;
                    } else if (item.running && !item.control_ui_ready) {
                        dashboardLink = '<a class="env-link disabled" href="javascript:void(0)" aria-disabled="true" title="目标环境的 Control UI 静态资源尚未构建">Control UI 未构建</a>';
                    } else if (item.running) {
                        dashboardLink = '<a class="env-link disabled" href="javascript:void(0)" aria-disabled="true" title="只有当前激活环境可以打开 Dashboard">仅当前激活环境可打开</a>';
                    } else {
                        dashboardLink = '<a class="env-link disabled" href="javascript:void(0)" aria-disabled="true" title="环境未运行，无法打开 Dashboard">环境未运行</a>';
                    }
                    const officialExtra = item.id === 'official'
                        ? `
                            <button class="btn" onclick="manageOfficial('update')">更新官方验证版</button>
                            <button class="btn" onclick="manageOfficial('${item.running ? 'stop' : 'start'}')">${item.running ? '停止官方验证版' : '启动官方验证版'}</button>
                            <button class="btn" onclick="manageOfficial('install-schedule')">${item.auto_update_enabled ? '重装自动更新' : '启用自动更新'}</button>
                        `
                        : `<button class="btn" ${item.active ? 'disabled' : ''} onclick="switchEnvironment('primary')">保持主用版</button>`;
                    const targetMeta = item.id === 'official' ? `目标版本: ${item.target_head || '-'}<br/>` : '';
                    const updateMeta = item.id === 'official' ? `<br/>自动更新: ${item.auto_update_enabled ? '已开启' : '未开启'}` : '';
                    return `
                        <div class="env-card ${item.active ? 'active' : ''}">
                            <div class="env-title-row">
                                <div class="env-title">${item.name}</div>
                                <div class="env-pill ${item.active ? 'active' : ''}">${item.active ? '当前守护中' : '可切换'}</div>
                            </div>
                            <div class="event-details">${item.description}</div>
                            <div class="env-meta">
                                env: ${item.id}<br/>
                                端口: ${item.port}<br/>
                                版本: ${item.git_head}<br/>
                                代码: ${item.code}<br/>
                                状态目录: ${item.home}<br/>
                                listener pid: ${item.listener_pid || '-'}<br/>
                                token 前缀: ${item.token_prefix || '-'}<br/>
                                ${targetMeta}
                                状态: ${item.running ? (item.healthy ? '健康' : '运行中但异常') : '未运行'}${updateMeta}
                            </div>
                            <div class="env-actions wrap">
                                ${switchBtn}
                                ${dashboardLink}
                                ${officialExtra}
                            </div>
                        </div>
                    `;
                }).join('');
                
                // 慢会话
                const tbody = document.getElementById('slow-sessions');
                tbody.innerHTML = data.sessions.sessions.length ? '' : '<tr><td colspan="5" style="text-align:center;color:#666">暂无会话</td></tr>';
                data.sessions.sessions.forEach(s => {
                    const row = document.createElement('tr');
                    row.innerHTML = `<td>${s.time}</td><td title="${s.question}">${s.question || '-'}</td><td>${s.replies || 0}条</td><td>${s.duration}s</td><td>${s.status}</td>`;
                    tbody.appendChild(row);
                });
                
                // 内存占用排行
                const memoryAttrEl = document.getElementById('memory-attribution');
                const memoryItemsEl = document.getElementById('memory-items');
                if (data.memory_summary) {
                    memoryAttrEl.innerHTML = `
                        <div class="memory-box">
                            <div class="memory-box-title">总览口径</div>
                            <div class="memory-box-main">${data.memory_summary.summary}</div>
                            <div class="memory-box-sub">${data.memory_summary.note}</div>
                        </div>
                        <div class="memory-box">
                            <div class="memory-box-title">对账结果</div>
                            <div class="memory-box-main">Top 15: ${data.memory_summary.top15_gb.toFixed(1)}G</div>
                            <div class="memory-box-sub">未归属到单进程: ${data.memory_summary.unattributed_gb.toFixed(1)}G</div>
                        </div>
                    `;
                    memoryItemsEl.innerHTML = (data.memory_summary.items || []).map(item => `
                        <div class="memory-item">
                            <div>
                                <div class="memory-item-name">${item.name}</div>
                                <div class="memory-item-note">${item.note || ''}</div>
                            </div>
                            <div class="memory-item-value">${item.value_gb.toFixed(1)}G</div>
                        </div>
                    `).join('');
                } else {
                    memoryAttrEl.innerHTML = '';
                    memoryItemsEl.innerHTML = '';
                }
                const procEl = document.getElementById('top-processes');
                procEl.innerHTML = data.top_processes && data.top_processes.length ? '' : '<tr><td colspan="5" style="text-align:center;color:#666">暂无数据</td></tr>';
                if (data.top_processes) {
                    data.top_processes.slice(0, 15).forEach(p => {
                        const row = document.createElement('tr');
                        row.innerHTML = `<td>${p.pid}</td><td>${p.user}</td><td>${p.cpu.toFixed(1)}%</td><td>${(p.mem_mb/1024).toFixed(1)} GB</td><td title="${p.cmd}">${p.cmd}</td>`;
                        procEl.appendChild(row);
                    });
                }
                
                // 诊断
                const diagEl = document.getElementById('diagnoses');
                diagEl.innerHTML = data.diagnoses.map(d => `
                    <div class="diagnose-item">
                        <span class="diagnose-icon">${d.level === 'error' ? '🔴' : d.level === 'warning' ? '🟡' : '🟢'}</span>
                        <div class="diagnose-content">
                            <div class="diagnose-title">${d.title}</div>
                            <div class="diagnose-msg">${d.message}</div>
                        </div>
                        ${d.action ? `<button class="diagnose-action" onclick="restartGateway()">${d.action}</button>` : ''}
                    </div>
                `).join('');

                // 问题定位摘要
                const incidentEl = document.getElementById('incident-summary');
                incidentEl.innerHTML = `
                    <div class="incident-card ${incident.status || 'ok'}">
                        <div class="incident-label" title="当前最主要问题">关注点</div>
                        <div class="incident-main">${incident.headline || '最近未发现明显异常'}</div>
                        <div class="incident-sub" title="详细说明">${incident.focus || '-'}</div>
                    </div>
                    <div class="incident-card">
                        <div class="incident-label" title="最近记录到的阶段">阶段</div>
                        <div class="incident-main">${incident.last_stage || '-'}</div>
                            <div class="incident-sub"></div>
                    </div>
                    <div class="incident-card">
                        <div class="incident-label" title="建议优先执行的动作">动作</div>
                        <div class="incident-main">${incident.action || '继续观察即可'}</div>
                        <div class="incident-sub" title="最近问题">${incident.last_question || '-'}</div>
                    </div>
                `;
                renderSimpleList('overview-environment-cards', envs.map(item => ({
                    title: `${item.name} · ${item.active ? '当前使用中' : '备用'}`,
                    body: `${item.running ? (item.healthy ? '健康' : '异常') : '未运行'} · ${item.git_head} · ${item.port}`
                })), '暂无环境信息');
                renderSimpleList('overview-task-cards', [
                    {title: `任务总数 ${taskSummary.total || 0}`, body: `运行中 ${taskSummary.running || 0} · 阻塞 ${taskSummary.blocked || 0} · 已完成 ${taskSummary.completed || 0}`},
                    {title: `当前会话 ${((data.task_registry || {}).session_resolution || {}).active_task_id || '暂无'}`, body: ((data.task_registry || {}).session_resolution || {}).summary || '当前没有需要会话裁决的复杂任务。'}
                ], '暂无任务摘要');
                renderSimpleList('overview-recent-events', (data.recent_events || []).slice(0, 3).map(item => ({
                    title: item.message || '未命名事件',
                    body: formatChangeDetails(item)
                })), '最近没有新的异常或进度事件');
                const failureItems = [];
                if (modelFailureSummary.primary_type && modelFailureSummary.primary_type !== 'ok') {
                    failureItems.push({title: `主失败类型：${modelFailureSummary.headline}`, body: (modelFailureSummary.items || []).map(item => `${item.label} x${item.count}`).join(' | ') || '暂无细节'});
                }
                (data.errors || []).slice(0, 2).forEach(item => failureItems.push({title: '最近错误日志', body: `${item.time} · ${item.message}`}));
                if ((data.recent_events || []).some(item => String(item.message || '').includes('无回复') || String(item.message || '').includes('WebSocket'))) {
                    const event = (data.recent_events || []).find(item => String(item.message || '').includes('无回复') || String(item.message || '').includes('WebSocket'));
                    failureItems.push({title: '最新失败边界', body: event ? `${event.message} · ${formatChangeDetails(event)}` : '最近没有模型或交付失败记录。'});
                }
                renderSimpleList('overview-failure-summary', failureItems, '最近没有明显的模型或交付失败。');
                renderSimpleList('overview-warning-list', envAlerts.length ? envAlerts : (data.diagnoses || []).slice(0, 2).map(item => ({title: item.title, body: item.message})), '当前没有需要升级处理的风险。');
                renderSimpleList('release-summary-list', [
                    {title: activeEnv && activeEnv.id === 'official' ? '当前主视角在 Official' : '当前主视角在 Primary', body: activeEnv ? `${activeEnv.code} · ${activeEnv.git_head}` : '未识别当前环境'},
                    {title: '发布判断', body: (promotionSummary.reasons || []).join(' | ') || '当前没有额外阻断条件。'}
                ], '暂无发布补充信息');

                // 任务注册表
                const taskRegistry = data.task_registry || {};
                const taskSummaryEl = document.getElementById('task-registry-summary');
                const taskListEl = document.getElementById('task-registry-list');
                const controlPlaneSummaryEl = document.getElementById('control-plane-summary');
                const controlPlaneSummarySecondaryEl = document.getElementById('control-plane-summary-secondary');
                const controlQueueBoardEl = document.getElementById('control-queue-board');
                const sessionResolutionEl = document.getElementById('session-resolution');
                const currentTask = taskRegistry.current || null;
                const controlPlane = data.control_plane || {};
                if (!taskRegistry.enabled) {
                    taskSummaryEl.innerHTML = `
                        <div class="memory-box-title">当前活动任务</div>
                        <div class="memory-box-main">未启用</div>
                        <div class="memory-box-sub">可通过 ENABLE_TASK_REGISTRY 打开任务注册表。</div>
                    `;
                    taskListEl.innerHTML = '';
                    controlQueueBoardEl.innerHTML = '<div class="event-empty">任务注册表未启用</div>';
                    if (controlPlaneSummarySecondaryEl) {
                        controlPlaneSummarySecondaryEl.innerHTML = controlPlaneSummaryEl.innerHTML;
                    }
                    sessionResolutionEl.innerHTML = `
                        <div class="memory-box-title">当前会话</div>
                        <div class="memory-box-main">未启用</div>
                        <div class="memory-box-sub">会话裁决依赖任务注册表。</div>
                    `;
                } else {
                    const summary = taskRegistry.summary || {};
                    const timeline = (currentTask && currentTask.timeline) || [];
                    const receipt = (currentTask && currentTask.receipt_summary) || {};
                    const control = (currentTask && currentTask.control) || {};
                    const controlAction = (control && control.control_action) || null;
                    const controlQueue = taskRegistry.control_queue || [];
                    const sessionResolution = taskRegistry.session_resolution || {};
                    const renderPhaseStrip = (phases) => {
                        const items = phases || [];
                        if (!items.length) return '';
                        return `<div class="phase-strip">${items.map(item => `
                            <div class="phase-pill ${item.state || 'pending'}">${item.label || item.agent}: ${item.state || 'pending'}</div>
                        `).join('')}</div>`;
                    };
                    taskSummaryEl.innerHTML = currentTask ? `
                        <div class="memory-box-title">当前活动任务</div>
                        <div class="memory-box-main">${currentTask.question || '未知任务'}</div>
                        <div class="memory-box-sub">状态: ${currentTask.status} | 阶段: ${currentTask.current_stage} | 会话: ${currentTask.session_key}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">任务总数: ${summary.total || 0} | 运行中: ${summary.running || 0} | 阻塞: ${summary.blocked || 0} | 后台: ${summary.background || 0}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">控制裁决: ${control.approved_summary || '-'} | 证据: ${control.evidence_level || '-'} | 合同: ${(control.contract || {}).id || '-'} | 对外口径: ${control.claim_level || '-'}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">证据摘要: ${control.evidence_summary || '-'} </div>
                        <div class="memory-box-sub" style="margin-top:6px;">协议: request=${(control.protocol || {}).request || '-'} | confirmed=${(control.protocol || {}).confirmed || '-'} | final=${(control.protocol || {}).final || '-'} | blocked=${(control.protocol || {}).blocked || '-'} | ack=${(control.protocol || {}).ack_id || '-'}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">下一执行人: ${control.next_actor || '-'} | 缺失回执: ${(control.missing_receipts || []).join(', ') || '无'}${controlAction ? ` | 控制动作: ${controlAction.action_type} (${controlAction.status}, attempts=${controlAction.attempts})` : ''}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">最近回执: ${receipt.agent || '-'} / ${receipt.phase || '-'} / ${receipt.action || '-'}${receipt.evidence && receipt.evidence !== '-' ? ` | ${receipt.evidence}` : ''}</div>
                        ${renderPhaseStrip(control.phase_statuses)}
                        ${timeline.length ? `<div class="memory-box-sub" style="margin-top:8px;">时间线: ${timeline.map(item => `${item.created_label} ${item.event_type}`).join(' → ')}</div>` : ''}
                        ${controlQueue.length ? `
                            <div class="control-queue">
                                ${controlQueue.slice(0, 3).map(item => `
                                    <div class="control-queue-item">
                                        <div class="control-queue-title">${item.action_type} · ${item.status}</div>
                                        <div class="control-queue-meta">task_id=${item.task_id} | state=${item.control_state} | attempts=${item.attempts}</div>
                                        <div class="control-queue-meta">${item.summary || '-'}</div>
                                    </div>
                                `).join('')}
                            </div>
                        ` : ''}
                    ` : `
                        <div class="memory-box-title">当前活动任务</div>
                        <div class="memory-box-main">暂无活动任务</div>
                        <div class="memory-box-sub">最近没有需要守护跟踪的复杂任务。</div>
                        <div class="memory-box-sub" style="margin-top:6px;">任务总数: ${summary.total || 0} | 已完成: ${summary.completed || 0} | 无可见回复: ${summary.no_reply || 0}</div>
                    `;
                    const tasks = taskRegistry.tasks || [];
                    taskListEl.innerHTML = makeExpandableList(tasks, item => `
                        <div class="event-item ${item.status === 'completed' ? 'info' : (item.status === 'blocked' ? 'warning' : 'error')}">
                            <div class="event-header">
                                <div class="event-title">${item.question || '未知任务'}</div>
                                <div class="event-time">${item.status} · ${item.current_stage}</div>
                            </div>
                            <div class="event-details">
                                task_id=${item.task_id} | session=${item.session_key}<br/>
                                最近进展时间: ${item.last_progress_label || '-'}<br/>
                                控制: ${(item.control || {}).control_state || '-'} | 证据: ${(item.control || {}).evidence_level || '-'} | 口径: ${(item.control || {}).claim_level || '-'}<br/>
                                摘要: ${(item.control || {}).evidence_summary || '-'}<br/>
                                协议: request=${((item.control || {}).protocol || {}).request || '-'} | confirmed=${((item.control || {}).protocol || {}).confirmed || '-'} | final=${((item.control || {}).protocol || {}).final || '-'} | blocked=${((item.control || {}).protocol || {}).blocked || '-'}${((item.control || {}).protocol || {}).ack_id ? `<br/>ack=${((item.control || {}).protocol || {}).ack_id}` : ''}${(item.control || {}).missing_receipts?.length ? `<br/>缺失回执: ${(item.control || {}).missing_receipts.join(', ')}` : ''}${(item.control || {}).next_actor ? `<br/>下一执行人: ${(item.control || {}).next_actor}` : ''}${item.blocked_reason ? `<br/>阻塞: ${item.blocked_reason}` : ''}
                                ${renderPhaseStrip((item.control || {}).phase_statuses)}
                            </div>
                        </div>
                    `, '<div class="event-empty">暂无任务记录</div>', { limit: 3, buttonLabel: '展开更多任务', collapseLabel: '收起任务' });
                    const actionStats = controlPlane.actions || {};
                    const taskStats = controlPlane.tasks || {};
                    controlPlaneSummaryEl.innerHTML = `
                        <div class="memory-box-title">控制面裁决</div>
                        <div class="memory-box-main">${controlPlane.headline || '暂无控制面摘要'}</div>
                        <div class="memory-box-sub">ACK 成功率: ${controlPlane.ack_success_rate || 0}% | 已验证任务: ${taskStats.verified || 0} | 待恢复: ${taskStats.recoverable || 0} | 阻塞: ${taskStats.blocked || 0}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">动作队列: pending=${actionStats.pending || 0} · sent=${actionStats.sent || 0} · blocked=${actionStats.blocked || 0} · resolved=${actionStats.resolved || 0}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">下一执行人分布: ${Object.entries(taskStats.next_actor_counts || {}).map(([k, v]) => `${k}:${v}`).join(' · ') || '暂无'}</div>
                    `;
                    if (controlPlaneSummarySecondaryEl) {
                        controlPlaneSummarySecondaryEl.innerHTML = controlPlaneSummaryEl.innerHTML;
                    }
                    controlQueueBoardEl.innerHTML = makeExpandableList(controlQueue, item => `
                        <div class="event-item ${item.status === 'blocked' ? 'warning' : (item.status === 'sent' ? 'info' : 'anomaly')}">
                            <div class="event-header">
                                <div class="event-title">${item.action_type || '-'}</div>
                                <div class="event-time">${item.status || '-'} · attempts=${item.attempts || 0}</div>
                            </div>
                            <div class="event-details">
                                task_id=${item.task_id || '-'} | state=${item.control_state || '-'}<br/>
                                ${(item.required_receipts || []).length ? `required=${item.required_receipts.join(', ')}<br/>` : ''}
                                ${(item.summary || '-')}${item.reason ? `<br/>reason=${item.reason}` : ''}${item.last_error ? `<br/>last_error=${item.last_error}` : ''}
                            </div>
                        </div>
                    `, '<div class="event-empty">当前没有待执行的控制动作</div>', { limit: 3, buttonLabel: '展开更多控制动作', collapseLabel: '收起控制动作' });
                    sessionResolutionEl.innerHTML = `
                        <div class="memory-box-title">当前会话</div>
                        <div class="memory-box-main">${sessionResolution.active_task_id || '暂无活动任务'}</div>
                        <div class="memory-box-sub">状态: ${sessionResolution.active_task_status || '-'} | 后台任务: ${sessionResolution.background_tasks || 0} | 迟到结果: ${sessionResolution.stale_results || 0}</div>
                        <div class="memory-box-sub" style="margin-top:6px;">${sessionResolution.summary || '当前没有需要会话裁决的复杂任务。'}</div>
                    `;
                }

                // 活跃代理
                const agentActivity = data.active_agents || {};
                const agentSummaryEl = document.getElementById('active-agents-summary');
                const agentListEl = document.getElementById('active-agents-list');
                const agentSummary = agentActivity.summary || {};
                const activeAgents = agentActivity.agents || [];
                if (!activeAgents.length) {
                    agentSummaryEl.innerHTML = `
                        <div class="memory-box-title">当前活跃代理</div>
                        <div class="memory-box-main">暂无活跃代理</div>
                        <div class="memory-box-sub">最近 ${Math.round((agentSummary.lookback_seconds || 1800) / 60)} 分钟内没有检测到新的 agent 会话更新。</div>
                    `;
                    agentListEl.innerHTML = '<div class="event-empty">暂无 agent 活动记录</div>';
                    renderAgentFocus([]);
                } else {
                    agentSummaryEl.innerHTML = `
                        <div class="memory-box-title">当前活跃代理</div>
                        <div class="memory-box-main">${agentSummary.active_agents || activeAgents.length} 个代理活跃</div>
                        <div class="memory-box-sub">最近 ${Math.round((agentSummary.lookback_seconds || 1800) / 60)} 分钟内，共检测到 ${agentSummary.recent_sessions || activeAgents.length} 个活跃 session。绿色高亮表示当前仍在处理中，灰化表示暂时无新动作。</div>
                    `;
                    const processingKeywords = ['正在', '启动', '派发', '等待', '回执受限', '处理中'];
                    agentListEl.innerHTML = activeAgents.map(item => `
                        <div class="agent-seat ${processingKeywords.some(keyword => (item.state_label || '').includes(keyword)) ? 'processing' : ''} ${item.agent_id === selectedAgentId ? 'active' : ''}" onclick="selectedAgentId='${item.agent_id}'; loadData()">
                            <div class="agent-seat-head">
                                <div>
                                    <div class="agent-name">${item.emoji ? item.emoji + ' ' : ''}${item.display_name || item.agent_id}</div>
                                    <div class="agent-meta">${item.updated_label || '-'} · ${item.agent_id || '-'}</div>
                                </div>
                                <div class="agent-state-pill">${item.state_label || '活动中'}</div>
                            </div>
                            <div class="agent-seat-sub">任务：${item.task_hint || '未抽取到任务提示'}<br/>${item.detail || '暂无细节'}<br/>会话文件：${item.session_file || '-'}</div>
                        </div>
                    `).join('');
                    renderAgentFocus(activeAgents);
                }

                // 反思与自进化
                const learningCenter = data.learning_center || {};
                const learningSummaryEl = document.getElementById('learning-summary');
                const learningListEl = document.getElementById('learning-list');
                const learningSummary = learningCenter.summary || {};
                const learnings = learningCenter.learnings || [];
                const suggestions = learningCenter.suggestions || [];
                const reflections = learningCenter.reflections || [];
                learningSummaryEl.innerHTML = `
                    <div class="memory-box-title">Evolution Center</div>
                    <div class="memory-box-main">待验证: ${learningSummary.pending || 0} · 已审阅: ${learningSummary.reviewed || 0} · 已升级: ${learningSummary.promoted || 0}</div>
                    <div class="memory-box-sub">最近反思: ${reflections[0] ? `${reflections[0].created_label} · promoted=${(reflections[0].summary || {}).promoted || 0}` : '暂无反思运行记录'}</div>
                `;
                const suggestionMarkup = suggestions.length ? `
                    <div class="event-item info">
                        <div class="event-header">
                            <div class="event-title">当前升级建议</div>
                            <div class="event-time">Top ${suggestions.length}</div>
                        </div>
                        <div class="event-details">
                            ${suggestions.map(item => `${item.title} · ${item.status} · x${item.occurrences} · ${item.action}`).join('<br/>')}
                        </div>
                    </div>
                ` : '';
                learningListEl.innerHTML = `${suggestionMarkup}${makeExpandableList(learnings, item => `
                    <div class="event-item ${item.status === 'promoted' ? 'info' : 'warning'}">
                        <div class="event-header">
                            <div class="event-title">${item.title || '未命名 learning'}</div>
                            <div class="event-time">${item.status} · occurrences=${item.occurrences || 0}</div>
                        </div>
                        <div class="event-details">
                            ${item.detail || '-'}<br/>
                            category=${item.category || '-'} | env=${item.env_id || '-'}${item.promoted_target ? ` | promote=${item.promoted_target}` : ''}
                        </div>
                    </div>
                `, '<div class="event-empty">暂无 learnings</div>', { limit: 3, buttonLabel: '展开更多 learnings', collapseLabel: '收起 learnings' })}`;
                const learningSuggestionsEl = document.getElementById('learning-suggestions');
                const learningReflectionsEl = document.getElementById('learning-reflections');
                if (learningSuggestionsEl) {
                    learningSuggestionsEl.innerHTML = suggestions.length ? suggestions.map(item => `
                        <div class="event-item info">
                            <div class="event-header">
                                <div class="event-title">${item.title}</div>
                                <div class="event-time">${item.status} · x${item.occurrences}</div>
                            </div>
                            <div class="event-details">${item.action || '-'}${item.detail ? `<br/>${item.detail}` : ''}</div>
                        </div>
                    `).join('') : '<div class="event-empty">当前没有新的改进建议</div>';
                }
                if (learningReflectionsEl) {
                    learningReflectionsEl.innerHTML = reflections.length ? reflections.map(item => `
                        <div class="event-item ${item.status === 'completed' ? 'info' : 'warning'}">
                            <div class="event-header">
                                <div class="event-title">${item.created_label || '未命名反思'}</div>
                                <div class="event-time">promoted=${(item.summary || {}).promoted || 0}</div>
                            </div>
                            <div class="event-details">pending=${(item.summary || {}).pending || 0} · reviewed=${(item.summary || {}).reviewed || 0}</div>
                        </div>
                    `).join('') : '<div class="event-empty">暂无反思记录</div>';
                }

                // 最近异常 / 进度
                const eventsEl = document.getElementById('recent-events');
                if (!data.recent_events || data.recent_events.length === 0) {
                    eventsEl.innerHTML = '<div class="event-item"><div class="event-title">暂无最近异常</div><div class="event-details">Guardian 已启动，但最近没有记录到异常或阶段事件。</div></div>';
                } else {
                    eventsEl.innerHTML = makeExpandableList(data.recent_events, item => {
                        const details = formatChangeDetails(item);
                        const typeLabel = item.type === 'anomaly' ? '异常' : '进度';
                        const stamp = `${item.date || ''} ${item.time || ''}`.trim();
                        return `
                            <div class="event-item ${item.type}">
                                <div class="event-meta">${typeLabel} · ${stamp || '-'}</div>
                                <div class="event-title">${item.message}</div>
                                <div class="event-details">${details}</div>
                            </div>
                        `;
                    }, '<div class="event-item"><div class="event-title">暂无最近异常</div><div class="event-details">Guardian 已启动，但最近没有记录到异常或阶段事件。</div></div>', { limit: 4, buttonLabel: '展开更多事件', collapseLabel: '收起事件' });
                }
                
                // 错误日志
                const errEl = document.getElementById('error-logs');
                errEl.innerHTML = data.errors.length ? '' : '<tr><td colspan="2" style="text-align:center;color:#666">暂无错误</td></tr>';
                data.errors.slice(0, 10).forEach(e => {
                    const row = document.createElement('tr');
                    row.innerHTML = `<td>${e.time}</td><td>${e.message}</td>`;
                    errEl.appendChild(row);
                });
                
                // 进程
                const procListEl = document.getElementById('processes');
                const procs = [
                    {name: 'Gateway', info: data.gateway_process},
                    {name: 'Guardian', info: data.guardian_process},
                ];
                procListEl.innerHTML = procs.map(p => p.info ? 
                    `<tr><td>${p.name}</td><td>${p.info.pid}</td><td>${p.info.cpu}%</td><td>${p.info.mem.toFixed(1)}%</td></tr>` :
                    `<tr><td>${p.name}</td><td colspan="3" class="status-error">未运行</td></tr>`
                ).join('');
                
                // 配置
                const autoUpdate = data.config.AUTO_UPDATE;
                document.getElementById('auto-update-toggle').checked = autoUpdate;
                document.getElementById('auto-update-status').textContent = autoUpdate ? '已开启' : '已关闭';
                document.getElementById('current-version').textContent = data.version.current || 'unknown';
                document.getElementById('version-history').textContent = data.version.history ? data.version.history.length + ' 个历史版本' : '无';
                renderSimpleList('context-readiness', (contextReadiness.checks || []).map(item => ({
                    title: `${item.ok ? '已满足' : '未满足'} · ${item.name}`,
                    body: item.detail
                })), '暂无上下文治理信息');
                renderSimpleList('system-info-summary', [
                    {title: '当前版本', body: `${data.version.current || 'unknown'} · 历史 ${data.version.history ? data.version.history.length : 0} 条`},
                    {title: '资源占用', body: `CPU ${data.metrics.cpu}% · 内存 ${data.metrics.mem_used}G / ${data.metrics.mem_total}G`}
                ], '暂无系统信息');
                
                const dingtalkWebhook = data.config.DINGTALK_WEBHOOK;
                const feishuWebhook = data.config.FEISHU_WEBHOOK;
                
                const dingtalkBtn = document.getElementById('dingtalk-btn');
                const feishuBtn = document.getElementById('feishu-btn');
                
                if (dingtalkWebhook) {
                    dingtalkBtn.textContent = '已配置';
                    dingtalkBtn.classList.add('configured');
                    document.getElementById('dingtalk-status').textContent = '已配置';
                } else {
                    dingtalkBtn.textContent = '配置';
                    dingtalkBtn.classList.remove('configured');
                    document.getElementById('dingtalk-status').textContent = '未配置';
                }
                
                if (feishuWebhook) {
                    feishuBtn.textContent = '已配置';
                    feishuBtn.classList.add('configured');
                    document.getElementById('feishu-status').textContent = '已配置';
                } else {
                    feishuBtn.textContent = '配置';
                    feishuBtn.classList.remove('configured');
                    document.getElementById('feishu-status').textContent = '未配置';
                }
                
                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            } catch(e) {
                console.error(e);
            }
        }
        
        async function restartGateway() {
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = '重启中...';
            try {
                const res = await fetch('/api/restart', {method: 'POST'});
                const data = await res.json();
                // 3秒后自动刷新数据
                setTimeout(() => {
                    loadData();
                    btn.disabled = false;
                    btn.textContent = '🔁 重启 Gateway';
                }, 3000);
            } catch(e) {
                btn.disabled = false;
                btn.textContent = '🔁 重启 Gateway';
                alert('重启请求失败');
            }
        }

        async function switchEnvironment(envId) {
            if (!confirm(`确认切换守护目标到 ${envId} 吗？这会停止另一边的 Gateway。`)) return;
            try {
                const res = await fetch('/api/environments/switch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({env_id: envId})
                });
                const data = await res.json();
                showToast(data.success ? 'success' : 'error', data.message || (data.success ? '切换成功' : '切换失败'));
                setTimeout(loadData, 2000);
            } catch (e) {
                showToast('error', e.message || '切换失败');
            }
        }

        async function manageOfficial(action) {
            const labels = {
                'prepare': '准备官方验证版',
                'start': '启动官方验证版',
                'stop': '停止官方验证版',
                'update': '更新官方验证版',
                'install-schedule': '启用官方自动更新',
                'schedule-status': '查看自动更新状态'
            };
            const label = labels[action] || action;
            try {
                const res = await fetch('/api/environments/manage', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action})
                });
                const data = await res.json();
                showToast(data.success ? 'success' : 'error', data.message || (data.success ? `${label}成功` : `${label}失败`));
                setTimeout(loadData, 2000);
            } catch (e) {
                showToast('error', e.message || `${label}失败`);
            }
        }
        
        async function emergencyRecover() {
            if (!confirm('🚨 急救模式将：\\n1. 恢复最近一次配置快照\\n2. 重新启动 Gateway\\n\\n确定要执行吗？')) return;
            try {
                const res = await fetch('/api/emergency-recover', {method: 'POST'});
                const data = await res.json();
                alert(data.message);
            } catch(e) {
                alert('急救请求失败');
            }
        }
        
        async function toggleAutoUpdate(checkbox) {
            const value = checkbox.checked;
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: 'AUTO_UPDATE', value: value})
                });
                const data = await res.json();
                if (!data.success) {
                    alert(data.message);
                    checkbox.checked = !value;
                }
            } catch(e) {
                alert('配置保存失败');
                checkbox.checked = !value;
            }
        }
        
        async function configureWebhook(type) {
            const key = type === 'DINGTALK' ? 'DINGTALK_WEBHOOK' : 'FEISHU_WEBHOOK';
            const value = prompt('请输入 ' + (type === 'DINGTALK' ? '钉钉' : '飞书') + ' Webhook URL:');
            if (value === null) return;
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: key, value: '"' + value + '"'})
                });
                const data = await res.json();
                alert(data.message);
                if (data.success) loadData();
            } catch(e) {
                alert('配置保存失败');
            }
        }
        
        loadData();
        setInterval(loadData, 5000);
        
        function switchTab(tabName, evt) {
            console.log('Switching to:', tabName);
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            evt.target.classList.add('active');
            document.getElementById('tab-' + tabName).classList.add('active');
            console.log('Tab content element:', document.getElementById('tab-' + tabName));
            if (tabName === 'recovery') {
                loadChanges();
                loadSnapshots();
            }
        }
        
        async function loadChanges() {
            console.log('Loading changes...');
            try {
                const res = await fetch('/api/changes?days=30');
                console.log('Response:', res.status);
                const data = await res.json();
                console.log('Data:', data);
                const tbody = document.getElementById('change-logs');
                if (!data.changes || data.changes.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666">暂无日志</td></tr>';
                    return;
                }
                tbody.innerHTML = data.changes.reverse().map(c => {
                    const typeIcon = {'restart': '🔁', 'config': '⚙️', 'recover': '🚨', 'update': '🔄', 'version': '📋', 'pipeline': '⏳', 'snapshot': '📦', 'anomaly': '🚨'}[c.type] || '📝';
                    return `<tr><td>${c.date || ''}</td><td>${c.time}</td><td>${typeIcon} ${c.type}</td><td>${c.message}</td><td>${formatChangeDetails(c)}</td></tr>`;
                }).join('');
            } catch(e) {
                console.error(e);
            }
        }

        function formatChangeDetails(change) {
            const details = change.details || {};
            if (change.type === 'pipeline') {
                return `阶段: ${details.marker || '-'} | 时间: ${details.timestamp || '-'}`;
            }
            if (change.type === 'anomaly') {
                const marker = details.marker ? ` | 阶段: ${details.marker}` : '';
                return `问题: ${details.question || '-'} | 耗时: ${details.duration || '-'}秒${marker} | 时间: ${details.timestamp || '-'}`;
            }
            if (change.type === 'restart') {
                return `PID: ${details.old_pid || '-'} -> ${details.new_pid || '-'}`;
            }
            if (change.type === 'recover') {
                return `快照: ${details.snapshot || '-'} `;
            }
            if (change.type === 'version') {
                if (details.from && details.to) return `${details.from} -> ${details.to}`;
                return details.commit || '-';
            }
            if (change.type === 'snapshot') {
                return `快照: ${details.snapshot || '-'} `;
            }
            return JSON.stringify(details);
        }

        async function loadSnapshots() {
            try {
                const res = await fetch('/api/snapshots');
                const data = await res.json();
                const tbody = document.getElementById('snapshot-logs');
                if (!data.snapshots || data.snapshots.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666">暂无快照</td></tr>';
                    return;
                }
                tbody.innerHTML = data.snapshots.map(s => {
                    const created = s.created_at ? new Date(s.created_at).toLocaleString() : '-';
                    return `<tr><td>${s.name}</td><td>${s.label || '-'}</td><td>${created}</td><td>${s.file_count}</td><td><button class="btn" onclick="restoreSnapshot('${s.name}')">恢复</button></td></tr>`;
                }).join('');
            } catch (e) {
                console.error(e);
            }
        }

        async function captureSnapshot() {
            const label = prompt('请输入快照标签:', 'manual');
            if (label === null) return;
            try {
                const res = await fetch('/api/snapshots', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({label})
                });
                const data = await res.json();
                alert(data.message);
                if (data.success) loadSnapshots();
            } catch (e) {
                alert('创建快照失败');
            }
        }

        async function restoreSnapshot(name) {
            if (!confirm('将恢复选中的配置快照，并发起 Gateway 重启。确定继续吗？')) return;
            try {
                const res = await fetch('/api/snapshots/restore', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name})
                });
                const data = await res.json();
                alert(data.message);
                if (data.success) loadSnapshots();
            } catch (e) {
                alert('恢复快照失败');
            }
        }
    </script>
    <script>
        window.onerror = function(msg, url, line) {
            document.body.innerHTML += '<div style="background:red;padding:10px;color:white">JS Error: ' + msg + ' line:' + line + '</div>';
        };
    </script>
</body>
</html>
'''
    return render_template_string(html)


@app.route("/api/status")
def api_status():
    """获取状态 API"""
    config = load_config()
    selected_env = env_spec(active_env_id(config), config)
    metrics = get_system_metrics()
    gateway_process = get_gateway_process_for_env(selected_env)
    guardian_process = get_guardian_process_info()
    gateway_healthy = check_gateway_health_for_env(selected_env)
    sessions = analyze_sessions(5, selected_env)
    errors = get_error_logs(20, selected_env)
    version = get_version(selected_env)
    safe_config = sanitize_config_for_ui(config)
    version_history = load_versions()
    diagnoses = get_diagnoses(metrics, sessions, [gateway_process, guardian_process])
    top_processes = get_top_processes(15)
    recent_events = get_recent_anomalies(limit=8, days=7)
    incident_summary = build_incident_summary(recent_events)
    memory_summary = summarize_memory_usage(metrics, top_processes)
    environments = list_openclaw_environments(config)
    task_registry = get_task_registry_payload(limit=8)
    active_agents = get_active_agent_activity(selected_env, config)
    learning_center = get_learning_center_payload(limit=10)
    promotion_summary = build_environment_promotion_summary(environments, task_registry)
    promotion_last_run = STORE.load_runtime_value("promotion_last_run", {})
    control_plane = get_control_plane_overview(selected_env["id"])
    environment_integrity = detect_environment_inconsistencies(environments, selected_env["id"])
    for issue in environment_integrity:
        diagnoses.insert(
            0,
            {
                "level": "error" if issue.get("severity") == "error" else "warning",
                "title": str(issue.get("title") or "环境状态异常"),
                "message": str(issue.get("detail") or "active env 与 listener 不一致"),
                "action": "检查环境切换",
            },
        )
    model_failure_summary = build_model_failure_summary(errors, recent_events)
    if model_failure_summary.get("primary_type") not in {"", "ok"}:
        diagnoses.insert(
            0,
            {
                "level": "warning",
                "title": f"最新失败层级：{model_failure_summary.get('headline')}",
                "message": " | ".join(
                    f"{item.get('label')} x{item.get('count')}"
                    for item in (model_failure_summary.get("items") or [])[:3]
                )
                or "最近存在模型或交付失败。",
                "action": "查看异常排查",
            },
        )
    context_readiness = build_context_lifecycle_readiness(config)
    
    data = {
        "active_environment": selected_env["id"],
        "environments": environments,
        "environment_integrity": environment_integrity,
        "metrics": metrics,
        "gateway_process": gateway_process,
        "guardian_process": guardian_process,
        "gateway_healthy": gateway_healthy,
        "sessions": sessions,
        "errors": errors,
        "model_failure_summary": model_failure_summary,
        "version": {"current": version, "history": version_history.get("history", [])},
        "config": safe_config,
        "diagnoses": diagnoses,
        "top_processes": top_processes,
        "recent_events": recent_events,
        "incident_summary": incident_summary,
        "memory_summary": memory_summary,
        "task_registry": task_registry,
        "active_agents": active_agents,
        "learning_center": learning_center,
        "control_plane": control_plane,
        "promotion_summary": promotion_summary,
        "promotion_last_run": promotion_last_run,
        "context_readiness": context_readiness,
    }
    return app.response_class(
        response=json.dumps(data, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route("/api/task-registry")
def api_task_registry():
    """Return a focused task-registry payload for external consumers."""
    payload = get_task_registry_payload(limit=20)
    return app.response_class(
        response=json.dumps(payload, ensure_ascii=False),
        mimetype="application/json",
    )


@app.route("/api/learnings")
def api_learnings():
    payload = get_learning_center_payload(limit=20)
    return app.response_class(
        response=json.dumps(payload, ensure_ascii=False),
        mimetype="application/json",
    )


@app.route("/api/shared-state")
def api_shared_state():
    payload = build_shared_state_snapshot(load_config())
    return app.response_class(
        response=json.dumps(payload, ensure_ascii=False),
        mimetype="application/json",
    )


@app.route("/api/environments/switch", methods=["POST"])
def api_switch_environment():
    """切换当前守护目标环境。"""
    try:
        data = request.get_json(silent=True) or {}
        env_id = str(data.get("env_id", "")).strip()
        success, message = switch_openclaw_environment(env_id)
        if success:
            record_change("version", f"切换守护环境到 {env_id}", {"to": env_id})
        return jsonify({"success": success, "message": message, "env_id": env_id})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/environments/manage", methods=["POST"])
def api_manage_environment():
    """Manage official validation environment lifecycle and updates."""
    try:
        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).strip()
        success, message = manage_official_environment(action)
        if success:
            record_change("version", f"官方验证版操作: {action}", {"action": action})
        return jsonify({"success": success, "message": message, "action": action})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/environments/promote", methods=["POST"])
def api_promote_environment():
    """Promote validated official environment into primary."""
    try:
        data = request.get_json(silent=True) or {}
        source_env = str(data.get("source_env", "official")).strip() or "official"
        target_env = str(data.get("target_env", "primary")).strip() or "primary"
        if source_env != "official" or target_env != "primary":
            return jsonify({"success": False, "message": "当前只支持 official -> primary 的版本晋升"})
        result = execute_official_promotion()
        status = result.get("status", "unknown")
        success = status == "promoted"
        if status == "failed_preflight":
            failed = [item.get("detail", "") for item in (result.get("preflight") or {}).get("checks", []) if not item.get("ok")]
            message = "晋升前检查未通过：" + (" | ".join(failed) if failed else "请查看流程状态")
        elif status == "rolled_back":
            message = f"晋升失败，已自动回滚：{result.get('error', '未知错误')}"
        elif status == "promoted":
            message = "官方验证版已晋升为当前主用版，并通过自动验活"
        else:
            message = f"晋升流程结束：{status}"
        return jsonify({"success": success, "message": message, "result": result, "status": status})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/open-dashboard/<env_id>")
def open_dashboard(env_id: str):
    """Open the target OpenClaw dashboard through a server-side redirect."""
    config = load_config()
    spec = env_spec(env_id, config)
    if spec["id"] != env_id:
        return "Unknown environment", 404
    if active_env_id(config) != env_id:
        return "Environment is not active", 409
    if get_listener_pid(int(spec["port"])) is None:
        return "Environment is not running", 409
    if not env_has_control_ui_assets(spec):
        return "Control UI assets are not built for this environment", 409
    return redirect(env_dashboard_url(spec), code=302)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """重启 Gateway"""
    try:
        ok, message, old_pid_str, new_pid, target_env = restart_active_openclaw_environment()

        if ok and new_pid and new_pid != old_pid_str:
            record_change(
                "restart",
                f"{env_spec(target_env)['name']} 重启成功 (PID: {old_pid_str} → {new_pid})",
                {"old_pid": old_pid_str, "new_pid": new_pid, "env_id": target_env},
            )
            return jsonify({
                "success": True,
                "message": f"{env_spec(target_env)['name']} 已重启\n旧PID: {old_pid_str or '无'}\n新PID: {new_pid}",
                "old_pid": old_pid_str,
                "new_pid": new_pid,
                "env_id": target_env,
            })
        elif ok and new_pid:
            return jsonify({
                "success": True,
                "message": f"{env_spec(target_env)['name']} 正在运行 (PID: {new_pid})",
                "new_pid": new_pid,
                "env_id": target_env,
            })
        else:
            return jsonify({"success": False, "message": message, "env_id": target_env})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/emergency-recover", methods=["POST"])
def api_emergency_recover():
    """急救：恢复最近一次配置快照并重启。"""
    try:
        config = load_config()
        if not config.get("ENABLE_SNAPSHOT_RECOVERY", True):
            return jsonify({
                "success": False,
                "message": "当前已禁用 snapshot recovery。请先在本地配置中显式开启 ENABLE_SNAPSHOT_RECOVERY=true。"
            })

        snapshot_dir = SNAPSHOTS.restore_latest_snapshot()
        if snapshot_dir is None:
            return jsonify({"success": False, "message": "没有可恢复的配置快照"})

        old_pid = get_listener_pid()
        if old_pid is not None:
            subprocess.run(f"kill {old_pid}", shell=True, check=False)
            time.sleep(2)

        with open(BASE_DIR / "logs" / "guardian.log", "a") as log_handle:
            subprocess.Popen(
                ["openclaw", "gateway", "run"],
                cwd=str(OPENCLAW_CODE),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

        record_change("recover", f"恢复配置快照并重启: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
        return jsonify({"success": True, "message": f"已恢复配置快照并发起重启: {snapshot_dir.name}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/changes")
def api_changes():
    """获取变更日志"""
    days = request.args.get("days", 7, type=int)
    changes = get_recent_changes(days)
    return jsonify({"changes": changes})


@app.route("/api/snapshots")
def api_snapshots():
    """获取配置快照列表。"""
    return jsonify({"snapshots": list_snapshots()})


@app.route("/api/snapshots", methods=["POST"])
def api_snapshot_create():
    """手动创建配置快照。"""
    try:
        data = request.get_json(silent=True) or {}
        label = str(data.get("label", "manual")).strip() or "manual"
        snapshot_dirs = create_config_snapshots(label)
        if not snapshot_dirs:
            return jsonify({"success": False, "message": "没有可快照的配置文件"})
        names = [path.name for path in snapshot_dirs]
        record_change("snapshot", "手动创建配置快照", {"snapshots": names})
        return jsonify({"success": True, "message": f"已创建配置快照: {', '.join(names)}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/snapshots/restore", methods=["POST"])
def api_snapshot_restore():
    """恢复指定快照并发起重启。"""
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"success": False, "message": "缺少快照名称"})

        snapshot_dir = SNAPSHOTS.snapshot_root / name
        if not snapshot_dir.exists() or not snapshot_dir.is_dir():
            return jsonify({"success": False, "message": "快照不存在"})

        SNAPSHOTS.restore_snapshot(snapshot_dir)

        old_pid = get_listener_pid()
        if old_pid is not None:
            subprocess.run(f"kill {old_pid}", shell=True, check=False)
            time.sleep(2)

        with open(BASE_DIR / "logs" / "guardian.log", "a") as log_handle:
            subprocess.Popen(
                ["openclaw", "gateway", "run"],
                cwd=str(OPENCLAW_CODE),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

        record_change("recover", f"恢复指定配置快照并重启: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
        return jsonify({"success": True, "message": f"已恢复配置快照并发起重启: {snapshot_dir.name}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/config", methods=["POST"])
def api_config():
    """更新配置"""
    try:
        data = request.get_json()
        key = data.get("key")
        value = data.get("value")
        
        if not key:
            return jsonify({"success": False, "message": "缺少配置键"})
        
        if save_config(key, str(value)):
            return jsonify({"success": True, "message": "配置已更新"})
        else:
            return jsonify({"success": False, "message": "保存配置失败"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


if __name__ == "__main__":
    import socket
    raise_nofile_limit()
    
    def find_free_port(start=8080):
        for port in range(start, start + 10):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("", port))
                sock.close()
                return port
            except:
                continue
        return 8080

    requested_port = os.environ.get("DASHBOARD_PORT", "").strip()
    if requested_port.isdigit():
        port = int(requested_port)
    else:
        port = find_free_port()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    print("=" * 50)
    print("OpenClaw 健康监控中心")
    print(f"访问: http://{host}:{port}")
    print("=" * 50)
    app.run(host=host, port=port, debug=False)
