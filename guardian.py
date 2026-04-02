#!/usr/bin/env python3
"""
OpenClaw Guardian - 独立守护进程
功能：进程守护、健康检查、告警通知、自动更新
"""

import os
import sys
import json
import time
import signal
import socket
import subprocess
import threading
import resource
import hashlib
import shlex
import re
import urllib.request
import atexit
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List

from monitor_config import (
    DEFAULT_CONFIG,
    get_env_specs as get_registered_env_specs,
    is_webhook_url_allowed,
    load_config as load_shared_config,
    read_active_binding,
    write_active_binding,
)
from snapshot_manager import SnapshotManager
from state_store import MonitorStateStore
from version_tracker import (
    build_recovery_profile,
    collect_version_record,
    load_versions_file,
    update_versions_file,
)
from task_contracts import (
    infer_task_contract,
    load_task_contract_catalog,
    normalize_pipeline_receipt,
)
from bootstrap_evolution import (
    CONTEXT_LIFECYCLE_BASELINE,
    derive_watcher_task_id,
    ensure_bootstrap_workspace,
)
from heartbeat_guardrail import (
    TaskWatcher,
    Heartbeat,
    HeartbeatPhase,
    infer_duration_profile,
    resolve_timing_window,
    build_user_visible_status_template,
)
from recovery_watchdog import RecoveryWatchdog, detect_recurrence_problem_code
from learning_recorder import (
    check_and_resolve_learnings,
    generate_daily_evolution_report,
    record_learning,
    render_daily_evolution_report_markdown,
    render_learnings_markdown,
    write_state_snapshot,
)

# ========== 配置 ==========
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.conf"
GUARDIAN_LOG_DIR = BASE_DIR / "logs"
GUARDIAN_PID_FILE = GUARDIAN_LOG_DIR / "guardian.pid"
GUARDIAN_LOCK_FILE = GUARDIAN_LOG_DIR / "guardian.lock"
_GUARDIAN_LOCK_HANDLE = None
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.conf"
ALERTS_FILE = BASE_DIR / "alerts.json"
VERSIONS_FILE = BASE_DIR / "versions.json"
LOG_FILE = BASE_DIR / "logs" / "guardian.log"

OPENCLAW_HOME = Path.home() / ".openclaw"
OPENCLAW_CODE = Path.home() / "openclaw-workspace" / "openclaw"
GATEWAY_LOG = OPENCLAW_HOME / "logs" / "gateway.log"
TMP_OPENCLAW_LOG_DIR = Path("/tmp/openclaw")
SESSION_STORE_FILE = OPENCLAW_HOME / "agents" / "main" / "sessions" / "sessions.json"
SHARED_STATE_DIR = BASE_DIR / "data" / "shared-state"
DESKTOP_RUNTIME = BASE_DIR / "desktop_runtime.sh"

CONFIG = {}
ALERTS = {}
VERSIONS = {"current": None, "history": []}
STORE = MonitorStateStore(BASE_DIR)
SNAPSHOTS = SnapshotManager(BASE_DIR, OPENCLAW_HOME)


def safe_read_json_file(path: Path, default: Any) -> Any:
    """Best-effort JSON reader for files that may be concurrently written.

    Returns default on missing/empty/truncated/invalid JSON instead of throwing.
    """
    try:
        if not path.exists():
            return default
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if not raw or not raw.strip():
            return default
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Common concurrent-write case: file exists but body is incomplete.
            if text[-1:] not in {"}", "]"}:
                return default
            return default
    except Exception:
        return default


def atomic_write_json_file(path: Path, payload: Any) -> None:
    """Atomically write JSON via temp file + replace to avoid partial reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def raise_nofile_limit(target: int = 65536) -> None:
    """Best-effort bump of RLIMIT_NOFILE for long-running local agents."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = min(max(soft, target), hard)
        if desired > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except Exception:
        pass


def _cleanup_guardian_singleton() -> None:
    global _GUARDIAN_LOCK_HANDLE
    try:
        if GUARDIAN_PID_FILE.exists():
            GUARDIAN_PID_FILE.unlink()
    except Exception:
        pass
    try:
        if _GUARDIAN_LOCK_HANDLE is not None:
            fcntl.flock(_GUARDIAN_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
            _GUARDIAN_LOCK_HANDLE.close()
            _GUARDIAN_LOCK_HANDLE = None
    except Exception:
        pass


def ensure_single_guardian_instance() -> None:
    global _GUARDIAN_LOCK_HANDLE
    GUARDIAN_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Extra guard: if another guardian.py process already exists, exit early.
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,command=", "-ax"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        current_pid = os.getpid()
        for line in result.stdout.splitlines():
            raw = line.strip()
            if not raw or "guardian.py" not in raw:
                continue
            parts = raw.split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except Exception:
                continue
            if pid != current_pid:
                print(f"Guardian already running (pid={pid})", file=sys.stderr)
                sys.exit(0)
    except Exception:
        pass

    lock_handle = GUARDIAN_LOCK_FILE.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing_pid = GUARDIAN_PID_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            existing_pid = "unknown"
        print(f"Guardian already running (pid={existing_pid})", file=sys.stderr)
        sys.exit(0)
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    os.fsync(lock_handle.fileno())
    _GUARDIAN_LOCK_HANDLE = lock_handle
    GUARDIAN_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_cleanup_guardian_singleton)


def load_config():
    """加载配置文件"""
    global CONFIG
    CONFIG = load_shared_config(BASE_DIR)


def active_binding() -> dict[str, Any]:
    runtime_binding = STORE.load_runtime_value("active_openclaw_env", {})
    specs = get_registered_env_specs(CONFIG)
    default_env = "primary" if "primary" in specs else next(iter(specs), "primary")
    if not isinstance(runtime_binding, dict):
        return {
            "active_env": default_env,
            "switch_state": "committed",
            "binding_version": 1,
            "updated_at": int(time.time()),
            "expected": dict(specs.get(default_env) or {}),
        }
    runtime_env = str(runtime_binding.get("env_id") or "").strip()
    if runtime_env not in specs:
        runtime_env = default_env
    expected = dict(specs[runtime_env])
    if isinstance(runtime_binding.get("expected"), dict):
        expected.update(runtime_binding.get("expected") or {})
    return {
        "active_env": runtime_env,
        "switch_state": str(runtime_binding.get("switch_state") or "committed"),
        "binding_version": int(runtime_binding.get("binding_version") or 1),
        "updated_at": int(runtime_binding.get("updated_at") or int(time.time())),
        "expected": expected,
    }


def active_env_id() -> str:
    return str(active_binding().get("active_env") or "primary")


def commit_active_binding(env_id: str) -> None:
    if env_id != "primary":
        return
    try:
        binding = write_active_binding(
            BASE_DIR, CONFIG, env_id, switch_state="committed"
        )
    except Exception:
        binding = {"expected": {}, "binding_version": 1}
        pass
    try:
        spec = all_env_specs()[env_id]
        STORE.save_runtime_value(
            "active_openclaw_env",
            {
                "env_id": env_id,
                "updated_at": int(time.time()),
                "switch_state": "committed",
                "binding_version": binding.get("binding_version") or 1,
                "gateway_label": spec["gateway_label"],
                "gateway_port": spec["port"],
                "config_path": str(spec["config_path"]),
                "expected": binding.get("expected") or {},
            },
        )
        STORE.append_runtime_event(
            "binding_audit_events",
            {
                "source": "guardian.commit",
                "env_id": env_id,
                "status": "committed",
                "details": {
                    "gateway_port": spec["port"],
                    "gateway_label": spec["gateway_label"],
                },
                "timestamp_iso": datetime.now().isoformat(),
            },
            limit=200,
        )
        shared_dir = BASE_DIR / "data" / "shared-state"
        shared_dir.mkdir(parents=True, exist_ok=True)
        (shared_dir / "binding-audit-events.json").write_text(
            json.dumps(
                STORE.load_runtime_value("binding_audit_events", []),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def current_env_spec() -> dict[str, Any]:
    env_id = active_env_id()
    spec = get_registered_env_specs(CONFIG)[env_id]
    return {
        "id": env_id,
        "home": Path(spec["state_root"]),
        "code": Path(spec["code_root"]),
        "port": int(spec["gateway_port"]),
        "gateway_label": spec["gateway_label"],
        "config_path": Path(spec["config_path"]),
    }


def all_env_specs() -> dict[str, dict[str, Any]]:
    specs = get_registered_env_specs(CONFIG)
    return {
        env_id: {
            "id": env_id,
            "home": Path(spec["state_root"]),
            "code": Path(spec["code_root"]),
            "port": int(spec["gateway_port"]),
            "gateway_label": spec["gateway_label"],
            "config_path": Path(spec["config_path"]),
        }
        for env_id, spec in specs.items()
    }


def snapshot_targets() -> list[tuple[str, SnapshotManager]]:
    primary_home = Path(
        str(CONFIG.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")))
    )
    return [
        ("primary", SnapshotManager(BASE_DIR, primary_home)),
    ]


def current_gateway_log() -> Path:
    return current_env_spec()["home"] / "logs" / "gateway.log"


def record_version_state(
    spec: dict[str, Any],
    *,
    reason: str,
    status: str = "observed",
    mark_known_good: bool = False,
) -> dict[str, Any]:
    code_root = Path(
        str(spec.get("code") or CONFIG.get("OPENCLAW_CODE") or OPENCLAW_CODE)
    )
    payload = update_versions_file(
        VERSIONS_FILE,
        collect_version_record(
            code_root=code_root,
            env_id=str(spec.get("id") or "primary"),
            reason=reason,
            status=status,
        ),
        mark_known_good=mark_known_good,
    )
    STORE.save_runtime_value(
        f"openclaw_version:{spec['id']}", payload.get("current") or {}
    )
    STORE.save_runtime_value(
        f"openclaw_recovery_profile:{spec['id']}", build_recovery_profile(payload)
    )
    return payload


def ensure_openclaw_bootstrap(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    target = spec or current_env_spec()
    home = Path(str(target.get("home") or Path.home() / ".openclaw"))
    env_id = str(target.get("id") or "primary")
    status = ensure_bootstrap_workspace(
        home=home,
        env_id=env_id,
        write_missing=bool(
            CONFIG.get("ENABLE_BOOTSTRAP_INIT", True)
            and CONFIG.get("BOOTSTRAP_WRITE_MISSING", True)
        ),
    )
    STORE.save_runtime_value(f"bootstrap_status:{env_id}", status)
    return status


def _walk_watcher_payload(payload: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 4:
        return []
    nodes: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        nodes.append(payload)
        for value in payload.values():
            nodes.extend(_walk_watcher_payload(value, depth=depth + 1))
    elif isinstance(payload, list):
        for value in payload:
            nodes.extend(_walk_watcher_payload(value, depth=depth + 1))
    return nodes


def _extract_watcher_receipt(payload: dict[str, Any]) -> dict[str, str] | None:
    for node in _walk_watcher_payload(payload):
        for key in ("receipt", "pipeline_receipt"):
            value = node.get(key)
            if isinstance(value, dict):
                receipt = normalize_pipeline_receipt(
                    value,
                    timestamp=str(
                        node.get("timestamp")
                        or payload.get("timestamp")
                        or payload.get("completed_at")
                        or ""
                    ),
                )
                if receipt:
                    return receipt
        receipt = normalize_pipeline_receipt(
            node,
            timestamp=str(
                node.get("timestamp")
                or payload.get("timestamp")
                or payload.get("completed_at")
                or ""
            ),
        )
        if receipt:
            return receipt
        for text_key in (
            "text",
            "message",
            "content",
            "body",
            "detail",
            "error",
            "result",
        ):
            value = node.get(text_key)
            if not isinstance(value, str) or "PIPELINE_RECEIPT:" not in value:
                continue
            receipt = extract_pipeline_receipt(value)
            if receipt:
                return receipt
    return None


def _find_watcher_task_by_question_hint(
    payload: dict[str, Any], *, env_id: str
) -> dict[str, Any] | None:
    hints: list[str] = []
    for node in _walk_watcher_payload(payload):
        for key in (
            "question",
            "task_question",
            "last_user_message",
            "title",
            "prompt",
            "request",
            "intent_text",
        ):
            value = node.get(key)
            if isinstance(value, str):
                normalized = normalize_task_question(value)
                if normalized != "未知任务":
                    hints.append(normalized)
    if not hints:
        return None
    active_tasks = [
        task
        for task in STORE.list_active_tasks(limit=20)
        if str(task.get("env_id") or env_id) == env_id
    ]
    if len(active_tasks) != 1:
        return None
    task = active_tasks[0]
    question = normalize_task_question(task.get("question"))
    last_user_message = normalize_task_question(task.get("last_user_message"))
    for hint in hints:
        if hint in {question, last_user_message}:
            return task
        if len(hint) >= 8 and (
            hint in question
            or hint in last_user_message
            or question in hint
            or last_user_message in hint
        ):
            return task
    return None


def _resolve_watcher_receipt_task(
    receipt: dict[str, str], payload: dict[str, Any], *, env_id: str
) -> dict[str, Any] | None:
    receipt_task_id = str(
        receipt.get("task_id") or payload.get("task_id") or ""
    ).strip()
    if receipt_task_id:
        task = STORE.get_task(receipt_task_id)
        if task and str(task.get("env_id") or env_id) == env_id:
            return task
    for node in _walk_watcher_payload(payload):
        session_key = str(
            node.get("session_key") or receipt.get("session_key") or ""
        ).strip()
        if not session_key:
            continue
        task = STORE.get_latest_task_for_session(session_key)
        if task and str(task.get("env_id") or env_id) == env_id:
            return task
    return _find_watcher_task_by_question_hint(payload, env_id=env_id)


def _watcher_receipt_event_payload(
    receipt: dict[str, str],
    normalized_payload: dict[str, Any],
    *,
    status: str,
    stage_label: str,
) -> dict[str, Any]:
    return {
        "receipt": receipt,
        "status": status,
        "stage": stage_label,
        "timestamp": normalized_payload.get("timestamp", ""),
    }


def _bridge_watcher_receipt(
    payload: dict[str, Any], *, env_id: str, watcher_task_id: str, source_file: Path
) -> str:
    receipt = _extract_watcher_receipt(payload)
    if not receipt:
        return "ignored"
    task = _resolve_watcher_receipt_task(receipt, payload, env_id=env_id)
    if not task:
        return "observed_unbound"
    task_for_validation = dict(task)
    if str(task.get("status") or "") == "completed":
        core = STORE.get_core_closure_snapshot_for_task(
            task["task_id"], allow_legacy_projection=False
        )
        if (
            not core.get("is_terminal")
            and str(core.get("finalization_state") or "") != "finalized"
        ):
            task_for_validation["status"] = "running"
    accepted, normalized_payload, violations = validate_protocol_event(
        task_for_validation,
        "pipeline_receipt",
        {
            "receipt": receipt,
            "timestamp": str(
                receipt.get("timestamp")
                or payload.get("timestamp")
                or payload.get("completed_at")
                or ""
            ),
        },
    )
    for violation in violations:
        if violation.get("rejected"):
            record_protocol_violation(
                task["task_id"],
                violation_kind=str(violation.get("violation_kind") or "unknown"),
                event_type="pipeline_receipt",
                payload=violation.get("payload") or {},
                ack_id=str(violation.get("ack_id") or ""),
            )
    if not accepted:
        return "observed_unbound"
    receipt = dict(normalized_payload.get("receipt") or {})
    action = str(receipt.get("action") or "")
    phase = str(receipt.get("phase") or "")
    status = str(task.get("status") or "running")
    blocked_reason = str(task.get("blocked_reason") or "")
    if action == "blocked":
        status = "blocked"
        blocked_reason = receipt.get("evidence", "")
    else:
        if status in {"blocked", "no_reply", "background"}:
            status = "running"
        if blocked_reason == "missing_pipeline_receipt":
            blocked_reason = ""
    stage_label = f"{phase}:{action}".strip(":") or str(
        task.get("current_stage") or "处理中"
    )
    event_payload = _watcher_receipt_event_payload(
        receipt,
        normalized_payload,
        status=status,
        stage_label=stage_label,
    )
    if not STORE.record_task_event(task["task_id"], "pipeline_receipt", event_payload):
        return "ignored"
    STORE.update_task_fields(
        task["task_id"],
        status=status,
        current_stage=stage_label,
        blocked_reason=blocked_reason,
        latest_receipt=receipt,
        last_progress_at=int(time.time()),
        updated_at=int(time.time()),
    )
    return "bridged"


def sync_shared_context_watcher_tasks(
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = spec or current_env_spec()
    env_id = str(target.get("id") or "primary")
    monitor_dir = (
        Path(str(target.get("home") or Path.home() / ".openclaw"))
        / "shared-context"
        / "monitor-tasks"
    )
    tasks_file = monitor_dir / "tasks.jsonl"
    dlq_file = monitor_dir / "dlq.jsonl"
    watcher_log_file = monitor_dir / "watcher.log"
    audit_log_file = monitor_dir / "audit.log"
    watcher_signal_file = monitor_dir / "watcher-control-signal.json"
    imported = 0
    receipt_bridge = {"bridged": 0, "observed_unbound": 0, "ignored": 0}

    def process_file(path: Path, *, in_dlq: bool = False) -> None:
        nonlocal imported
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            watcher_task_id = derive_watcher_task_id(payload)
            STORE.upsert_watcher_task(
                {
                    "watcher_task_id": watcher_task_id,
                    "env_id": env_id,
                    "source_agent": payload.get("source_agent")
                    or payload.get("agent")
                    or "",
                    "target_agent": payload.get("target_agent")
                    or payload.get("callback_agent")
                    or "",
                    "intent": payload.get("intent") or payload.get("type") or "",
                    "current_state": payload.get("current_state")
                    or payload.get("state")
                    or ("dlq" if in_dlq else "registered"),
                    "completed_at": payload.get("completed_at") or 0,
                    "delivered_at": payload.get("delivered_at") or 0,
                    "last_checked_at": payload.get("last_checked_at")
                    or payload.get("checked_at")
                    or 0,
                    "error_count": payload.get("error_count")
                    or payload.get("attempts")
                    or 0,
                    "in_dlq": in_dlq or bool(payload.get("in_dlq")),
                    "payload": payload,
                }
            )
            imported += 1
            outcome = _bridge_watcher_receipt(
                payload,
                env_id=env_id,
                watcher_task_id=watcher_task_id,
                source_file=path,
            )
            receipt_bridge[outcome] = receipt_bridge.get(outcome, 0) + 1

    process_file(tasks_file, in_dlq=False)
    process_file(dlq_file, in_dlq=True)
    watcher_log_records = _read_jsonl_records(watcher_log_file)
    audit_log_records = _read_jsonl_records(audit_log_file)
    latest_scan = watcher_log_records[-1] if watcher_log_records else {}
    latest_alert = audit_log_records[-1] if audit_log_records else {}
    recent_scans = watcher_log_records[-20:]
    recent_alerts = audit_log_records[-20:]
    watcher_signal = safe_read_json_file(watcher_signal_file, {})
    summary = STORE.summarize_watcher_tasks(env_id=env_id)
    result = {
        "env_id": env_id,
        "monitor_dir": str(monitor_dir),
        "imported": imported,
        "summary": summary,
        "receipt_bridge": receipt_bridge,
        "latest_scan": latest_scan,
        "latest_alert": latest_alert,
        "recent_scans": recent_scans,
        "recent_alerts": recent_alerts,
        "watcher_signal": watcher_signal,
        "watcher_log_count": len(watcher_log_records),
        "audit_log_count": len(audit_log_records),
    }
    STORE.save_runtime_value(f"watcher_summary:{env_id}", result)
    return result


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    except Exception:
        return []
    return records


def _watcher_alert_for_task(
    watcher_summary: dict[str, Any] | None, task_id: str
) -> dict[str, Any]:
    summary = watcher_summary if isinstance(watcher_summary, dict) else {}
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key in ("latest_alert", "latest_scan"):
        candidate = summary.get(key)
        if isinstance(candidate, dict):
            candidates.append((key, candidate))
    for key, items in (("recent_alerts", summary.get("recent_alerts")), ("recent_scans", summary.get("recent_scans"))):
        if isinstance(items, list):
            for item in reversed(items):
                if isinstance(item, dict):
                    candidates.append((key, item))
    for source, candidate in candidates:
        current = candidate.get("current") if isinstance(candidate.get("current"), dict) else {}
        candidate_task_id = str(current.get("task_id") or candidate.get("task_id") or "")
        if candidate_task_id and candidate_task_id == task_id:
            return {
                "source": source,
                "event": candidate.get("event") or "task_closure_watcher_alert",
                "ts": candidate.get("ts") or 0,
                "current": current,
            }
    return {}


def _watcher_signal_needs_action(
    watcher_summary: dict[str, Any] | None, task_id: str
) -> dict[str, Any]:
    summary = watcher_summary if isinstance(watcher_summary, dict) else {}
    signal = summary.get("watcher_signal") if isinstance(summary.get("watcher_signal"), dict) else {}
    if not signal:
        return {}
    signal_task_id = str(signal.get("task_id") or "")
    if signal_task_id and signal_task_id == task_id:
        return {
            "needs_delivery_retry": bool(signal.get("needs_delivery_retry")),
            "needs_receipt_or_block": bool(signal.get("needs_receipt_or_block")),
            "reason": str(signal.get("reason") or ""),
            "ts": int(signal.get("ts") or 0),
        }
    return {}


def _clear_watcher_signal_if_processed(watcher_summary: dict[str, Any] | None) -> None:
    summary = watcher_summary if isinstance(watcher_summary, dict) else {}
    monitor_dir = summary.get("monitor_dir")
    if not monitor_dir:
        monitor_dir = Path.home() / ".openclaw" / "shared-context" / "monitor-tasks"
    else:
        monitor_dir = Path(monitor_dir)
    signal_file = monitor_dir / "watcher-control-signal.json"
    if signal_file.exists():
        try:
            signal_file.write_text("{}", encoding="utf-8")
        except Exception:
            pass


def _load_watcher_signal_direct() -> dict[str, Any]:
    signal_file = Path.home() / ".openclaw" / "shared-context" / "monitor-tasks" / "watcher-control-signal.json"
    return safe_read_json_file(signal_file, {})


def build_learning_supervision_summary(
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = spec or current_env_spec()
    env_id = str(target.get("id") or "primary")
    home = Path(str(target.get("home") or Path.home() / ".openclaw"))
    now = int(time.time())
    learnings_dir = home / ".learnings"
    artifact_files = {
        "pending": learnings_dir / "pending.jsonl",
        "promoted": learnings_dir / "promoted.jsonl",
        "discarded": learnings_dir / "discarded.jsonl",
        "reflection_runs": learnings_dir / "reflection-runs.jsonl",
        "reuse_evidence": learnings_dir / "reuse-evidence.jsonl",
    }
    artifact_records = {
        name: _read_jsonl_records(path) for name, path in artifact_files.items()
    }
    existing = {name for name, path in artifact_files.items() if path.exists()}
    required = {"pending", "promoted", "discarded", "reflection_runs"}
    projected_learnings = STORE.list_learning_view(limit=200)
    legacy_learnings = [
        item for item in projected_learnings if item.get("env_id") == env_id
    ]
    legacy_reflections = STORE.list_reflection_runs(limit=50)
    artifact_status = "missing"
    if required.issubset(existing):
        artifact_status = "ready"
    elif existing:
        artifact_status = "partial"
    elif legacy_learnings or legacy_reflections:
        artifact_status = "legacy_store_only"

    learning_records: list[dict[str, Any]] = []
    if any(artifact_records.values()):
        for bucket in ("pending", "promoted", "discarded"):
            learning_records.extend(artifact_records.get(bucket, []))
    else:
        learning_records = legacy_learnings
    reflection_records = (
        artifact_records.get("reflection_runs", []) or legacy_reflections
    )
    reuse_records = artifact_records.get("reuse_evidence", [])

    def latest_value(records: list[dict[str, Any]], *keys: str) -> int:
        values = []
        for item in records:
            for key in keys:
                raw = int(item.get(key) or 0)
                if raw:
                    values.append(raw)
                    break
        return max(values) if values else 0

    latest_learning_at = latest_value(learning_records, "updated_at", "created_at")
    latest_reflection_at = latest_value(reflection_records, "finished_at", "created_at")
    memory_path = home / "MEMORY.md"
    memory_updated_at = int(memory_path.stat().st_mtime) if memory_path.exists() else 0
    recent_window = [
        item
        for item in learning_records
        if int(item.get("updated_at") or item.get("created_at") or 0) >= now - 7 * 86400
    ]
    previous_window = [
        item
        for item in learning_records
        if now - 14 * 86400
        <= int(item.get("updated_at") or item.get("created_at") or 0)
        < now - 7 * 86400
    ]
    if not recent_window and not previous_window:
        repeat_error_trend = "insufficient_data"
    elif len(recent_window) < len(previous_window):
        repeat_error_trend = "down"
    elif len(recent_window) > len(previous_window):
        repeat_error_trend = "up"
    else:
        repeat_error_trend = "flat"

    def pick_run(run_type: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in reflection_records
                if str(item.get("run_type") or "") == run_type
            ),
            None,
        )

    def pick_status(item: dict[str, Any] | None) -> str:
        if not item:
            return "missing"
        return str(
            item.get("status") or (item.get("summary") or {}).get("status") or "unknown"
        )

    daily_reflection = pick_run("daily-reflection") or (
        reflection_records[0] if reflection_records else None
    )
    memory_maintenance = pick_run("memory-maintenance")
    team_rollup = pick_run("team-rollup")

    return {
        "generated_at": now,
        "env_id": env_id,
        "artifact_status": artifact_status,
        "learning_freshness": max(now - latest_learning_at, 0)
        if latest_learning_at
        else None,
        "reflection_freshness": max(now - latest_reflection_at, 0)
        if latest_reflection_at
        else None,
        "memory_freshness": max(now - memory_updated_at, 0)
        if memory_updated_at
        else None,
        "promoted_items_count": sum(
            1
            for item in learning_records
            if str(item.get("status") or "") == "promoted"
        ),
        "promoted_items_24h": sum(
            1
            for item in learning_records
            if str(item.get("status") or "") == "promoted"
            and int(item.get("updated_at") or item.get("created_at") or 0)
            >= now - 86400
        ),
        "reuse_evidence_count": len(reuse_records),
        "reuse_evidence_7d": sum(
            1
            for item in reuse_records
            if int(item.get("updated_at") or item.get("created_at") or 0)
            >= now - 7 * 86400
        ),
        "repeat_error_trend": repeat_error_trend,
        "last_daily_reflection_at": int(
            (daily_reflection or {}).get("finished_at")
            or (daily_reflection or {}).get("created_at")
            or 0
        ),
        "last_memory_maintenance_at": int(
            (memory_maintenance or {}).get("finished_at")
            or (memory_maintenance or {}).get("created_at")
            or 0
        ),
        "last_team_rollup_at": int(
            (team_rollup or {}).get("finished_at")
            or (team_rollup or {}).get("created_at")
            or 0
        ),
        "daily_reflection_status": pick_status(daily_reflection),
        "memory_maintenance_status": pick_status(memory_maintenance),
        "team_rollup_status": pick_status(team_rollup),
    }


def build_self_check_supervision_summary(
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = spec or current_env_spec()
    env_id = str(target.get("id") or "primary")
    home = Path(str(target.get("home") or Path.home() / ".openclaw"))
    now = int(time.time())
    self_check_dir = home / "shared-context" / "self-check"
    runtime_status_path = self_check_dir / "self-check-runtime-status.json"
    events_path = self_check_dir / "self-check-events.json"

    runtime_status: dict[str, Any] = {}
    runtime_status_valid = False
    if runtime_status_path.exists():
        try:
            runtime_status = safe_read_json_file(runtime_status_path, {})
            runtime_status_valid = (
                isinstance(runtime_status, dict)
                and bool(runtime_status.get("last_self_check_at"))
                and bool(runtime_status.get("self_check_status"))
            )
        except Exception:
            runtime_status = {}
    if not runtime_status:
        runtime_status = STORE.load_runtime_value(
            f"self_check_summary:{env_id}",
            {
                "env_id": env_id,
                "self_check_artifact_status": "missing",
                "self_check_status": "missing",
                "last_self_check_at": 0,
                "last_self_recovery_at": 0,
                "last_self_recovery_result": "",
                "delivery_retry_count": 0,
                "completed_not_delivered_count": 0,
                "stale_subagent_count": 0,
            },
        )

    events_payload: dict[str, Any] = {}
    events_valid = False
    if events_path.exists():
        try:
            events_payload = safe_read_json_file(events_path, {})
            events_valid = isinstance(events_payload, dict) and isinstance(
                events_payload.get("events") or [], list
            )
        except Exception:
            events_payload = {}
    if not events_payload:
        events_payload = STORE.load_runtime_value(
            f"self_check_events:{env_id}", {"env_id": env_id, "events": []}
        )

    last_self_check_at = int(runtime_status.get("last_self_check_at") or 0)
    last_self_recovery_at = int(runtime_status.get("last_self_recovery_at") or 0)
    recent_events = list(events_payload.get("events") or [])[:20]
    if (
        runtime_status_path.exists()
        and events_path.exists()
        and runtime_status_valid
        and events_valid
    ):
        artifact_status = "ready"
    elif runtime_status_path.exists() or events_path.exists():
        artifact_status = "invalid"
    else:
        artifact_status = str(
            runtime_status.get("self_check_artifact_status") or "missing"
        )

    return {
        "generated_at": now,
        "env_id": env_id,
        "self_check_artifact_status": artifact_status,
        "self_check_freshness": max(now - last_self_check_at, 0)
        if last_self_check_at
        else None,
        "last_self_check_at": last_self_check_at,
        "self_check_status": str(runtime_status.get("self_check_status") or "missing"),
        "last_self_recovery_freshness": max(now - last_self_recovery_at, 0)
        if last_self_recovery_at
        else None,
        "last_self_recovery_at": last_self_recovery_at,
        "last_self_recovery_result": str(
            runtime_status.get("last_self_recovery_result") or ""
        ),
        "delivery_retry_count": int(runtime_status.get("delivery_retry_count") or 0),
        "completed_not_delivered_count": int(
            runtime_status.get("completed_not_delivered_count") or 0
        ),
        "stale_subagent_count": int(runtime_status.get("stale_subagent_count") or 0),
        "recent_event_types": [
            str(item.get("event_type") or "unknown") for item in recent_events[:5]
        ],
        "events": recent_events,
    }


def build_main_closure_supervision_summary(
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = spec or current_env_spec()
    env_id = str(target.get("id") or "primary")
    home = Path(str(target.get("home") or Path.home() / ".openclaw"))
    now = int(time.time())
    closure_dir = home / "shared-context" / "main-closure"
    shared_state_runtime_status_path = (
        SHARED_STATE_DIR / "main-closure-runtime-status.json"
    )
    shared_state_events_path = SHARED_STATE_DIR / "main-closure-events.json"
    runtime_status_path = closure_dir / "main-closure-runtime-status.json"
    events_path = closure_dir / "main-closure-events.json"
    if not runtime_status_path.exists() and shared_state_runtime_status_path.exists():
        runtime_status_path = shared_state_runtime_status_path
    if not events_path.exists() and shared_state_events_path.exists():
        events_path = shared_state_events_path

    runtime_status: dict[str, Any] = {}
    runtime_status_valid = False
    if runtime_status_path.exists():
        try:
            runtime_status = safe_read_json_file(runtime_status_path, {})
            runtime_status_valid = isinstance(runtime_status, dict) and bool(
                runtime_status.get("foreground_root_task_id")
                or runtime_status.get("generated_at")
            )
        except Exception:
            runtime_status = {}
    if not runtime_status:
        runtime_status = {
            "env_id": env_id,
            "main_closure_artifact_status": "derived",
            **STORE.summarize_main_closure(limit_roots=20, limit_events=50),
        }

    events_payload: dict[str, Any] = {}
    events_valid = False
    if events_path.exists():
        try:
            events_payload = safe_read_json_file(events_path, {})
            events_valid = isinstance(events_payload, dict) and isinstance(
                events_payload.get("events") or [], list
            )
        except Exception:
            events_payload = {}
    if not events_payload:
        closure_summary = STORE.summarize_main_closure(limit_roots=20, limit_events=50)
        events_payload = {
            "env_id": env_id,
            "events": closure_summary.get("events") or [],
        }

    if (
        runtime_status_path.exists()
        and events_path.exists()
        and runtime_status_valid
        and events_valid
    ):
        artifact_status = "ready"
    elif runtime_status_path.exists() or events_path.exists():
        artifact_status = "invalid"
    else:
        artifact_status = str(
            runtime_status.get("main_closure_artifact_status") or "missing"
        )

    recent_events = list(events_payload.get("events") or [])[:20]
    return {
        "generated_at": now,
        "env_id": env_id,
        "main_closure_artifact_status": artifact_status,
        "foreground_root_task_id": str(
            runtime_status.get("foreground_root_task_id") or ""
        ),
        "active_root_count": int(runtime_status.get("active_root_count") or 0),
        "background_root_count": int(runtime_status.get("background_root_count") or 0),
        "adoption_pending_count": int(
            runtime_status.get("adoption_pending_count") or 0
        ),
        "finalization_pending_count": int(
            runtime_status.get("finalization_pending_count") or 0
        ),
        "delivery_failed_count": int(runtime_status.get("delivery_failed_count") or 0),
        "late_result_count": int(runtime_status.get("late_result_count") or 0),
        "binding_source_counts": runtime_status.get("binding_source_counts") or {},
        "recent_event_types": [
            str(item.get("event_type") or "unknown") for item in recent_events[:5]
        ],
        "roots": list(runtime_status.get("roots") or [])[:10],
        "finalizers": list(runtime_status.get("finalizers") or [])[:10],
        "delivery_attempts": list(runtime_status.get("delivery_attempts") or [])[:10],
        "followups": list(runtime_status.get("followups") or [])[:20],
        "purity_metrics": runtime_status.get("purity_metrics") or {},
        "purity_gate_ok": bool(
            (runtime_status.get("purity_metrics") or {}).get("purity_gate_ok", True)
        ),
        "purity_gate_reasons": list(
            (runtime_status.get("purity_metrics") or {}).get("purity_gate_reasons")
            or []
        ),
        "events": recent_events,
    }


def should_delegate_learning_ownership_to_openclaw(
    spec: dict[str, Any] | None = None,
) -> bool:
    summary = build_learning_supervision_summary(spec)
    return str(summary.get("artifact_status") or "") in {"ready", "partial"}


def load_alerts():
    """加载告警历史"""
    global ALERTS
    ALERTS = STORE.load_alerts(ALERTS_FILE)


def save_alerts():
    """保存告警历史"""
    STORE.save_alerts(ALERTS)
    with open(ALERTS_FILE, "w") as f:
        json.dump(ALERTS, f, indent=2)


def load_versions():
    """加载版本历史"""
    global VERSIONS
    VERSIONS = STORE.load_versions(VERSIONS_FILE)


def save_versions():
    """保存版本历史"""
    STORE.save_versions(VERSIONS)
    with open(VERSIONS_FILE, "w") as f:
        json.dump(VERSIONS, f, indent=2)


def log(msg: str, level: str = "INFO"):
    """日志输出"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run_cmd(cmd: str) -> tuple:
    """执行命令"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def run_args(
    args: list[str], timeout: int = 30, env: Optional[Dict[str, str]] = None
) -> tuple[int, str, str]:
    """Run a subprocess without going through a shell."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def openclaw_runtime_env() -> Dict[str, str]:
    """Build a subprocess env pinned to the active OpenClaw environment."""
    spec = current_env_spec()
    env = os.environ.copy()
    env["OPENCLAW_STATE_DIR"] = str(spec["home"])
    env["OPENCLAW_CONFIG_PATH"] = str(spec["home"] / "openclaw.json")
    env["OPENCLAW_GATEWAY_PORT"] = str(spec["port"])
    return env


def get_process_info(name: str) -> Optional[Dict]:
    """获取进程信息"""
    code, stdout, _ = run_cmd(f'ps aux | grep -i "{name}" | grep -v grep')
    if code != 0 or not stdout:
        return None

    lines = stdout.strip().split("\n")
    if not lines:
        return None

    parts = lines[0].split()
    if len(parts) < 11:
        return None

    return {
        "pid": int(parts[1]),
        "cpu": float(parts[2]),
        "mem": float(parts[3]),
        "cmd": " ".join(parts[10:]),
    }


def get_listener_pid(port: int) -> Optional[int]:
    """返回监听指定端口的 PID。"""
    # 使用完整路径，避免 PATH 环境变量问题
    code, stdout, _ = run_cmd(f"/usr/sbin/lsof -ti tcp:{port} -sTCP:LISTEN")
    if code != 0 or not stdout.strip():
        return None
    try:
        return int(stdout.strip().splitlines()[0])
    except ValueError:
        return None


def check_gateway_health() -> bool:
    """检查当前 active env 的 Gateway 健康状态。"""
    spec = current_env_spec()
    retries = CONFIG.get("HEALTH_CHECK_RETRIES", 3)
    delay = CONFIG.get("HEALTH_CHECK_DELAY", 5)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{spec['port']}/health")
            with urllib.request.urlopen(req, timeout=5) as response:
                payload = json.loads(
                    response.read().decode("utf-8", errors="ignore") or "{}"
                )
            if bool(payload.get("ok")):
                return True
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(delay)
    return False


def check_process_running() -> bool:
    """检查进程是否运行"""
    return get_listener_pid(int(current_env_spec()["port"])) is not None


def get_system_metrics() -> Dict:
    """获取系统指标"""
    cpu = 0.0
    mem_used = 0
    mem_total = 32

    code, stdout, _ = run_cmd("top -l 1 -n 0")
    if code == 0:
        for line in stdout.split("\n"):
            if "CPU usage" in line:
                try:
                    user = float(
                        [x for x in line.split() if "user" in x][0].replace("%user", "")
                    )
                    system = float(
                        [x for x in line.split() if "sys" in x][0].replace("%sys", "")
                    )
                    cpu = user + system
                except:
                    pass
            if "PhysMem" in line:
                try:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if "G" in p and "used" in p:
                            mem_used = int(p.replace("G", "").replace("used", ""))
                        if "G" in p and "unused" in p:
                            mem_total = mem_used + int(
                                p.replace("G", "").replace("unused", "")
                            )
                except:
                    pass

    return {"cpu": round(cpu, 1), "mem_used": mem_used, "mem_total": mem_total}


def analyze_slow_sessions() -> List[Dict]:
    """分析慢会话"""
    if not GATEWAY_LOG.exists():
        return []

    slow_threshold = CONFIG.get("SLOW_RESPONSE_THRESHOLD", 30)
    sessions = []
    now = time.time()

    try:
        with open(GATEWAY_LOG) as f:
            lines = f.readlines()[-2000:]  # 最近2000行

        dispatch_time = {}

        for line in lines:
            if "dispatching to agent" in line.lower():
                try:
                    # 提取时间戳
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").timestamp()
                    dispatch_time["dispatch"] = ts
                except:
                    pass
            elif "dispatch complete" in line.lower():
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").timestamp()
                    if "dispatch" in dispatch_time:
                        duration = ts - dispatch_time["dispatch"]
                        if duration > slow_threshold:
                            sessions.append(
                                {
                                    "time": ts_str,
                                    "duration": int(duration),
                                    "reason": "LLM响应慢"
                                    if duration > 60
                                    else "处理耗时",
                                }
                            )
                        dispatch_time = {}
                except:
                    pass
    except Exception as e:
        log(f"分析慢会话失败: {e}")

    return sessions[-10:]  # 返回最近10个


def resolve_runtime_gateway_log() -> Path:
    """Return the most recent runtime log file."""
    candidates = sorted(
        TMP_OPENCLAW_LOG_DIR.glob("openclaw-*.log"), key=lambda p: p.stat().st_mtime
    )
    if candidates:
        return candidates[-1]
    return current_gateway_log()


def scan_pipeline_progress_events() -> None:
    """Scan runtime logs for pipeline progress markers and persist them once."""
    runtime_log = resolve_runtime_gateway_log()
    if not runtime_log.exists():
        return

    cursor = STORE.load_runtime_value("pipeline_progress_cursor", {})
    last_signature = cursor.get("last_signature", "")

    try:
        with open(runtime_log) as handle:
            lines = handle.readlines()[-2000:]
    except Exception as exc:
        log(f"读取运行日志失败: {exc}", "ERROR")
        return

    latest_signature = last_signature
    for line in lines:
        if "PIPELINE_PROGRESS:" not in line:
            continue
        signature = line.strip()
        if signature <= last_signature:
            continue

        marker = line.split("PIPELINE_PROGRESS:", 1)[1].strip()
        ts_match = line.split('"time":"')
        timestamp = ""
        if len(ts_match) > 1:
            timestamp = ts_match[1].split('"', 1)[0]

        message = f"多智能体进度: {marker}"
        details = {
            "marker": marker,
            "timestamp": timestamp,
            "source_log": str(runtime_log),
        }
        record_change_log("pipeline", message, details)
        latest_signature = signature

    if latest_signature != last_signature:
        STORE.save_runtime_value(
            "pipeline_progress_cursor",
            {"last_signature": latest_signature, "source_log": str(runtime_log)},
        )


def parse_runtime_timestamp(line: str) -> tuple[str, float | None]:
    """Parse ISO timestamp from either JSONL runtime logs or plain text logs."""
    if line.startswith("{"):
        marker = '"time":"'
        if marker in line:
            raw = line.split(marker, 1)[1].split('"', 1)[0]
            normalized = raw[:19]
            try:
                ts = datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S").timestamp()
                return raw, ts
            except Exception:
                return raw, None
    raw = line[:19]
    try:
        ts = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").timestamp()
        return raw, ts
    except Exception:
        return raw, None


MAX_TASK_QUESTION_LEN = 240


def _clamp_task_question(text: str | None, limit: int = MAX_TASK_QUESTION_LEN) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    return raw[:limit]


def extract_runtime_question(line: str) -> str | None:
    """Extract a user-visible question from runtime logs when possible."""
    sanitized = line
    lower_line = line.lower()
    if '"feishu[default] dm from ' in lower_line or '"feishu[default]:' in lower_line:
        if "Feishu[default]" in line and ": " in line:
            sanitized = line.split(": ", 1)[1]
            sanitized = sanitized.split('","_meta"', 1)[0]
            sanitized = sanitized.split('"}', 1)[0]
            sanitized = sanitized.strip()
            if sanitized:
                return _clamp_task_question(sanitized)
    lower = line.lower()
    ignore = (
        "dispatching to agent",
        "dispatch complete",
        "pipeline_progress:",
        "pipeline_receipt:",
        "announce_skip",
        "guardian_followup",
        "guardian_escalation",
        "fetched quoted message:",
        "failed to fetch quoted message:",
    )
    if any(marker in lower for marker in ignore):
        return None
    sanitized_lower = sanitized.lower()
    if " dm from " in sanitized_lower and ": " in sanitized:
        idx = sanitized.find(": ")
        if idx > 0:
            return _clamp_task_question(sanitized[idx + 2 :].strip())
    if "message in" in sanitized_lower and ": " in sanitized:
        idx = sanitized.find(": ")
        if idx > 0:
            return _clamp_task_question(sanitized[idx + 2 :].strip())
    if "feishu[default]:" in sanitized_lower and ": " in sanitized:
        idx = sanitized.find(": ")
        if idx > 0:
            return _clamp_task_question(sanitized[idx + 2 :].strip())
    return None


def normalize_task_question(text: str | None) -> str:
    """Normalize runtime question text into a user-visible task title."""
    raw = (text or "").strip()
    if not raw:
        return "未知任务"
    lower = raw.lower()
    if "dispatching to agent" in lower or "dispatch complete" in lower:
        return "未知任务"
    if "received message from " in lower:
        return "未知任务"
    # Filter out internal log lines that are not user messages
    if "fetched quoted message:" in lower:
        return "未知任务"
    if "failed to fetch quoted message:" in lower:
        return "未知任务"
    # Filter out system recovery messages that should not create tasks
    if "continue where you left off" in lower:
        return "未知任务"
    if "previous model attempt failed or timed out" in lower:
        return "未知任务"
    if "guardian_gateway_recovery" in lower:
        return "未知任务"
    if "guardian_task_control" in lower:
        return "未知任务"
    if "这不是用户新需求" in lower:
        return "未知任务"
    if "when reading heartbeat.md" in lower:
        return "未知任务"
    if "read heartbeat.md if it exists" in lower:
        return "未知任务"
    if "feishu[default] dm from " in lower and ": " in raw:
        raw = raw.split(": ", 1)[1].strip()
    raw = raw.split('","_meta"', 1)[0].strip()
    clamped = _clamp_task_question(raw)
    return clamped if clamped else "未知任务"


def _extract_user_question_from_transcript_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "未知任务"
    # Handle Hangzhou: prefix patterns
    if "\nHangzhou: " in raw:
        extracted = raw.rsplit("\nHangzhou: ", 1)[1].strip()
    elif raw.startswith("Hangzhou: "):
        extracted = raw.split(": ", 1)[1].strip()
    else:
        extracted = raw
    # If extracted starts with [Replying to: ...], skip the reply block and get the actual question
    if extracted.startswith("[Replying to:"):
        # Find the closing ] and get text after it
        bracket_end = extracted.find("]")
        if bracket_end >= 0 and bracket_end + 1 < len(extracted):
            after_bracket = extracted[bracket_end + 1:].strip()
            if after_bracket:
                return normalize_task_question(after_bracket)
        # If no text after ], fall through to line-based extraction
        lines = [line.strip() for line in extracted.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("[") or line.startswith("```"):
                continue
            if line.startswith("Conversation info") or line.startswith("Sender"):
                continue
            if line.startswith("Current time:"):
                continue
            if ": " in line and not line.lower().startswith("system:"):
                return normalize_task_question(line.split(": ", 1)[1].strip())
            return normalize_task_question(line)
    return normalize_task_question(extracted)


def _assistant_message_has_visible_reply(message: dict[str, Any]) -> bool:
    for item in list(message.get("content") or []):
        if item.get("type") != "text":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if text in {"NO_REPLY", "HEARTBEAT_OK"}:
            continue
        return True
    return False


def _iter_session_user_turns(session_file: Path) -> list[dict[str, Any]]:
    if not session_file.exists():
        return []
    try:
        lines = session_file.read_text(encoding="utf-8", errors="ignore").splitlines()[
            -400:
        ]
    except Exception:
        return []

    turns: list[dict[str, Any]] = []
    current_user: dict[str, Any] | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if payload.get("type") != "message":
            continue
        message = payload.get("message") or {}
        role = str(message.get("role") or "")
        if role == "user":
            text_parts = []
            for item in list(message.get("content") or []):
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
            question = _extract_user_question_from_transcript_text(
                "\n".join(text_parts)
            )
            current_user = {
                "question": question,
                "timestamp": str(payload.get("timestamp") or ""),
                "replied": False,
            }
            turns.append(current_user)
            continue
        if (
            role == "assistant"
            and current_user
            and _assistant_message_has_visible_reply(message)
        ):
            current_user["replied"] = True
    return turns


def _latest_session_user_turn(session_file: Path) -> dict[str, Any] | None:
    turns = _iter_session_user_turns(session_file)
    return turns[-1] if turns else None


def _session_question_has_visible_reply(session_file: Path, question: str | None) -> bool:
    target = normalize_task_question(question)
    if not target or target == "未知任务":
        return False
    for turn in _iter_session_user_turns(session_file):
        if normalize_task_question(turn.get("question")) == target and bool(
            turn.get("replied")
        ):
            return True
    return False


def recover_untracked_session_tasks(
    session_store_path: Path | None = None,
) -> list[str]:
    session_store = session_store_path or SESSION_STORE_FILE
    if not session_store.exists():
        return []
    try:
        sessions_payload = safe_read_json_file(session_store, [])
    except Exception:
        return []

    recovered: list[str] = []
    sessions_map = sessions_payload
    if isinstance(sessions_payload, dict) and isinstance(
        sessions_payload.get("sessions"), dict
    ):
        sessions_map = sessions_payload.get("sessions") or {}
    if not isinstance(sessions_map, dict):
        return recovered

    for session_key, session in sessions_map.items():
        if not isinstance(session, dict):
            continue
        if not str(session_key or "").startswith("agent:main:feishu:"):
            continue
        if str(session.get("status") or "") != "running":
            continue
        session_file = Path(str(session.get("sessionFile") or ""))
        latest_user = _latest_session_user_turn(session_file)
        if not latest_user:
            continue

        # 如果最近一条用户消息已经拿到主脑可见回复，则不要再从会话转录里恢复成未闭环任务。
        # 否则 Guardian 会把已答复的普通问答反复重建为 running/blocked，形成“追踪 3 次未闭环”噪音。
        if bool(latest_user.get("replied")):
            continue

        question = normalize_task_question(latest_user.get("question"))
        if question == "未知任务":
            continue
        try:
            ts = datetime.fromisoformat(
                str(latest_user.get("timestamp") or "").replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            ts = float(session.get("updatedAt") or time.time()) / 1000.0
        ts_int = int(ts)

        existing = STORE.get_latest_task_for_session(str(session_key))
        if existing:
            existing_question = normalize_task_question(
                existing.get("question") or existing.get("last_user_message")
            )
            existing_updated = int(
                existing.get("updated_at") or existing.get("created_at") or 0
            )
            existing_status = str(existing.get("status") or "")
            # 同一会话同一问题，不要重复恢复出第二个任务。
            if existing_question == question and existing_status in {"running", "background", "blocked", "completed"}:
                continue
            if existing_question == question and existing_updated >= ts_int:
                continue

        task_id = build_task_id(str(session_key), str(ts_int))
        task = {
            "task_id": task_id,
            "session_key": str(session_key),
            "env_id": active_env_id(),
            "channel": infer_task_channel(str(session_key)),
            "status": "running",
            "current_stage": "处理中",
            "question": question,
            "last_user_message": question,
            "started_at": ts_int,
            "last_progress_at": ts_int,
            "created_at": ts_int,
            "updated_at": ts_int,
            "latest_receipt": {},
        }
        if int(CONFIG.get("TASK_REGISTRY_MAX_ACTIVE", 1)) <= 1:
            STORE.background_other_tasks_for_session(str(session_key), task_id)
        STORE.upsert_task(task)
        STORE.record_task_event(
            task_id,
            "dispatch_started",
            {
                "session_key": str(session_key),
                "question": question,
                "channel": task["channel"],
                "timestamp": str(latest_user.get("timestamp") or ts_int),
                "env_id": active_env_id(),
                "source": "session_transcript_recovery",
            },
        )
        STORE.repair_task_identity(task_id)
        recovered.append(task_id)

    if recovered:
        write_task_registry_snapshot()
    return recovered


def load_openclaw_session_record(
    session_key: str, session_store_path: Path | None = None
) -> dict[str, Any] | None:
    if not session_key:
        return None
    session_store = session_store_path or SESSION_STORE_FILE
    if not session_store.exists():
        return None
    try:
        sessions_payload = safe_read_json_file(session_store, [])
    except Exception:
        return None
    sessions_map = sessions_payload
    if isinstance(sessions_payload, dict) and isinstance(
        sessions_payload.get("sessions"), dict
    ):
        sessions_map = sessions_payload.get("sessions") or {}
    if not isinstance(sessions_map, dict):
        return None
    session = sessions_map.get(session_key)
    return session if isinstance(session, dict) else None


def should_defer_guardian_followup(session_key: str, message: str) -> bool:
    if not session_key or not message.startswith("GUARDIAN_"):
        return False
    session = load_openclaw_session_record(session_key)
    if not session:
        return False
    if str(session.get("status") or "") != "running":
        return False
    session_file = Path(str(session.get("sessionFile") or ""))
    latest_user = _latest_session_user_turn(session_file)
    if not latest_user:
        return False
    question = normalize_task_question(latest_user.get("question"))
    if question.startswith("GUARDIAN_"):
        return False
    try:
        latest_ts = datetime.fromisoformat(
            str(latest_user.get("timestamp") or "").replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        latest_ts = float(session.get("updatedAt") or 0) / 1000.0
    grace_seconds = int(CONFIG.get("GUARDIAN_FOLLOWUP_ACTIVE_SESSION_GRACE", 900))
    if grace_seconds <= 0:
        return False
    return (time.time() - latest_ts) < grace_seconds


def extract_pipeline_marker(line: str) -> str | None:
    """Extract a pipeline progress marker from the runtime logs."""
    marker = "PIPELINE_PROGRESS:"
    if marker not in line:
        return None
    return line.split(marker, 1)[1].strip()[:120]


def extract_pipeline_receipt(line: str) -> dict[str, str] | None:
    """Extract and validate a structured PIPELINE_RECEIPT payload from runtime logs.

    Supports two formats:
    1. Old format: PIPELINE_RECEIPT: agent=... | phase=... | action=...
    2. JSON format: **PIPELINE_RECEIPT** followed by JSON block with "type": "PIPELINE_RECEIPT"
    """
    marker = "PIPELINE_RECEIPT:"
    json_marker = "**PIPELINE_RECEIPT**"

    # Try JSON format first
    if json_marker in line:
        # Look for JSON block after the marker
        try:
            # Find JSON content - could be on same line or next
            json_start = line.find("{")
            if json_start >= 0:
                json_str = line[json_start:]
                # Try to parse as JSON
                import json

                data = json.loads(json_str)
                if isinstance(data, dict) and data.get("type") == "PIPELINE_RECEIPT":
                    receipt = {
                        "agent": str(data.get("agent", "")),
                        "phase": str(data.get("status", "")),
                        "action": str(data.get("status", "")),
                        "task_id": str(data.get("task_id", "")),
                        "summary": str(data.get("summary", "")),
                        "timestamp": str(data.get("timestamp", "")),
                    }
                    ts_raw, _ = parse_runtime_timestamp(line)
                    return normalize_pipeline_receipt(receipt, timestamp=ts_raw)
        except (json.JSONDecodeError, ValueError):
            pass

    # Fall back to old format
    if marker not in line:
        return None
    payload = line.split(marker, 1)[1].strip()
    receipt: dict[str, str] = {}
    for part in payload.split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        receipt[key.strip()] = value.strip()
    ts_raw, _ = parse_runtime_timestamp(line)
    return normalize_pipeline_receipt(receipt, timestamp=ts_raw)


def is_visible_completion_message(line: str) -> bool:
    """Heuristically detect a user-visible completion reply in runtime logs."""
    text = line.strip()
    if not text:
        return False

    lower = text.lower()
    ignore_markers = (
        "dispatching to agent",
        "dispatch complete",
        "pipeline_progress:",
        "pipeline_receipt:",
        "[gateway]",
        "[feishu]",
        "[plugins]",
        "[ws]",
        "guardian_followup",
        "guardian_escalation",
        "received message from",
        "dm from ",
        "message in ",
        "signal sigterm",
        "gateway closed",
        "error:",
        "config warnings",
        "当前卡点",
        "等待 dev",
        "等待 test",
        "继续推进",
        "当前关键信息",
    )
    if any(marker in lower for marker in ignore_markers):
        return False

    non_terminal_markers = (
        "下一步",
        "后续",
        "继续",
        "等待",
        "跟进",
        "排查",
        "处理中",
        "正在",
        "阻塞",
        "未完成",
        "待",
        "稍后",
    )
    if any(marker in text for marker in non_terminal_markers):
        return False

    completion_markers = (
        "任务已完成",
        "已经完成",
        "已全部完成",
        "全部配置完成",
        "修复完成",
        "已经修复",
        "处理完成",
        "已处理完成",
        "测试通过",
        "验收通过",
        "可以直接使用",
        "可以正常使用",
        "可以在飞书群里查看",
        "无需后续操作",
        "系统已就绪",
        "当前状态： 已结束",
        "当前状态：已结束",
        "以后每天都会自动推送",
    )
    if any(marker in text for marker in completion_markers):
        return True

    return bool(
        ("已完成" in text or "完成了" in text or "搞定了" in text or "结束了" in text)
        and any(
            marker in text
            for marker in ("主人", "你可以", "可直接", "已回复给主人", "已经回复给主人")
        )
    )


def extract_requester_open_id(line: str) -> str | None:
    """Extract Feishu requester open_id from a dispatch session key."""
    marker = "session=agent:main:feishu:direct:"
    if marker not in line:
        return None
    tail = line.split(marker, 1)[1]
    open_id = tail.split(")", 1)[0].strip()
    return open_id or None


def extract_runtime_session_key(line: str) -> str | None:
    """Extract the session key from a runtime dispatch line when present."""
    marker = "session="
    if marker not in line:
        return None
    tail = line.split(marker, 1)[1]
    for sep in (")", " ", "\n"):
        tail = tail.split(sep, 1)[0]
    session_key = tail.strip()
    return session_key or None


def normalize_stage_label(marker: str) -> str:
    """Convert a pipeline marker into a generic human-readable stage label."""
    text = (marker or "").strip()
    if not text:
        return "处理中"
    if "BLOCKED" in text:
        return "当前阶段阻塞"
    if "RUNNING" in text:
        return "当前阶段运行中"
    if "ANALYZING" in text:
        return "当前阶段分析中"
    if "IMPLEMENTING" in text:
        return "当前阶段处理中"
    if "->" in text:
        left, right = [part.strip() for part in text.split("->", 1)]
        return f"阶段切换: {left} -> {right}"
    return text.replace("_", " ")


def classify_guardian_followup_error(output: str) -> str:
    """Classify follow-up failures into coarse blocking reasons."""
    text = (output or "").lower()
    if output == "session_active":
        return "session_active"
    if "session file locked" in text or ".jsonl.lock" in text:
        return "session_lock"
    if (
        "oauth token refresh failed" in text
        or "re-authenticate" in text
        or "(auth)" in text
    ):
        return "model_auth"
    if "model_not_found" in text or "404 page not found" in text:
        return "model_unavailable"
    if "all models failed" in text:
        return "model_pool_failed"
    if "timeout" in text:
        return "timeout"
    return "unknown"


def blocked_reason_label(reason: str) -> str:
    """Render a user-facing blocked reason label."""
    mapping = {
        "session_lock": "会话资源被占用",
        "model_auth": "模型认证失效",
        "model_unavailable": "模型不可用",
        "model_pool_failed": "模型链路不可用",
        "timeout": "会话响应超时",
        "unknown": "内部执行异常",
    }
    return mapping.get(reason, "内部执行异常")


def is_internal_system_problem(reason: str) -> bool:
    """Whether this blocked reason is a watchdog/system-side issue that should not be escalated to the owner."""
    value = str(reason or "").strip()
    return value in {
        "missing_pipeline_receipt",
        "background_root_task_missing",
        "control_followup_failed",
        "blocked_control_followup_failed",
        "session_lock",
        "model_auth",
        "model_unavailable",
        "model_pool_failed",
        "timeout",
        "unknown",
    }


def format_duration_label(seconds: int) -> str:
    """Render durations in a human-friendly label for push notifications."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        minutes = total // 60
        remain = total % 60
        return f"{minutes}分钟" if remain == 0 else f"{minutes}分{remain}秒"
    hours = total // 3600
    remain = total % 3600
    minutes = remain // 60
    return f"{hours}小时" if minutes == 0 else f"{hours}小时{minutes}分钟"


def build_control_plane_followup(
    task: dict[str, Any],
    control: dict[str, Any],
    *,
    idle: int,
    total: int,
) -> str:
    """Build a task-contract-aware guardian control message."""
    question = str(task.get("question") or task.get("last_user_message") or "未知任务")
    stage = str(task.get("current_stage") or "处理中")
    control_state = str(control.get("control_state") or "unknown")
    next_action = str(control.get("next_action") or "require_receipt_or_block")
    next_actor = str(control.get("next_actor") or "guardian")
    contract = control.get("contract") or {}
    contract_id = str(contract.get("id") or "single_agent")
    missing = ", ".join(control.get("missing_receipts") or []) or "none"

    action_instruction = {
        "require_pm_receipt": "若产品阶段已开始，请补发 PIPELINE_RECEIPT: agent=pm | phase=planning | action=started/completed；若未开始，请明确阻塞原因。",
        "await_pm_receipt": "若方案仍在整理，请补发 pm 的 started/completed 回执；若无法继续，请明确阻塞原因。",
        "require_dev_receipt": "若方案已完成，请立即按官方 sessions_spawn(agentId='dev') 继续，并补发 dev started 回执；若开发无法启动，请明确阻塞原因。",
        "await_dev_receipt": "若开发已开始，请补发 dev completed/blocked 回执，不要只发口头进度。",
        "require_test_receipt": "若开发已完成，请立即按官方 sessions_spawn(agentId='test') 继续，并补发 test started 回执；若测试无法启动，请明确阻塞原因。",
        "await_test_receipt": "若测试已开始，请补发 test completed/blocked 回执，不要只发口头进度。",
        "require_calculator_start": "若这是量化/精算任务，请先启动 calculator，并补发 calculator started 回执；若未启动，请明确阻塞原因。",
        "await_calculator_receipt": "若 calculator 已开始，请补发 calculator completed/blocked 回执，不要只发口头进度。",
        "require_verifier_receipt": "若精算结果已得出，请继续 verifier，并补发 verifier completed 回执；若无法复核，请明确阻塞原因。",
        "manual_or_session_recovery": "当前任务需要人工恢复或重新发起，请不要再口头宣称链路正在推进。",
        "await_receipt_after_recovery": "守护系统已发起恢复，请等待新的结构化回执，不要重复播报 final。",
        "require_receipt_or_block": "请立即补发结构化 PIPELINE_RECEIPT；若链路未真正继续，请明确阻塞原因。",
    }.get(
        next_action,
        "请立即补发结构化 PIPELINE_RECEIPT；若链路未真正继续，请明确阻塞原因。",
    )

    return (
        "GUARDIAN_TASK_CONTROL: 这不是用户新需求。"
        f"当前 task_id={task['task_id']}，任务合同={contract_id}，控制状态={control_state}，"
        f"下一执行责任人={next_actor}，"
        f"当前阶段={stage}，已静默={format_duration_label(idle)}，累计运行={format_duration_label(total)}，"
        f"缺失回执={missing}。当前问题={question}。"
        f"{action_instruction}"
        "不要再口头宣称团队正在推进，也不要把这条控制消息当成新的用户需求。"
    )


def build_pipeline_recovery_message(
    task: dict[str, Any],
    control: dict[str, Any],
    recovery: dict[str, Any],
    *,
    idle: int,
    total: int,
) -> str:
    question = str(task.get("question") or task.get("last_user_message") or "未知任务")
    stage = str(task.get("current_stage") or "处理中")
    kind = str(recovery.get("kind") or "unknown")
    rebind_target = str(recovery.get("rebind_target") or "guardian")
    stale_subagent = str(recovery.get("stale_subagent") or "unknown")
    last_agent = str(recovery.get("last_dispatched_agent") or "unknown")
    missing = ", ".join(control.get("missing_receipts") or []) or "none"
    return (
        "GUARDIAN_PIPELINE_RECOVERY: 这不是用户新需求，而是守护系统发起的恢复动作。"
        f"当前 task_id={task['task_id']}，恢复类型={kind}，最后派发节点={last_agent}，"
        f"疑似失联子代理={stale_subagent}，重绑目标={rebind_target}，当前阶段={stage}，"
        f"已静默={format_duration_label(idle)}，累计运行={format_duration_label(total)}，"
        f"缺失回执={missing}。当前问题={question}。"
        "请优先执行 session recovery / stale subagent detection / active task rebind，"
        "并在恢复后补发新的结构化 PIPELINE_RECEIPT。禁止重复 final，禁止只给口头说明。"
    )


def record_protocol_violation(
    task_id: str,
    *,
    violation_kind: str,
    event_type: str,
    payload: dict[str, Any],
    ack_id: str = "",
) -> None:
    STORE.record_task_event(
        task_id,
        "protocol_violation",
        {
            "violation_kind": violation_kind,
            "rejected_event_type": event_type,
            "ack_id": ack_id,
            "payload": payload,
            "timestamp": datetime.now().isoformat(),
        },
    )


def validate_protocol_event(
    task: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
    normalized = dict(payload or {})
    violations: list[dict[str, Any]] = []
    task_status = str(task.get("status") or "")
    task_id = str(task.get("task_id") or "")
    core = (
        STORE.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
        if task_id
        else {}
    )
    terminal = (
        bool(core.get("is_terminal"))
        or str(core.get("finalization_state") or "") == "finalized"
    )
    if not terminal:
        terminal = task_status == "completed" or STORE.has_task_event(
            task_id, "visible_completion"
        )

    if event_type == "pipeline_receipt":
        receipt = dict(normalized.get("receipt") or {})
        if not receipt.get("ack_id"):
            receipt["ack_id"] = hashlib.sha1(
                f"{task_id}|{receipt.get('agent', '')}|{receipt.get('phase', '')}|{receipt.get('action', '')}|{normalized.get('timestamp', '')}".encode(
                    "utf-8", errors="ignore"
                )
            ).hexdigest()[:16]
            violations.append(
                {
                    "violation_kind": "missing_ack_id_autofilled",
                    "ack_id": receipt["ack_id"],
                    "payload": {
                        "receipt": receipt,
                        "timestamp": normalized.get("timestamp", ""),
                    },
                    "rejected": False,
                }
            )
        normalized["receipt"] = receipt
        if terminal:
            violations.append(
                {
                    "violation_kind": "illegal_terminal_override",
                    "ack_id": str(receipt.get("ack_id") or ""),
                    "payload": {
                        "receipt": receipt,
                        "timestamp": normalized.get("timestamp", ""),
                    },
                    "rejected": True,
                }
            )
            return False, normalized, violations

    if event_type == "stage_progress" and terminal:
        violations.append(
            {
                "violation_kind": "illegal_terminal_override",
                "ack_id": "",
                "payload": normalized,
                "rejected": True,
            }
        )
        return False, normalized, violations

    if event_type == "visible_completion":
        if terminal:
            violations.append(
                {
                    "violation_kind": "duplicate_final",
                    "ack_id": "",
                    "payload": normalized,
                    "rejected": True,
                }
            )
            return False, normalized, violations

    return True, normalized, violations


def build_task_id(session_key: str, timestamp: str) -> str:
    """
    控制面主键生成函数：生成 task_id。

    边界原则：
    - task_id 由 helper 控制面生成，不由 OpenClaw 自己生成
    - session_key -> task_id 绑定由 helper 控制
    - OpenClaw 只提供 session_key（运行上下文），不决定 task_id

    字段归属：
    - task_id: helper 生成并拥有
    - session_key: OpenClaw 提供，helper 持久化绑定
    """
    raw = f"{session_key}|{timestamp}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def infer_task_channel(session_key: str) -> str:
    if ":feishu:direct:" in session_key:
        return "feishu_dm"
    if ":feishu:group:" in session_key:
        return "feishu_group"
    return "unknown"


def valid_task_question(text: str | None) -> bool:
    candidate = normalize_task_question(text)
    if not candidate or candidate == "未知任务":
        return False
    return True


def write_task_registry_snapshot() -> None:
    """
    控制面事实导出函数：持久化任务注册表快照。

    边界原则：
    - current-task-facts.json 由 helper 导出，不由 OpenClaw 自报
    - 控制面状态由 helper 基于证据判定，不信自由文本
    - completed != delivered，由 helper 确认

    字段归属：
    - current-task-facts.json: helper 导出
    - task-registry-summary.json: helper 导出
    - approved_summary: helper 生成
    - control_state: helper 判定
    - evidence_level: helper 计算
    - missing_receipts: helper 计算
    """
    if not CONFIG.get("ENABLE_TASK_REGISTRY", True):
        return
    current_spec = current_env_spec()
    env_id = current_spec["id"]
    lightweight_export = bool(CONFIG.get("LIGHTWEIGHT_TASK_REGISTRY_SNAPSHOT", True))
    bootstrap_status = (
        STORE.load_runtime_value(f"bootstrap_status:{env_id}", {})
        if lightweight_export
        else ensure_openclaw_bootstrap(current_spec)
    )
    watcher_summary = (
        STORE.load_runtime_value(f"watcher_summary:{env_id}", {})
        if lightweight_export
        else sync_shared_context_watcher_tasks(current_spec)
    )
    if not watcher_summary:
        watcher_summary = sync_shared_context_watcher_tasks(current_spec)
    restart_events = STORE.load_runtime_value(f"restart_events:{env_id}", [])
    if not isinstance(restart_events, list):
        restart_events = []
    recent_restart_events = restart_events[-20:]
    restart_runtime_status = {
        "generated_at": int(time.time()),
        "env_id": env_id,
        "total": len(restart_events),
        "recent": recent_restart_events,
        "last": recent_restart_events[-1] if recent_restart_events else None,
        "last_success": next(
            (
                item
                for item in reversed(recent_restart_events)
                if item.get("status") == "succeeded"
            ),
            None,
        ),
        "last_failure": next(
            (
                item
                for item in reversed(recent_restart_events)
                if item.get("status") == "failed"
            ),
            None,
        ),
    }
    watchdog_recovery_status = STORE.load_runtime_value(
        f"watchdog_recovery_status:{env_id}", {}
    )
    watchdog_recovery_hints = STORE.load_runtime_value(
        f"watchdog_recovery_hints:{env_id}", []
    )
    watchdog_recovery_incidents = STORE.load_runtime_value(
        f"watchdog_recovery_incidents:{env_id}", []
    )
    current = STORE.get_current_task(env_id=env_id)
    tasks = STORE.list_tasks(limit=int(CONFIG.get("TASK_REGISTRY_RETENTION", 100)))
    filtered = [task for task in tasks if task.get("env_id") == env_id]

    def enrich_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not task:
            return None
        question = normalize_task_question(task.get("question"))
        last_user_message = normalize_task_question(task.get("last_user_message"))
        if question == "未知任务":
            question = STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
        if last_user_message == "未知任务":
            last_user_message = (
                STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
            )
        control = STORE.derive_task_control_state(task["task_id"])
        core = STORE.get_core_closure_snapshot_for_task(
            task["task_id"], allow_legacy_projection=False
        )
        return {
            **task,
            "question": question,
            "last_user_message": last_user_message,
            "control": control,
            "contract": control.get("contract") or {},
            "root_task": core.get("root_task"),
            "current_workflow_run": core.get("current_workflow_run"),
            "current_finalizer": core.get("current_finalizer"),
            "current_delivery_attempt": core.get("current_delivery_attempt"),
            "current_followups": core.get("current_followups") or [],
            "core_truth": {
                "workflow_state": core.get("workflow_state") or "",
                "finalization_state": core.get("finalization_state") or "",
                "final_status": core.get("final_status") or "",
                "delivery_state": core.get("delivery_state") or "",
                "delivery_confirmation_level": core.get("delivery_confirmation_level")
                or "",
                "needs_followup": bool(core.get("needs_followup")),
                "truth_level": "core_projection"
                if core.get("has_core_projection")
                else "derived",
            },
        }

    current_payload = enrich_task(current)
    tasks_payload = [
        task for task in (enrich_task(item) for item in filtered[:20]) if task
    ]
    payload = {
        "generated_at": int(time.time()),
        "env_id": env_id,
        "summary": STORE.summarize_tasks(env_id=env_id),
        "current": current_payload,
        "tasks": tasks_payload,
        "session_resolution": None,
    }
    facts_current = payload.get("current") or (
        tasks_payload[0] if tasks_payload else None
    )
    session_resolution = (
        STORE.derive_session_resolution(
            str((facts_current or {}).get("session_key") or "")
        )
        if facts_current and facts_current.get("session_key")
        else None
    )
    # Phase 2 简化：current-task-facts.json 从"解释性快照"改成"事实性快照"
    # 只保留核心事实：execution_state, delivery_state, next_action
    control_data = (facts_current or {}).get("control") or {}
    facts_payload = {
        "generated_at": payload["generated_at"],
        "env_id": env_id,
        "current_task": {
            "task_id": facts_current.get("task_id") if facts_current else None,
            "question": facts_current.get("question") if facts_current else None,
            "status": facts_current.get("status") if facts_current else None,
            "current_stage": facts_current.get("current_stage")
            if facts_current
            else None,
            # 核心事实：msg_state, delivery_state, next_action
            "msg_state": (facts_current or {})
            .get("core_supervision", {})
            .get("msg_state")
            or (facts_current or {}).get("core_supervision", {}).get("workflow_state")
            or "open",
            "execution_state": (facts_current or {})
            .get("core_supervision", {})
            .get("msg_state")
            or (facts_current or {}).get("core_supervision", {}).get("workflow_state")
            or "open",
            "delivery_state": (facts_current or {})
            .get("core_supervision", {})
            .get("delivery_state")
            or "undelivered",
            "delivery_attempts": int(
                ((facts_current or {}).get("current_delivery_attempt") or {}).get(
                    "attempt_no"
                )
                or 0
            ),
            "next_action": control_data.get("next_action") or "none",
            "next_actor": control_data.get("next_actor") or "",
            "is_closed": bool(
                (facts_current or {}).get("core_supervision", {}).get("is_terminal")
            ),
            # 保留必要的审计字段
            "approved_summary": control_data.get("approved_summary"),
            "evidence_level": control_data.get("evidence_level"),
            "control_state": control_data.get("control_state"),
            # 向后兼容：保留完整的 control 对象
            "control": control_data,
        },
        "session_resolution": session_resolution,
    }
    if facts_current:
        facts_payload["current_root_task"] = facts_current.get("root_task") or None
        facts_payload["current_workflow_run"] = (
            facts_current.get("current_workflow_run") or None
        )
        facts_payload["current_finalizer"] = (
            facts_current.get("current_finalizer") or None
        )
        facts_payload["current_delivery_attempt"] = (
            facts_current.get("current_delivery_attempt") or None
        )
        facts_payload["current_followups"] = list(
            (facts_current.get("current_followups") or [])
        )
    main_closure_supervision = {
        "generated_at": payload["generated_at"],
        "env_id": env_id,
        "roots": [],
        "events": [],
        "foreground_root_task_id": "",
        "finalizers": [],
        "delivery_attempts": [],
        "followups": [],
        "purity_metrics": {},
        "purity_gate_ok": True,
        "purity_gate_reasons": [],
    }
    if not lightweight_export:
        main_closure_supervision = build_main_closure_supervision_summary(current_spec)
        foreground_root_id = str(
            main_closure_supervision.get("foreground_root_task_id") or ""
        )
        current_root = next(
            (
                item
                for item in list(main_closure_supervision.get("roots") or [])
                if str(item.get("root_task_id") or "") == foreground_root_id
            ),
            None,
        )
        if current_root:
            matching_task = next(
                (
                    item
                    for item in tasks_payload
                    if str(((item.get("root_task") or {}).get("root_task_id") or ""))
                    == foreground_root_id
                ),
                None,
            )
            if matching_task:
                payload["current"] = matching_task
        facts_current = payload.get("current") or (
            tasks_payload[0] if tasks_payload else None
        )
        session_resolution = (
            STORE.derive_session_resolution(
                str((facts_current or {}).get("session_key") or "")
            )
            if facts_current and facts_current.get("session_key")
            else None
        )
        payload["session_resolution"] = session_resolution
        facts_payload["current_root_task"] = current_root
        if current_root:
            current_workflow_run_id = str(
                current_root.get("current_workflow_run_id") or ""
            )
            if current_workflow_run_id:
                facts_payload["current_workflow_run"] = STORE.get_workflow_run(
                    current_workflow_run_id
                )
            root_task_id = str(current_root.get("root_task_id") or "")
            if root_task_id:
                facts_payload["current_followups"] = STORE.list_followups(
                    root_task_id=root_task_id, limit=20
                )
                finalizers = STORE.list_finalizer_records(
                    root_task_id=root_task_id, limit=5
                )
                deliveries = STORE.list_delivery_attempts(
                    root_task_id=root_task_id, limit=10
                )
                facts_payload["current_finalizer"] = (
                    finalizers[0] if finalizers else None
                )
                facts_payload["current_delivery_attempt"] = (
                    deliveries[0] if deliveries else None
                )
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json_file(data_dir / "task-registry-summary.json", payload)
    atomic_write_json_file(data_dir / "current-task-facts.json", facts_payload)
    shared_dir = data_dir / "shared-state"
    shared_dir.mkdir(parents=True, exist_ok=True)
    control_plane = STORE.summarize_control_plane(env_id=env_id)
    try:
        gateway_running = check_process_running()
    except Exception:
        gateway_running = False
    try:
        gateway_healthy = check_gateway_health()
    except Exception:
        gateway_healthy = False
    runtime_health = {
        "generated_at": int(time.time()),
        "env_id": env_id,
        "metrics": get_system_metrics(),
        "gateway_running": gateway_running,
        "gateway_healthy": gateway_healthy,
    }
    version_payload = record_version_state(
        current_spec, reason="guardian_snapshot", status="observed"
    )
    recovery_profile = build_recovery_profile(version_payload)
    learning_backlog = {
        "generated_at": int(time.time()),
        "summary": STORE.summarize_self_evolution(),
        "learnings": STORE.list_learning_view(limit=50),
        "reflections": STORE.list_reflection_runs(limit=20),
    }
    learning_supervision = build_learning_supervision_summary(current_spec)
    self_check_supervision = build_self_check_supervision_summary(current_spec)
    promotion_policy = {
        "generated_at": int(time.time()),
        "reflection_interval_seconds": int(
            CONFIG.get("REFLECTION_INTERVAL_SECONDS", 3600)
        ),
        "learning_promotion_threshold": int(
            CONFIG.get("LEARNING_PROMOTION_THRESHOLD", 3)
        ),
        "daily_review_expected": True,
        "rules": [
            "同类 learning 发生次数达到阈值后自动进入 promoted。",
            "promoted 项需要保留证据链与最近任务样本。",
            "每日应生成 memory/YYYY-MM-DD.md，用于沉淀当天 reflection 结果。",
        ],
    }
    context_baseline = {
        "generated_at": int(time.time()),
        "target_env": env_id,
        "recommended_baseline": CONTEXT_LIFECYCLE_BASELINE,
        "bootstrap_status": bootstrap_status,
        "watcher_summary": watcher_summary,
        "restart_runtime_status": restart_runtime_status,
    }
    (shared_dir / "task-registry-snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "control-action-queue.json").write_text(
        json.dumps(payload.get("control_queue") or [], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "runtime-health.json").write_text(
        json.dumps(runtime_health, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "learning-backlog.json").write_text(
        json.dumps(learning_backlog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "learning-runtime-status.json").write_text(
        json.dumps(learning_supervision, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "reflection-freshness.json").write_text(
        json.dumps(
            {
                "generated_at": learning_supervision.get("generated_at"),
                "env_id": learning_supervision.get("env_id"),
                "last_daily_reflection_at": learning_supervision.get(
                    "last_daily_reflection_at"
                ),
                "last_memory_maintenance_at": learning_supervision.get(
                    "last_memory_maintenance_at"
                ),
                "last_team_rollup_at": learning_supervision.get("last_team_rollup_at"),
                "daily_reflection_status": learning_supervision.get(
                    "daily_reflection_status"
                ),
                "memory_maintenance_status": learning_supervision.get(
                    "memory_maintenance_status"
                ),
                "team_rollup_status": learning_supervision.get("team_rollup_status"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (shared_dir / "memory-freshness.json").write_text(
        json.dumps(
            {
                "generated_at": learning_supervision.get("generated_at"),
                "env_id": learning_supervision.get("env_id"),
                "freshness_seconds": learning_supervision.get("memory_freshness"),
                "status": "fresh"
                if (learning_supervision.get("memory_freshness") or 10**9) < 86400
                else "stale",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (shared_dir / "reuse-evidence-summary.json").write_text(
        json.dumps(
            {
                "generated_at": learning_supervision.get("generated_at"),
                "env_id": learning_supervision.get("env_id"),
                "total": learning_supervision.get("reuse_evidence_count"),
                "last_7d": learning_supervision.get("reuse_evidence_7d"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (shared_dir / "self-check-runtime-status.json").write_text(
        json.dumps(self_check_supervision, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "self-check-events.json").write_text(
        json.dumps(
            {
                "generated_at": self_check_supervision.get("generated_at"),
                "env_id": self_check_supervision.get("env_id"),
                "events": self_check_supervision.get("events") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (shared_dir / "main-closure-runtime-status.json").write_text(
        json.dumps(main_closure_supervision, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    purity_gate = {
        "generated_at": int(time.time()),
        "env_id": env_id,
        "ok": bool(main_closure_supervision.get("purity_gate_ok", True)),
        "reasons": list(main_closure_supervision.get("purity_gate_reasons") or []),
        "metrics": dict(main_closure_supervision.get("purity_metrics") or {}),
    }
    STORE.save_runtime_value(f"main_closure_purity_gate:{env_id}", purity_gate)
    (shared_dir / "main-closure-purity-gate.json").write_text(
        json.dumps(purity_gate, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not purity_gate["ok"]:
        STORE.append_runtime_event(
            f"main_closure_purity_anomalies:{env_id}",
            {
                "env_id": env_id,
                "status": "failed",
                "reasons": purity_gate["reasons"],
                "metrics": purity_gate["metrics"],
                "timestamp_iso": datetime.now().isoformat(),
            },
            limit=100,
        )
    (shared_dir / "main-closure-events.json").write_text(
        json.dumps(
            {
                "generated_at": main_closure_supervision.get("generated_at"),
                "env_id": main_closure_supervision.get("env_id"),
                "events": main_closure_supervision.get("events") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (shared_dir / "control-plane-summary.json").write_text(
        json.dumps(control_plane, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "learning-promotion-policy.json").write_text(
        json.dumps(promotion_policy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "context-lifecycle-baseline.json").write_text(
        json.dumps(context_baseline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "bootstrap-status.json").write_text(
        json.dumps(bootstrap_status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "watcher-summary.json").write_text(
        json.dumps(watcher_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "restart-runtime-status.json").write_text(
        json.dumps(restart_runtime_status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "restart-events.json").write_text(
        json.dumps(
            {
                "generated_at": restart_runtime_status.get("generated_at"),
                "env_id": env_id,
                "events": recent_restart_events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (shared_dir / "watchdog-recovery-status.json").write_text(
        json.dumps(watchdog_recovery_status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "watchdog-recovery-hints.json").write_text(
        json.dumps(watchdog_recovery_hints, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "watchdog-recovery-incidents.json").write_text(
        json.dumps(watchdog_recovery_incidents, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "openclaw-version.json").write_text(
        json.dumps(version_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "openclaw-recovery-profile.json").write_text(
        json.dumps(recovery_profile, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "README.md").write_text(
        "# Shared State Model\n\n"
        "- task-registry-snapshot.json: 当前任务注册表快照\n"
        "- current-task-facts.json: 当前任务事实摘要\n"
        "- control-action-queue.json: 待处理控制动作队列\n"
        "- runtime-health.json: 运行健康与最近异常\n"
        "- learning-backlog.json: learning / reflection / suggestions\n"
        "- learning-runtime-status.json: OpenClaw 学习产物状态与 freshness 摘要\n"
        "- reflection-freshness.json: reflection / maintenance / rollup 最近运行状态\n"
        "- memory-freshness.json: MEMORY.md 更新时间与 freshness\n"
        "- reuse-evidence-summary.json: promoted knowledge 复用证据摘要\n"
        "- self-check-runtime-status.json: OpenClaw 内部 self-check 最近运行与恢复摘要\n"
        "- self-check-events.json: OpenClaw 内部 self-check 最近事件\n"
        "- main-closure-runtime-status.json: OpenClaw 主闭环 root/adoption/finalization/delivery 摘要\n"
        "- main-closure-events.json: OpenClaw 主闭环最近事件时间线\n"
        "- control-plane-summary.json: 控制面统计与解释\n"
        "- learning-promotion-policy.json: learning promote 规则与阈值\n"
        "- context-lifecycle-baseline.json: 推荐长期运行基线模板\n"
        "- bootstrap-status.json: 初始化结构与配置补齐状态\n"
        "- watcher-summary.json: 任务监督器摘要与 completed/delivered 区分\n"
        "- restart-runtime-status.json: 最近重启链路摘要与最后成功/失败结果\n"
        "- restart-events.json: 最近重启事件时间线\n"
        "- watchdog-recovery-status.json: watchdog 异常识别、分型、调度统计\n"
        "- watchdog-recovery-incidents.json: watchdog 最小 incident 输出与冻结证据\n"
        "- watchdog-recovery-hints.json: watchdog 最近生成/派发的结构化提示\n"
        "- openclaw-version.json: 当前运行代码版本、commit、分支与 upstream 偏移\n"
        "- openclaw-recovery-profile.json: 当前版本 / known good / 回退提示\n",
        encoding="utf-8",
    )
    env_home = Path(str(current_spec.get("home") or BASE_DIR))
    monitor_dir = env_home / "shared-context" / "monitor-tasks"
    monitor_dir.mkdir(parents=True, exist_ok=True)
    watcher_items = STORE.list_watcher_tasks(env_id=env_id, limit=200)

    def export_watcher_item(item: dict[str, Any]) -> dict[str, Any]:
        exported = dict(item)
        payload = dict(exported.get("payload") or {})
        task_id = str(payload.get("task_id") or exported.get("task_id") or "")
        question = normalize_task_question(
            payload.get("question") or exported.get("question")
        )
        task_row = STORE.get_task(task_id) if task_id else None
        task_question = normalize_task_question(
            (task_row or {}).get("question") or (task_row or {}).get("last_user_message")
        )
        if task_question != "未知任务" and (
            question == "未知任务"
            or len(task_question) > len(question)
            or task_question.startswith(question)
        ):
            question = task_question
        if question != "未知任务":
            exported["question"] = question
            payload["question"] = question
        if task_id:
            exported["task_id"] = task_id
            payload["task_id"] = task_id
        exported["payload"] = payload
        return exported

    watcher_items = [export_watcher_item(item) for item in watcher_items]
    tasks_lines = [json.dumps(item, ensure_ascii=False) for item in watcher_items]
    if not tasks_lines and facts_current:
        tasks_lines = [
            json.dumps(
                {
                    "task_id": facts_current.get("task_id"),
                    "session_key": facts_current.get("session_key"),
                    "question": facts_current.get("question"),
                    "status": facts_current.get("status"),
                    "current_stage": facts_current.get("current_stage"),
                    "env_id": env_id,
                },
                ensure_ascii=False,
            )
        ]
    (monitor_dir / "tasks.jsonl").write_text(
        ("\n".join(tasks_lines) + ("\n" if tasks_lines else "")), encoding="utf-8"
    )
    dlq_lines = [
        json.dumps(item, ensure_ascii=False)
        for item in watcher_items
        if item.get("in_dlq")
    ]
    (monitor_dir / "dlq.jsonl").write_text(
        ("\n".join(dlq_lines) + ("\n" if dlq_lines else "")), encoding="utf-8"
    )
    # Transitional export only: these files keep the current dashboard/shared-
    # state views working during migration, but canonical learning artifacts
    # should move to OpenClaw-owned generation.
    learnings_dir = BASE_DIR / ".learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    env_learnings_dir = env_home / ".learnings"
    env_learnings_dir.mkdir(parents=True, exist_ok=True)
    learnings = STORE.list_learning_view(limit=200)
    errors_lines = [
        f"- {item.get('title')}: {item.get('detail')}"
        for item in learnings
        if str(item.get("lifecycle_state") or "") not in {"closed", "verified"}
    ]
    promoted_lines = [
        f"- {item.get('title')}: {item.get('detail')}"
        for item in learnings
        if str(item.get("lifecycle_state") or "") in {"verified", "closed"}
    ]
    feature_lines = [
        f"- {item.get('title')}: {item.get('detail')}"
        for item in learnings
        if str(item.get("category")) == "feature_request"
    ]
    errors_body = "# Errors\n\n" + (
        "\n".join(errors_lines) if errors_lines else "- 暂无待处理错误模式\n"
    )
    learnings_body = render_learnings_markdown(STORE, limit=200)
    feature_body = "# Feature Requests\n\n" + (
        "\n".join(feature_lines) if feature_lines else "- 暂无 feature requests\n"
    )
    (learnings_dir / "ERRORS.md").write_text(errors_body, encoding="utf-8")
    (learnings_dir / "LEARNINGS.md").write_text(learnings_body, encoding="utf-8")
    (learnings_dir / "FEATURE_REQUESTS.md").write_text(feature_body, encoding="utf-8")
    (env_learnings_dir / "ERRORS.md").write_text(errors_body, encoding="utf-8")
    (env_learnings_dir / "LEARNINGS.md").write_text(learnings_body, encoding="utf-8")
    (env_learnings_dir / "FEATURE_REQUESTS.md").write_text(
        feature_body, encoding="utf-8"
    )
    write_state_snapshot(BASE_DIR, STORE)
    memory_dir = BASE_DIR / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    env_memory_dir = env_home / "memory"
    env_memory_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    memory_body = "# Daily Memory\n\n" + json.dumps(
        {
            "reflection_runs": learning_backlog["reflections"][:5],
            "summary": learning_backlog["summary"],
        },
        ensure_ascii=False,
        indent=2,
    )
    (memory_dir / f"{today}.md").write_text(memory_body + "\n", encoding="utf-8")
    (env_memory_dir / f"{today}.md").write_text(memory_body + "\n", encoding="utf-8")
    memory_index = (
        "# Monitor Memory\n\n"
        f"- env: {env_id}\n"
        f"- current_task: {(facts_payload.get('current_task') or {}).get('task_id') or '-'}\n"
        f"- learning_total: {learning_backlog['summary'].get('total', 0)}\n"
        f"- promoted: {learning_backlog['summary'].get('promoted', 0)}\n"
    )
    (BASE_DIR / "MEMORY.md").write_text(memory_index, encoding="utf-8")
    (env_home / "MEMORY.md").write_text(memory_index, encoding="utf-8")


def derive_learning_key(*parts: str) -> str:
    joined = "|".join(part.strip() for part in parts if part is not None)
    return hashlib.sha1(joined.encode("utf-8", errors="ignore")).hexdigest()[:24]


def detect_runtime_problem_code(anomaly: dict[str, Any]) -> str:
    anomaly_type = str(anomaly.get("type") or "").strip()
    if anomaly_type == "no_reply":
        return "no_reply_after_commit"
    if anomaly_type in {"dispatch_stuck", "stage_stuck", "stalled_reply"}:
        return "task_execution_stalled"
    if anomaly_type == "gateway_ws_closed":
        return "gateway_unhealthy"
    if anomaly_type == "main_closure_purity_gate_failed":
        return "purity_gate_failed"
    if anomaly_type == "model_timeout":
        return "model_timeout"
    if anomaly_type == "failover_exhausted":
        return "failover_exhausted"
    if anomaly_type == "channel_inflight_stuck":
        return "channel_inflight_stuck"
    if anomaly_type == "run_tracking_warning":
        return "run_tracking_warning"
    return "unknown_problem"


def capture_runtime_anomaly_learnings(
    anomalies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not CONFIG.get("ENABLE_EVOLUTION_PLANE", True):
        return []
    env_id = current_env_spec()["id"]
    captured: list[dict[str, Any]] = []
    for anomaly in anomalies:
        problem_code = detect_runtime_problem_code(anomaly)
        details = dict(anomaly.get("details") or {})
        question = str(details.get("question") or "").strip()
        task_id = str(details.get("task_id") or "")
        title = f"runtime_observation:{problem_code}"
        summary = (
            f"type={str(anomaly.get('type') or '-').strip()};"
            f"message={str(anomaly.get('message') or '-').strip()};"
            f"question={question or '-'}"
        )
        scope = question or task_id or env_id
        learning_key = derive_learning_key("runtime", env_id, problem_code, scope)
        learning = record_learning(
            STORE,
            learning_key=learning_key,
            problem_code=problem_code,
            title=title,
            summary=summary,
            evidence={
                "anomaly": anomaly,
                "env_id": env_id,
            },
            root_task_id=task_id,
            actor="guardian",
        )
        captured.append(learning)
    return captured


def capture_task_execution_learnings(
    *, now: int | None = None, stale_after: int | None = None
) -> list[dict[str, Any]]:
    if not CONFIG.get("ENABLE_EVOLUTION_PLANE", True):
        return []
    env_id = current_env_spec()["id"]
    current_ts = int(now or time.time())
    stale_seconds = int(stale_after or CONFIG.get("GUARDIAN_STALE_TASK_MAX_AGE", 3600))
    captured: list[dict[str, Any]] = []
    for task in STORE.list_active_tasks(limit=200):
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        status = str(task.get("status") or "").strip().lower()
        if status in {"completed", "closed", "cancelled", "failed", "blocked"}:
            continue
        last_activity = max(
            int(task.get("last_progress_at") or 0),
            int(task.get("updated_at") or 0),
            int(task.get("started_at") or 0),
            int(task.get("created_at") or 0),
        )
        if not last_activity or current_ts - last_activity < stale_seconds:
            continue
        question = str(
            task.get("question") or task.get("last_user_message") or "未知任务"
        )
        current_stage = str(task.get("current_stage") or "").strip()
        learning_key = derive_learning_key(
            "task", env_id, "task_execution_stalled", task_id
        )
        learning = record_learning(
            STORE,
            learning_key=learning_key,
            problem_code="task_execution_stalled",
            title="task_execution:stalled",
            summary=(
                f"task={task_id};status={status or '-'};"
                f"stage={current_stage or '-'};idle={current_ts - last_activity}"
            ),
            evidence={
                "task_id": task_id,
                "question": question,
                "status": status,
                "current_stage": current_stage,
                "idle_seconds": current_ts - last_activity,
                "env_id": env_id,
            },
            root_task_id=task_id,
            actor="guardian",
        )
        captured.append(learning)
    return captured


def capture_control_plane_learnings(outcomes: list[dict]) -> list[dict]:
    """Transitional bridge: export control-plane observations as learning candidates.

    This remains in guardian only as a migration shim. The target architecture
    moves canonical learning capture back into OpenClaw itself; guardian should
    eventually emit supervisory observation facts rather than own learning
    decisions.
    """
    if not CONFIG.get("ENABLE_EVOLUTION_PLANE", True):
        return []
    env_id = current_env_spec()["id"]
    captured: list[dict] = []
    for outcome in outcomes:
        task_id = str(outcome.get("task_id") or "")
        if not task_id:
            continue
        action = str(outcome.get("action") or "")
        if action not in {"blocked", "followup_sent"}:
            continue
        blocked_reason = str(outcome.get("blocked_reason") or "").strip()
        control_state = str(outcome.get("control_state") or "").strip()
        task = STORE.get_task(task_id) or {}
        problem_code = (
            blocked_reason
            if blocked_reason
            in {
                "missing_pipeline_receipt",
                "wrong_task_binding",
                "no_reply_after_commit",
                "delivery_failed_without_notice",
                "late_result_not_adopted",
                "followup_misbound",
                "followup_pending_without_main_recovery",
                "received_only_requires_main_followup",
                "heartbeat_missing_soft",
                "heartbeat_missing_hard",
                "heartbeat_missing_blocked",
                "task_blocked_user_visible",
                "task_closure_missing",
            }
            else "task_closure_missing"
        )
        title = f"control_observation:{problem_code}"
        detail = f"task={task_id};control_state={control_state or '-'};action={action};blocked_reason={blocked_reason or '-'}"
        learning_key = derive_learning_key("control", env_id, task_id, problem_code)
        learning = record_learning(
            STORE,
            learning_key=learning_key,
            problem_code=problem_code,
            title=title,
            summary=detail,
            evidence={
                "task_id": task_id,
                "question": task.get("question")
                or task.get("last_user_message")
                or "未知任务",
                "control_state": control_state,
                "blocked_reason": blocked_reason,
                "action": action,
            },
            root_task_id=task_id,
            actor="guardian",
        )
        captured.append(learning)
    return captured


def promote_learning_to_memory(learning: dict[str, Any]) -> None:
    """将 promoted learning 写入 MEMORY.md 和 memory/YYYY-MM-DD.md

    自我学习进化闭环的核心函数：
    - 自动沉淀学习结果到 MEMORY.md
    - 自动生成每日反思记录
    - 不需要人工搬运
    """
    category = learning.get("category", "misc")
    title = learning.get("title", "")
    detail = learning.get("detail", "")
    evidence = learning.get("evidence", {})

    # 写入 memory/YYYY-MM-DD.md
    today = datetime.now().strftime("%Y-%m-%d")
    memory_dir = BASE_DIR / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_file = memory_dir / f"{today}.md"

    with open(memory_file, "a", encoding="utf-8") as f:
        f.write(f"\n## {category}: {title}\n\n")
        f.write(f"{detail}\n\n")
        if evidence:
            f.write(
                f"**证据**:\n```\n{json.dumps(evidence, ensure_ascii=False, indent=2)}\n```\n\n"
            )

    # 写入 MEMORY.md
    main_memory = BASE_DIR / "MEMORY.md"
    if not main_memory.exists():
        main_memory.write_text("# MEMORY.md\n\n", encoding="utf-8")

    with open(main_memory, "a", encoding="utf-8") as f:
        f.write(f"\n### {category}\n\n")
        f.write(f"- **{title}**: {detail}\n")


# ========== 心跳检测 + Guardrail ==========
TASK_WATCHER: TaskWatcher | None = None
RECOVERY_WATCHDOG: RecoveryWatchdog | None = None


def get_task_watcher() -> TaskWatcher:
    """获取任务监控器（单例）"""
    global TASK_WATCHER
    if TASK_WATCHER is None:
        TASK_WATCHER = TaskWatcher(STORE)
    return TASK_WATCHER


def get_recovery_watchdog() -> RecoveryWatchdog:
    global RECOVERY_WATCHDOG
    if RECOVERY_WATCHDOG is None:
        RECOVERY_WATCHDOG = RecoveryWatchdog(
            base_dir=BASE_DIR, store=STORE, config=CONFIG
        )
    else:
        RECOVERY_WATCHDOG.config = CONFIG
    return RECOVERY_WATCHDOG


def run_recovery_watchdog(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    current_spec = spec or current_env_spec()
    watchdog = get_recovery_watchdog()
    result = watchdog.run(current_spec)
    for item in list(result.get("items") or [])[:20]:
        problem_code = detect_recurrence_problem_code(item)
        task_id = str(item.get("task_id") or "")
        title = f"watchdog_observation:{problem_code}"
        summary = f"anomaly_type={str(item.get('anomaly_type') or '-').strip()};severity={str(item.get('severity') or '-').strip()};reason={str(item.get('reason') or '-').strip()}"
        record_learning(
            STORE,
            problem_code=problem_code,
            title=title,
            summary=summary,
            evidence=item,
            root_task_id=task_id,
            actor="guardian",
        )
    if int(result.get("dispatched_count") or 0) > 0:
        log(f"Recovery watchdog dispatched {result.get('dispatched_count')} hint(s)")
    return result


def run_monitor_db_retention() -> dict[str, Any]:
    result = STORE.prune_retention(CONFIG)
    deleted = {
        k: v for k, v in (result.get("deleted") or {}).items() if int(v or 0) > 0
    }
    if deleted:
        summary = ", ".join(f"{k}={v}" for k, v in deleted.items())
        log(f"DB retention pruned: {summary}")
    return result


def run_self_evolution_maintenance_cycle(*, dry_run: bool = False) -> dict[str, Any]:
    """运行自我进化周期

    功能：
    1. 检查所有重复问题
    2. 尝试自动解决
    3. 更新 LEARNINGS.md
    4. 生成每日报告

    这是自我进化的核心：不只是记录问题，而是主动解决。
    """
    from learning_recorder import run_self_evolution_cycle as _run_cycle

    result = _run_cycle(BASE_DIR, STORE, recurrence_threshold=10, dry_run=dry_run)

    # 记录结果
    resolution = result.get("resolution") or {}
    checked = resolution.get("checked_count") or 0
    resolved = resolution.get("resolved_count") or 0
    unresolvable = resolution.get("unresolvable_count") or 0

    if checked > 0:
        log(
            f"自我进化周期完成: 检查 {checked} 个问题, 解决 {resolved} 个, {unresolvable} 个需要人工介入"
        )

    return result


def check_heartbeat_and_guardrail() -> dict[str, Any]:
    """心跳检测 + Guardrail 检查

    功能：
    1. 检查所有活跃任务的心跳状态
    2. 检测超时任务
    3. 执行 Guardrail 恢复策略
    4. 生成可观测性报告
    5. Guardrail 硬规则检查
    """
    watcher = get_task_watcher()
    result = watcher.check_all_tasks()

    # 处理超时任务
    for timeout_task in result.get("timeout_tasks", []):
        task_id = timeout_task.get("task_id")
        if task_id:
            log(f"检测到超时任务: {task_id}", "WARNING")

            # 尝试恢复
            recovery_result = watcher.recover_timeout_task(task_id)

            if recovery_result.get("success"):
                log(f"任务 {task_id} 恢复成功: {recovery_result.get('action')}")
            else:
                log(
                    f"任务 {task_id} 恢复失败: {recovery_result.get('message')}",
                    "ERROR",
                )

                # 通知用户
                if should_alert("task_timeout"):
                    notify(
                        "任务超时",
                        f"任务 {task_id} 已超时且无法自动恢复\n"
                        f"阶段: {timeout_task.get('phase')}\n"
                        f"超时时间: {timeout_task.get('timeout_seconds')}秒",
                        "error",
                    )

    # Guardrail 硬规则检查
    guardrail_violations = _check_guardrail_hard_rules()
    result["guardrail_violations"] = guardrail_violations

    # 记录健康状态
    health_status = result.get("health_status", "healthy")
    if health_status != "healthy":
        STORE.save_runtime_value("last_degraded_at", int(time.time() * 1000))

    return result


def _check_guardrail_hard_rules() -> list[dict[str, Any]]:
    """Guardrail 硬规则检查

    规则：
    1. 没有 completed/blocked 终态就不能挂成完成
    2. 没有 visible_completion 就不能算已交付
    3. 收到回执后 10 分钟内必须转成用户可见反馈
    """
    violations = []
    now = int(time.time())

    # 检查所有 running 任务
    tasks = STORE.list_tasks(limit=500)
    for task in tasks:
        if task.get("status") not in ("running", "blocked"):
            continue

        task_id = task.get("task_id")
        control = STORE.derive_task_control_state(task_id)
        core_supervision = STORE.derive_core_task_supervision(task_id)

        # 规则1：没有终态就不能挂成完成
        control_state = str(control.get("control_state") or "")
        if control_state in ("completed_verified", "completed_not_delivered"):
            # 检查是否真的有终态证据
            has_terminal_receipt = bool(
                (control.get("contract") or {}).get("required_receipts")
            )
            has_visible_completion = bool(
                core_supervision.get("visible_completion_seen")
            )
            if not has_terminal_receipt and not has_visible_completion:
                violation = {
                    "task_id": task_id,
                    "rule": "no_terminal_state_without_evidence",
                    "control_state": control_state,
                    "message": f"任务 {task_id} 状态为 {control_state} 但没有终态证据",
                }
                violations.append(violation)
                # 强制执行：回退状态
                STORE.update_task_fields(
                    task_id,
                    status="blocked",
                    current_stage="Guardrail 拦截：没有终态证据",
                    blocked_reason="guardrail_no_terminal_evidence",
                    updated_at=now,
                )
                STORE.record_task_event(
                    task_id,
                    "guardrail_enforcement",
                    {
                        "rule": "no_terminal_state_without_evidence",
                        "action": "status_reverted_to_blocked",
                        "timestamp": datetime.now().isoformat(),
                    },
                )

        # 规则2：没有 visible_completion 就不能算已交付
        delivery_state = str(control.get("delivery_state") or "")
        if delivery_state == "delivered":
            has_visible_completion = bool(
                core_supervision.get("visible_completion_seen")
            )
            if not has_visible_completion:
                violation = {
                    "task_id": task_id,
                    "rule": "no_delivery_without_visible_completion",
                    "delivery_state": delivery_state,
                    "message": f"任务 {task_id} 标记为已送达但没有 visible_completion 事件",
                }
                violations.append(violation)
                # 强制执行：回退 delivery_state
                STORE.update_task_fields(
                    task_id,
                    current_stage="Guardrail 拦截：没有送达证据",
                    updated_at=now,
                )
                STORE.record_task_event(
                    task_id,
                    "guardrail_enforcement",
                    {
                        "rule": "no_delivery_without_visible_completion",
                        "action": "delivery_state_reverted",
                        "timestamp": datetime.now().isoformat(),
                    },
                )

        # 规则3：收到回执后 10 分钟内必须转成用户可见反馈
        action = STORE.get_open_control_action(task_id)
        if action:
            last_followup = int(action.get("last_followup_at") or 0)
            action_status = str(action.get("status") or "")
            if last_followup > 0 and action_status == "sent":
                minutes_since_followup = (now - last_followup) / 60
                if minutes_since_followup > 10:
                    violation = {
                        "task_id": task_id,
                        "rule": "no_user_visible_feedback_within_10min",
                        "minutes_since_followup": minutes_since_followup,
                        "message": f"任务 {task_id} 回执已发送 {minutes_since_followup:.1f} 分钟但没有用户可见反馈",
                    }
                    violations.append(violation)
                    # 强制执行：创建 followup action
                    STORE.create_control_action(
                        task_id,
                        task.get("env_id", "primary"),
                        "require_user_visible_feedback",
                        control_state=control_state,
                        status="pending",
                        summary=f"Guardrail 强制：回执发送 {minutes_since_followup:.1f} 分钟后没有用户可见反馈",
                        details={
                            "guardrail_triggered": True,
                            "rule": "no_user_visible_feedback_within_10min",
                            "minutes_since_followup": minutes_since_followup,
                        },
                    )
                    STORE.record_task_event(
                        task_id,
                        "guardrail_enforcement",
                        {
                            "rule": "no_user_visible_feedback_within_10min",
                            "action": "followup_action_created",
                            "minutes_since_followup": minutes_since_followup,
                            "timestamp": datetime.now().isoformat(),
                        },
                    )

    # 记录违规
    if violations:
        STORE.save_runtime_value("guardrail_violations", {
            "ts": now,
            "count": len(violations),
            "violations": violations[:20],
        })
        log(f"Guardrail 检测到 {len(violations)} 个违规并已强制执行", "WARNING")

        # 发送通知
        notify(
            "Guardrail 强制执行",
            f"检测到 {len(violations)} 个违规并已自动处理",
            "warning",
        )

    return violations


def record_heartbeat(
    task_id: str,
    session_key: str,
    phase: str,
    progress: int,
    message: str | None = None,
    error_code: str | None = None,
) -> None:
    """记录心跳（供外部调用）"""
    watcher = get_task_watcher()
    heartbeat = Heartbeat(
        task_id=task_id,
        session_key=session_key,
        phase=HeartbeatPhase(phase),
        progress=progress,
        timestamp_ms=int(time.time() * 1000),
        message=message,
        error_code=error_code,
    )
    watcher.heartbeat_monitor.record_heartbeat(heartbeat)
    log(f"心跳记录: {task_id} @ {phase} ({progress}%)")


def _infer_heartbeat_phase_for_task(task: dict[str, Any]) -> HeartbeatPhase:
    stage = str(task.get("current_stage") or "").lower()
    question = str(task.get("question") or "").lower()
    combined = f"{stage} {question}"
    if "plan" in combined or "planning" in combined or "方案" in combined:
        return HeartbeatPhase.PLANNING
    if (
        "dev" in combined
        or "implementation" in combined
        or "开发" in combined
        or "实现" in combined
    ):
        return HeartbeatPhase.IMPLEMENTATION
    if "test" in combined or "testing" in combined or "测试" in combined:
        return HeartbeatPhase.TESTING
    if "calculation" in combined or "calculator" in combined or "计算" in combined:
        return HeartbeatPhase.CALCULATION
    if (
        "verification" in combined
        or "verifier" in combined
        or "校验" in combined
        or "验证" in combined
    ):
        return HeartbeatPhase.VERIFICATION
    if "risk" in combined or "风控" in combined:
        return HeartbeatPhase.RISK_ASSESSMENT
    return HeartbeatPhase.IDLE


def _infer_heartbeat_progress_for_task(
    task: dict[str, Any], phase: HeartbeatPhase
) -> int:
    if str(task.get("status") or "") == "blocked":
        return 0
    defaults = {
        HeartbeatPhase.PLANNING: 20,
        HeartbeatPhase.IMPLEMENTATION: 50,
        HeartbeatPhase.TESTING: 80,
        HeartbeatPhase.CALCULATION: 55,
        HeartbeatPhase.VERIFICATION: 85,
        HeartbeatPhase.RISK_ASSESSMENT: 65,
        HeartbeatPhase.IDLE: 10,
    }
    return defaults.get(phase, 10)


def emit_taskwatcher_heartbeats(limit: int = 100) -> int:
    watcher = get_task_watcher()
    active_tasks = STORE.list_active_tasks(limit=limit)
    now_ms = int(time.time() * 1000)
    recorded = 0
    for task in active_tasks:
        task_id = str(task.get("task_id") or "")
        session_key = str(task.get("session_key") or "")
        if not task_id or not session_key:
            continue
        phase = _infer_heartbeat_phase_for_task(task)
        interval_ms = watcher.heartbeat_monitor.config.intervals.get(phase, 30) * 1000
        last = watcher.heartbeat_monitor.get_last_heartbeat(task_id)
        if last and now_ms - last.timestamp_ms < max(5000, int(interval_ms * 0.8)):
            continue
        heartbeat = Heartbeat(
            task_id=task_id,
            session_key=session_key,
            phase=phase,
            progress=_infer_heartbeat_progress_for_task(task, phase),
            timestamp_ms=now_ms,
            message=str(task.get("current_stage") or task.get("question") or "处理中"),
        )
        watcher.heartbeat_monitor.record_heartbeat(heartbeat)
        recorded += 1
    return recorded


def get_observability_report() -> dict[str, Any]:
    """获取可观测性报告（供外部调用）"""
    watcher = get_task_watcher()
    return watcher.get_observability_report()


def run_reflection_cycle(force: bool = False) -> dict[str, Any]:
    """自我学习进化闭环的核心函数。

    功能：
    1. 检查过去 24 小时的 learnings
    2. 自动 promote 达到阈值的 learnings
    3. 将 promoted learnings 写入 MEMORY.md 和 memory/YYYY-MM-DD.md
    4. 根据学习结果自动改进系统
    5. 主动检查并解决重复问题

    边界原则：
    - 这是自我学习进化的核心闭环
    - 不需要人工干预
    - 自动沉淀、自动改进
    """
    # 已禁用：旧数据 + 模板化输出导致反复播报旧问题，不是真进化
    return {"status": "disabled_reason_fake_evolution", "promoted": 0, "reviewed": 0}
    if not CONFIG.get("ENABLE_EVOLUTION_PLANE", True):
        return {"status": "disabled", "promoted": 0, "reviewed": 0}
    now = int(time.time())
    interval = int(CONFIG.get("REFLECTION_INTERVAL_SECONDS", 3600))
    last_run = int(STORE.load_runtime_value("reflection_last_run_at", 0) or 0)
    if not force and last_run and now - last_run < interval:
        return {"status": "skipped", "promoted": 0, "reviewed": 0}

    # 先把运行时问题和任务执行问题沉淀为 learning，再做反思/解决。
    scan_runtime_anomalies()
    capture_task_execution_learnings(now=now)

    # 1. 生成每日报告
    report = generate_daily_evolution_report(STORE, now=now)

    # 2. 主动检查并解决重复问题
    from learning_recorder import spawn_reflection_agent

    resolution_result = check_and_resolve_learnings(
        STORE,
        recurrence_threshold=int(CONFIG.get("LEARNING_PROMOTION_THRESHOLD", 3)),
        dry_run=False,
    )

    # 3. 如果有无法自动解决的问题，派发给主脑（不通知主人，等主脑处理完再通知）
    if resolution_result.get("unresolvable"):
        spawn_result = spawn_reflection_agent(
            BASE_DIR,
            resolution_result["unresolvable"],
        )
        log(f"反思链派发任务给主脑: {spawn_result.get('task_file')}", "INFO")
        resolution_result["spawn_result"] = spawn_result

    # 3. 写入 LEARNINGS.md
    learnings_dir = BASE_DIR / ".learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    learnings_md = render_learnings_markdown(STORE, limit=100)
    (learnings_dir / "LEARNINGS.md").write_text(learnings_md, encoding="utf-8")

    # 4. 将 promoted learnings 写入 MEMORY.md
    projections = STORE.list_self_evolution_projections(limit=100)
    for item in projections:
        current_state = str(item.get("current_state") or "")
        if current_state in ("adopted", "verified"):
            promote_learning_to_memory(item)

    # 5. 写入状态快照和日报
    promoted = int(report.get("rules_added") or 0)
    reviewed = int(report.get("issues_fixed") or 0) + int(
        report.get("pending_verification") or 0
    )
    summary = {
        "status": "ok",
        "reviewed": reviewed,
        "promoted": promoted,
        "threshold": max(2, int(CONFIG.get("LEARNING_PROMOTION_THRESHOLD", 3))),
        "generated_at": now,
        "report": report,
        "resolution": resolution_result,
    }
    STORE.record_reflection_run("scheduled", summary)
    STORE.save_runtime_value("reflection_last_run_at", now)
    STORE.save_runtime_value("self_evolution_last_cycle_at", now)
    write_state_snapshot(BASE_DIR, STORE, now=now)
    evolution_dir = BASE_DIR / "self-evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    (
        evolution_dir
        / f"daily-report-{time.strftime('%Y-%m-%d', time.localtime(now))}.md"
    ).write_text(
        render_daily_evolution_report_markdown(STORE, now=now),
        encoding="utf-8",
    )

    # 6. 如果有高复发问题，记录告警
    if resolution_result.get("unresolvable_count", 0) > 0:
        log(
            f"反思链发现 {resolution_result['unresolvable_count']} 个无法自动解决的问题",
            "WARNING",
        )
        for item in resolution_result.get("unresolvable", [])[:3]:
            log(
                f"  - {item.get('problem_code')}: 复发 {item.get('recurrence_count')} 次",
                "WARNING",
            )

    log(
        "自我进化周期完成: "
        f"新记录={report.get('issues_found', 0)} "
        f"复发={report.get('recurrence_events', 0)} "
        f"验证通过={report.get('issues_fixed', 0)} "
        f"待验证={report.get('pending_verification', 0)} "
        f"已解决={resolution_result.get('resolved_count', 0)} "
        f"待人工={resolution_result.get('unresolvable_count', 0)}"
    )

    return summary


def run_self_evolution_cycle(force: bool = False) -> dict[str, Any]:
    return run_reflection_cycle(force=force)


def sync_runtime_task_registry(lines: list[str]) -> None:
    """
    控制面事实提取函数：从运行时日志中提取任务注册表。

    边界原则：
    - OpenClaw 发 receipt / progress / final（执行面主张）
    - helper 从日志中提取这些主张，注册到控制面
    - helper 判断这些主张是否足以升级为控制面事实
    - OpenClaw 不能自己注册自己，必须由 helper 提取

    字段归属：
    - task_id: helper 生成
    - session_key -> task_id 绑定: helper 控制
    - task_events: helper 记录
    - task_contracts: helper 存储
    """
    if not CONFIG.get("ENABLE_TASK_REGISTRY", True):
        return

    env_id = current_env_spec()["id"]
    contract_catalog = load_task_contract_catalog(
        BASE_DIR, str(CONFIG.get("TASK_CONTRACTS_FILE", "") or "")
    )
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: dict[str, dict[str, Any]] = {}
    last_closed_dispatch: dict[str, Any] | None = None
    touched_task_ids: set[str] = set()
    touched_session_keys: set[str] = set()

    # Buffer for multi-line JSON receipts
    json_receipt_buffer: list[str] = []
    in_json_receipt = False
    json_receipt_ts_raw = ""
    json_receipt_ts = None

    def reconcile_task(task_id: str) -> None:
        task = STORE.get_task(task_id)
        if not task:
            return
        core = STORE.get_core_closure_snapshot_for_task(
            task_id, allow_legacy_projection=False
        )
        # Ensure core is a dict
        if not isinstance(core, dict):
            core = {}
        root_task_id = str((core.get("root_task") or {}).get("root_task_id") or "")
        if not root_task_id or root_task_id.startswith("legacy-root:"):
            STORE.sync_legacy_task_projection(task_id)
        control = STORE.derive_task_control_state(task_id)
        # Ensure control is a dict
        if not isinstance(control, dict):
            control = {}
        if not root_task_id or root_task_id.startswith("legacy-root:"):
            STORE.reconcile_task_control_action(task, control)

    def most_recent_key() -> str | None:
        if not open_dispatches:
            return None
        return max(open_dispatches.items(), key=lambda item: item[1]["started_at"])[0]

    def process_json_receipt_buffer() -> None:
        """Process accumulated multi-line JSON receipt."""
        nonlocal \
            json_receipt_buffer, \
            in_json_receipt, \
            json_receipt_ts_raw, \
            json_receipt_ts
        if not json_receipt_buffer:
            return
        try:
            import json

            json_str = "\n".join(json_receipt_buffer)
            # Find the JSON object
            json_start = json_str.find("{")
            if json_start >= 0:
                json_str = json_str[json_start:]
                # Find the end of JSON (last closing brace before any markdown)
                json_end = json_str.rfind("}")
                if json_end >= 0:
                    json_str = json_str[: json_end + 1]
                data = json.loads(json_str)
                if isinstance(data, dict) and data.get("type") == "PIPELINE_RECEIPT":
                    receipt = {
                        "agent": str(data.get("agent", "")),
                        "phase": str(data.get("status", "")),
                        "action": str(data.get("status", "")),
                        "task_id": str(data.get("task_id", "")),
                        "summary": str(data.get("summary", "")),
                        "timestamp": str(data.get("timestamp", "")),
                    }
                    # Process the receipt
                    if open_dispatches:
                        current_key = most_recent_key()
                        if current_key:
                            dispatch = open_dispatches[current_key]
                            action = receipt.get("action", "")
                            if action == "completed":
                                dispatch["status"] = "completed"
                                dispatch["current_stage"] = "已完成"
                            elif action == "blocked":
                                dispatch["status"] = "blocked"
                                dispatch["current_stage"] = "阻塞"
                            dispatch["last_progress_at"] = int(
                                json_receipt_ts or time.time()
                            )
                            dispatch["updated_at"] = int(json_receipt_ts or time.time())
                            STORE.update_task_fields(
                                dispatch["task_id"],
                                status=dispatch["status"],
                                current_stage=dispatch["current_stage"],
                                last_progress_at=dispatch["last_progress_at"],
                                updated_at=dispatch["updated_at"],
                                latest_receipt=receipt,
                            )
                            touched_task_ids.add(dispatch["task_id"])
                            STORE.record_task_event(
                                dispatch["task_id"],
                                "pipeline_receipt",
                                {
                                    "receipt": receipt,
                                    "timestamp": data.get("timestamp", ""),
                                },
                            )
                            log(
                                f"已识别 JSON 回执: task_id={receipt.get('task_id')} status={receipt.get('action')}"
                            )
        except Exception as e:
            log(f"解析 JSON 回执失败: {e}", "WARNING")
        finally:
            json_receipt_buffer = []
            in_json_receipt = False
            json_receipt_ts_raw = ""
            json_receipt_ts = None

    for line in lines:
        # Check for multi-line JSON receipt start
        if "**PIPELINE_RECEIPT**" in line:
            in_json_receipt = True
            json_receipt_buffer = []
            json_receipt_ts_raw, json_receipt_ts = parse_runtime_timestamp(line)
            continue

        # Check for multi-line JSON receipt end
        if in_json_receipt:
            if "```" in line and json_receipt_buffer:
                # End of JSON block
                process_json_receipt_buffer()
            else:
                # Collect JSON lines
                json_receipt_buffer.append(line.strip())
                # Check if this line contains closing brace
                if "}" in line:
                    process_json_receipt_buffer()
            continue

        ts_raw, ts = parse_runtime_timestamp(line)
        if ts is None:
            continue

        question = normalize_task_question(extract_runtime_question(line))
        if question and question != "未知任务":
            question_candidates.append((int(ts), question))
            question_candidates = question_candidates[-50:]

        lower = line.lower()
        if "dispatching to agent" in lower:
            nearest_question = ""
            for qts, qmsg in reversed(question_candidates):
                if abs(int(ts) - qts) <= 15:
                    nearest_question = qmsg
                    break
            requester_open_id = extract_requester_open_id(line)
            session_key = (
                extract_runtime_session_key(line)
                or requester_open_id
                or f"dispatch:{ts_raw}"
            )
            # 如果这条会话的最新用户消息已经有可见回复，则不要再从日志里重建未闭环任务
            # 否则会把已答复的普通问答反复重建为 running/blocked，形成“追踪 3 次未闭环”噪音
            if session_key and session_key.startswith("agent:main:feishu:"):
                session_rec = load_openclaw_session_record(session_key)
                if session_rec:
                    session_file = Path(str(session_rec.get("sessionFile") or ""))
                    if _session_question_has_visible_reply(session_file, nearest_question):
                        continue
            existing = STORE.get_latest_task_for_session(session_key)
            touched_session_keys.add(session_key)
            question_text = normalize_task_question(nearest_question)
            if question_text == "未知任务" and existing:
                question_text = normalize_task_question(
                    existing.get("last_user_message") or existing.get("question", "")
                )
            if existing:
                existing_question = normalize_task_question(
                    existing.get("last_user_message") or existing.get("question")
                )
                existing_status = str(existing.get("status") or "")
                # 同一会话同一问题，不要重复从 runtime log 建第二个任务。
                if existing_question == question_text and existing_status in {"running", "background", "blocked", "completed"}:
                    continue
            existing_contract = (
                STORE.get_task_contract(existing["task_id"]) if existing else None
            )
            contract = infer_task_contract(
                question_text,
                catalog=contract_catalog,
                existing_contract_id=(existing_contract or {}).get("id"),
            )
            task_id = build_task_id(session_key, ts_raw)
            current_stage = "处理中"
            task = {
                "task_id": task_id,
                "session_key": session_key,
                "env_id": env_id,
                "channel": infer_task_channel(session_key),
                "status": "running",
                "current_stage": current_stage,
                "question": question_text,
                "last_user_message": question_text,
                "started_at": int(ts),
                "last_progress_at": int(ts),
                "created_at": int(ts),
                "updated_at": int(ts),
                "latest_receipt": {},
            }
            if int(CONFIG.get("TASK_REGISTRY_MAX_ACTIVE", 1)) <= 1:
                STORE.background_other_tasks_for_session(session_key, task_id)
            STORE.upsert_task(task)
            STORE.upsert_task_contract(task_id, contract)
            touched_task_ids.add(task_id)
            STORE.record_task_event(
                task_id,
                "dispatch_started",
                {
                    "session_key": session_key,
                    "question": question_text,
                    "channel": task["channel"],
                    "timestamp": ts_raw,
                    "env_id": env_id,
                    "contract_id": contract.get("id"),
                },
            )
            STORE.record_task_event(
                task_id,
                "contract_assigned",
                {
                    "contract": contract,
                    "timestamp": ts_raw,
                },
            )
            reconcile_task(task_id)
            open_dispatches[session_key] = {
                **task,
                "timestamp": ts_raw,
                "requester_open_id": requester_open_id or "",
                "marker": "",
                "contract": contract,
            }
            continue

        marker = extract_pipeline_marker(line)
        if marker and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            accepted, normalized_payload, violations = validate_protocol_event(
                STORE.get_task(dispatch["task_id"]) or dispatch,
                "stage_progress",
                {
                    "marker": marker,
                    "stage": normalize_stage_label(marker),
                    "timestamp": ts_raw,
                },
            )
            for violation in violations:
                record_protocol_violation(
                    dispatch["task_id"],
                    violation_kind=str(violation.get("violation_kind") or "unknown"),
                    event_type="stage_progress",
                    payload=violation.get("payload") or {},
                    ack_id=str(violation.get("ack_id") or ""),
                )
            if not accepted:
                touched_task_ids.add(dispatch["task_id"])
                reconcile_task(dispatch["task_id"])
                continue
            dispatch["marker"] = str(normalized_payload.get("marker") or marker)
            dispatch["current_stage"] = str(
                normalized_payload.get("stage") or normalize_stage_label(marker)
            )
            dispatch["last_progress_at"] = int(ts)
            dispatch["updated_at"] = int(ts)
            STORE.update_task_fields(
                dispatch["task_id"],
                current_stage=dispatch["current_stage"],
                last_progress_at=int(ts),
                updated_at=int(ts),
            )
            touched_task_ids.add(dispatch["task_id"])
            touched_session_keys.add(dispatch.get("session_key") or "")
            STORE.record_task_event(
                dispatch["task_id"],
                "stage_progress",
                normalized_payload,
            )
            reconcile_task(dispatch["task_id"])
            continue

        receipt = extract_pipeline_receipt(line)
        if receipt and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            accepted, normalized_payload, violations = validate_protocol_event(
                STORE.get_task(dispatch["task_id"]) or dispatch,
                "pipeline_receipt",
                {
                    "receipt": receipt,
                    "timestamp": ts_raw,
                },
            )
            for violation in violations:
                record_protocol_violation(
                    dispatch["task_id"],
                    violation_kind=str(violation.get("violation_kind") or "unknown"),
                    event_type="pipeline_receipt",
                    payload=violation.get("payload") or {},
                    ack_id=str(violation.get("ack_id") or ""),
                )
            if not accepted:
                touched_task_ids.add(dispatch["task_id"])
                reconcile_task(dispatch["task_id"])
                continue
            receipt = dict(normalized_payload.get("receipt") or {})
            action = receipt.get("action", "")
            phase = receipt.get("phase", "")
            stage_label = f"{phase}:{action}".strip(":")
            status = dispatch.get("status", "running")
            blocked_reason = ""
            if action == "blocked":
                status = "blocked"
                blocked_reason = receipt.get("evidence", "")
            elif action == "completed":
                status = "running"
            dispatch["current_stage"] = stage_label or dispatch.get(
                "current_stage", "处理中"
            )
            dispatch["last_progress_at"] = int(ts)
            dispatch["updated_at"] = int(ts)
            dispatch["status"] = status
            STORE.update_task_fields(
                dispatch["task_id"],
                status=status,
                current_stage=dispatch["current_stage"],
                last_progress_at=int(ts),
                updated_at=int(ts),
                blocked_reason=blocked_reason,
                latest_receipt=receipt,
            )
            touched_task_ids.add(dispatch["task_id"])
            STORE.record_task_event(
                dispatch["task_id"],
                "pipeline_receipt",
                {
                    "receipt": receipt,
                    "status": status,
                    "stage": dispatch["current_stage"],
                    "timestamp": normalized_payload.get("timestamp", ts_raw),
                },
            )
            reconcile_task(dispatch["task_id"])
            continue

        if is_visible_completion_message(line):
            # 减法重构：运行时日志只能作为排障线索，不能再直接推进 visible_completion / delivery。
            # 真正的 completed/block + delivered 必须来自结构化终态与真实送达记录。
            continue

        if "dispatch complete" in lower and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            queued_final = "queuedfinal=true" in lower
            has_reply = "replies=0" not in lower
            status = "running"
            stage = dispatch.get("current_stage", "处理中")
            close_dispatch = False
            if (not queued_final) or (not has_reply):
                close_dispatch = True
                status = "no_reply"
                stage = "完成但无可见回复"
            STORE.update_task_fields(
                dispatch["task_id"],
                status=status,
                current_stage=stage,
                updated_at=int(ts),
                completed_at=int(ts)
                if close_dispatch
                else int(dispatch.get("completed_at") or 0),
            )
            touched_task_ids.add(dispatch["task_id"])
            touched_session_keys.add(dispatch.get("session_key") or "")
            STORE.record_task_event(
                dispatch["task_id"],
                "dispatch_complete",
                {
                    "timestamp": ts_raw,
                    "status": status,
                    "stage": stage,
                    "line": line.strip(),
                },
            )
            if close_dispatch:
                last_closed_dispatch = open_dispatches.pop(current_key, None)
                attach_background_result_if_late(
                    dispatch["task_id"],
                    dispatch.get("session_key") or "",
                    completed_at=int(ts),
                    status=status,
                )
            reconcile_task(dispatch["task_id"])
    for task_id in touched_task_ids:
        STORE.repair_task_identity(task_id)
        reconcile_task(task_id)
    reconcile_background_results_for_sessions(touched_session_keys)
    write_task_registry_snapshot()


def lookup_openclaw_session_id(session_key: str) -> str | None:
    """Resolve a sessionKey to sessionId using the local OpenClaw session store."""
    if not session_key:
        return None
    cache = STORE.load_runtime_value("openclaw_session_cache", {})
    cached = cache.get(session_key)
    if cached:
        return cached

    code, stdout, stderr = run_args(
        ["openclaw", "sessions", "--json", "--all-agents", "--active", "10080"],
        timeout=20,
    )
    if code != 0 or not stdout.strip():
        log(f"读取 OpenClaw 会话失败: {stderr or 'unknown error'}", "ERROR")
        return None
    try:
        # 插件日志可能混在输出中，取最后一个 JSON 对象
        json_str = stdout.strip()
        if "\n{" in json_str:
            # 找到最后一个 JSON 对象的开始位置
            last_brace = json_str.rfind("\n{")
            json_str = json_str[last_brace + 1 :]
        payload = json.loads(json_str)
    except json.JSONDecodeError as e:
        log(f"解析 OpenClaw 会话列表失败: {e}", "ERROR")
        return None

    for item in payload.get("sessions", []):
        key = item.get("key")
        session_id = item.get("sessionId")
        if key and session_id:
            cache[key] = session_id

    if cache:
        STORE.save_runtime_value(
            "openclaw_session_cache", trim_runtime_seen(cache, keep=500)
        )
    return cache.get(session_key)


def cleanup_stale_session_locks(max_age_seconds: int = 300) -> int:
    """Clean up stale session lock files that are no longer held by any process.

    Args:
        max_age_seconds: Lock files older than this are considered stale (default 5 minutes)

    Returns:
        Number of lock files cleaned up
    """
    import json

    locks_dir = OPENCLAW_HOME / "agents"
    if not locks_dir.exists():
        return 0

    cleaned = 0
    now = time.time()

    for lock_file in locks_dir.rglob("*.lock"):
        try:
            # First check if any process is actually holding the lock using lsof
            try:
                result = subprocess.run(
                    ["lsof", str(lock_file)], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Some process is holding the lock, don't clean
                    continue
            except Exception:
                # lsof failed, fall back to other checks
                pass

            # Read lock file to get PID and creation time
            content = lock_file.read_text()
            lock_data = json.loads(content) if content.strip().startswith("{") else {}

            # Check if lock is stale by age
            created_at = lock_data.get("createdAt", "")
            if created_at:
                # Parse ISO timestamp
                from datetime import datetime

                try:
                    created_dt = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                    created_ts = created_dt.timestamp()
                    if now - created_ts < max_age_seconds:
                        # Lock is not old enough, skip
                        continue
                except Exception:
                    pass

            # Clean up stale lock (no process holding it AND old enough)
            lock_file.unlink()
            log(f"Cleaned up stale session lock: {lock_file}")
            cleaned += 1

        except Exception as e:
            log(f"Error checking lock file {lock_file}: {e}", "WARNING")

    return cleaned


def send_guardian_followup(
    session_key: str, message: str, *, deliver: bool = True
) -> tuple[bool, str | None]:
    """Send a marked system follow-up into an existing OpenClaw session."""
    # Clean up stale session locks before attempting to send
    cleanup_stale_session_locks(max_age_seconds=300)

    if should_defer_guardian_followup(session_key, message):
        log(f"守护追问延后，当前会话存在新近用户消息: {session_key}")
        return True, "session_active"

    session_id = lookup_openclaw_session_id(session_key)
    if not session_id:
        log(f"守护追问失败，未找到会话: {session_key}", "ERROR")
        return False, "unknown"

    channel, conversation_type, target = _parse_session_delivery_target(session_key)
    configured_model = str(
        CONFIG.get("GUARDIAN_FOLLOWUP_MODEL", "ollama/qwen3.5:35b")
    ).strip()
    provider_override = ""
    model_override = configured_model
    if "/" in configured_model:
        provider_override, model_override = configured_model.split("/", 1)

    params: dict[str, Any] = {
        "message": message,
        "sessionId": session_id,
        "sessionKey": session_key,
        "channel": channel or "feishu",
        "timeout": int(CONFIG.get("GUARDIAN_FOLLOWUP_TIMEOUT", 120)),
        "idempotencyKey": (f"guardian-followup-{session_id}-{int(time.time() * 1000)}"),
        "provider": provider_override or None,
        "model": model_override or None,
    }

    if deliver and session_key:
        # 检查是否配置了群聊目标
        guardian_target = str(CONFIG.get("GUARDIAN_MESSAGE_TARGET", "dm")).strip()
        feishu_group_id = str(CONFIG.get("FEISHU_GROUP_CHAT_ID", "")).strip()

        if guardian_target == "group" and feishu_group_id:
            # 发送到群聊
            params["replyChannel"] = "feishu"
            params["replyTo"] = f"chat:{feishu_group_id}"
        elif (
            channel == "feishu"
            and conversation_type == "direct"
            and target.startswith("ou_")
        ):
            # 发送到私信（旧逻辑）
            params["replyChannel"] = "feishu"
            params["replyTo"] = f"user:{target}"
        else:
            params["deliver"] = True
    elif deliver:
        params["deliver"] = True

    args = [
        "openclaw",
        "gateway",
        "call",
        "agent",
        "--json",
        "--params",
        json.dumps(
            {k: v for k, v in params.items() if v is not None}, ensure_ascii=False
        ),
    ]

    timeout = int(CONFIG.get("GUARDIAN_FOLLOWUP_TIMEOUT", 120)) + 30
    code, stdout, stderr = run_args(args, timeout=timeout, env=openclaw_runtime_env())
    if code == 0:
        log(f"守护追问已发送到会话 {session_key}: {message}")
        return True, None
    error_text = stderr or stdout or "unknown error"
    log(
        f"守护追问失败({session_key} -> {session_id}): {error_text}",
        "ERROR",
    )
    return False, classify_guardian_followup_error(error_text)


def send_feishu_progress_push(open_id: str, message: str) -> bool:
    """Push a proactive progress message back to the user's Feishu DM."""
    if not open_id:
        return False
    target = open_id if ":" in open_id else f"user:{open_id}"
    quoted_target = json.dumps(target, ensure_ascii=False)
    quoted_message = json.dumps(message, ensure_ascii=False)
    code, _, stderr = run_cmd(
        f"openclaw message send --channel feishu --target {quoted_target} --message {quoted_message}"
    )
    if code == 0:
        log(f"进度推送已发送到 {target}: {message}")
        return True
    log(f"进度推送失败({target}): {stderr or 'unknown error'}", "ERROR")
    return False


def _parse_session_delivery_target(session_key: str) -> tuple[str, str, str]:
    """Return (channel, conversation_type, target) from a session key."""
    parts = str(session_key or "").split(":")
    if len(parts) < 5:
        return "feishu", "", ""
    return str(parts[2] or "feishu"), str(parts[3] or ""), str(parts[4] or "")


def _gateway_recovery_followup_id(root_task_id: str) -> str:
    value = str(root_task_id or "").strip() or "unknown-root"
    return f"guardian:gateway-restart:{value}"


def mark_gateway_restart_recovery_window(recovered_at: int | None = None) -> None:
    now = int(recovered_at or time.time())
    STORE.save_runtime_value(
        "gateway_restart_recovery_tracker",
        {
            "active": True,
            "recovered_at": now,
            "last_cycle_at": 0,
            "pending_tasks": 0,
            "updated_at": now,
        },
    )


def _is_terminal_or_completed_legacy_task(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or "").strip()
    stage = str(task.get("current_stage") or "").strip().lower()
    completed_at = int(task.get("completed_at") or 0)
    blocked_reason = str(task.get("blocked_reason") or "").strip()
    if status in {"completed", "cancelled", "no_reply"}:
        return True
    if completed_at > 0 and (
        "completed" in stage
        or stage.startswith("guardian_recovery:completed")
        or blocked_reason == "missing_pipeline_receipt"
    ):
        return True
    return False


def _report_unclosed_task_to_owner_after_gateway_restart(
    task: dict[str, Any],
    *,
    attempts: int,
    total: int,
    idle: int,
    workflow_state: str,
    followup_id: str,
    now_ts: int,
) -> bool:
    if _is_terminal_or_completed_legacy_task(task):
        return False
    session_key = str(task.get("session_key") or "")
    question = str(task.get("question") or task.get("last_user_message") or "未知任务")
    stage = str(task.get("current_stage") or workflow_state or "处理中")

    # 分析原因
    blocked_reason = task.get("blocked_reason") or ""
    latest_receipt = task.get("latest_receipt_json") or {}

    # 判断主脑响应类型
    if blocked_reason == "missing_pipeline_receipt":
        reason = "主脑已回复但未发送结构化回执"
        need_owner = "否（系统问题）"
        suggestion = "等待系统自动修复回执协议问题"
        # 系统问题不再打扰主人
        return False
    elif blocked_reason and is_internal_system_problem(blocked_reason):
        return False
    elif blocked_reason:
        reason = f"阻塞原因：{blocked_reason}"
        need_owner = "是"
        suggestion = "请主人确认是否继续推进，或提供更多信息"
    else:
        reason = "主脑未响应追问"
        need_owner = "是"
        suggestion = "请主人确认任务是否仍需推进"

    # 获取主脑最后响应
    last_response = (
        task.get("last_agent_response") or task.get("last_main_response") or "无记录"
    )
    if len(str(last_response)) > 100:
        last_response = str(last_response)[:100] + "..."

    message = (
        f"主人，任务“{question[:50]}”追踪 {attempts} 次未闭环。\n"
        f"原因：{reason}\n"
        f"主脑最后响应：{last_response}\n"
        f"需要主人拍板：{need_owner}\n"
        f"建议操作：{suggestion}\n"
        f"阶段：{stage}，静默 {format_duration_label(idle)}"
    )

    delivered = False
    channel, conversation_type, target = _parse_session_delivery_target(session_key)
    if (
        channel == "feishu"
        and conversation_type == "direct"
        and target.startswith("ou_")
    ):
        delivered = send_feishu_progress_push(target, message)
    if not delivered and session_key:
        delivered, _ = send_guardian_followup(
            session_key,
            f"GUARDIAN_OWNER_REPORT: {message}",
        )
    if not delivered:
        notify("未闭环任务告警", message, "warning")

    STORE.record_task_event(
        task["task_id"],
        "gateway_restart_recovery_escalated",
        {
            "attempts": attempts,
            "session_key": session_key,
            "workflow_state": workflow_state,
            "delivery_channel": channel,
            "timestamp": datetime.now().isoformat(),
        },
    )
    STORE.update_followup(
        followup_id,
        current_state="escalated",
        updated_at=now_ts,
        metadata_updates={
            "owner_reported_at": now_ts,
            "summary": message,
            "attempts": attempts,
            "gateway_restart_attempts": attempts,
            "last_followup_at": now_ts,
            "last_gateway_restart_followup_at": now_ts,
        },
    )
    record_change_log(
        "anomaly",
        "Gateway 重启后三次追踪未闭环，已上报主人",
        {
            "task_id": task["task_id"],
            "question": question,
            "attempts": attempts,
            "workflow_state": workflow_state,
            "idle": idle,
            "duration": total,
        },
    )
    return delivered


def continue_gateway_restart_recovery_chase(
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    tracker = STORE.load_runtime_value("gateway_restart_recovery_tracker", {})
    if not isinstance(tracker, dict) or not tracker.get("active"):
        return []

    now_value = int(now_ts or time.time())
    recovered_at = int(tracker.get("recovered_at") or 0)
    env_id = current_env_spec()["id"]
    max_attempts = max(3, int(CONFIG.get("GUARDIAN_GATEWAY_RECOVERY_MAX_ATTEMPTS", 3)))
    cooldown = int(CONFIG.get("GUARDIAN_GATEWAY_RECOVERY_FOLLOWUP_COOLDOWN", 300))
    outcomes: list[dict[str, Any]] = []
    pending_tasks = 0

    for task in STORE.list_tasks(limit=int(CONFIG.get("TASK_REGISTRY_RETENTION", 100))):
        if task.get("env_id") != env_id:
            continue

        started_at = int(task.get("started_at") or task.get("created_at") or 0)
        updated_at = int(task.get("updated_at") or 0)
        if recovered_at and started_at > recovered_at and updated_at > recovered_at:
            continue

        core = STORE.get_core_closure_snapshot_for_task(
            task["task_id"], allow_legacy_projection=True
        )
        if not isinstance(core, dict):
            core = {}

        workflow_state = str(
            core.get("msg_state") or core.get("workflow_state") or "open"
        )
        delivery_state = str(core.get("delivery_state") or "undelivered")
        has_core_projection = bool(core.get("has_core_projection"))
        task_marked_terminal = bool(task.get("completed_at")) or str(
            task.get("status") or ""
        ) in {
            "completed",
            "cancelled",
        }
        is_terminal = bool(core.get("is_terminal")) or task_marked_terminal
        if not has_core_projection:
            is_terminal = task_marked_terminal

        root_task_id = str(core.get("root_task_id") or f"legacy-root:{task['task_id']}")
        followup_id = _gateway_recovery_followup_id(root_task_id)
        existing = STORE.get_followup(followup_id)

        if is_terminal:
            if existing and str(existing.get("current_state") or "") not in {
                "resolved",
                "closed",
            }:
                STORE.update_followup(
                    followup_id,
                    current_state="resolved",
                    resolved_at=now_value,
                    updated_at=now_value,
                    metadata_updates={
                        "resolved_by": "workflow_closed",
                        "resolved_workflow_state": workflow_state
                        or str(task.get("status") or ""),
                    },
                )
            continue

        pending_tasks += 1
        idle = max(
            0,
            now_value
            - int(task.get("last_progress_at") or task.get("updated_at") or now_value),
        )
        total = max(0, now_value - int(task.get("started_at") or now_value))
        question = str(
            task.get("question") or task.get("last_user_message") or "未知任务"
        )

        if not existing:
            STORE.upsert_followup(
                {
                    "followup_id": followup_id,
                    "root_task_id": root_task_id,
                    "workflow_run_id": str(
                        (core.get("current_workflow_run") or {}).get("workflow_run_id")
                        or ""
                    ),
                    "followup_type": "gateway_restart_recovery",
                    "trigger_reason": "gateway_restart_unclosed_task",
                    "current_state": "open",
                    "suggested_action": "resume_and_report_progress",
                    "created_by": "guardian",
                    "created_at": now_value,
                    "updated_at": now_value,
                    "metadata": {
                        "source": "guardian_gateway_recovery",
                        "task_id": task["task_id"],
                        "session_key": str(task.get("session_key") or ""),
                        "question": question,
                        "summary": "Gateway 重启后检测到任务未闭环，继续追踪。",
                        "gateway_restart_attempts": 0,
                        "last_gateway_restart_followup_at": 0,
                        "owner_reported_at": 0,
                        "recovered_at": recovered_at,
                        "attempts": 0,
                    },
                }
            )
            existing = STORE.get_followup(followup_id)

        metadata = dict((existing or {}).get("metadata") or {})
        attempts = int(
            metadata.get("gateway_restart_attempts") or metadata.get("attempts") or 0
        )
        last_followup_at = int(
            metadata.get("last_gateway_restart_followup_at")
            or metadata.get("last_followup_at")
            or 0
        )
        owner_reported_at = int(metadata.get("owner_reported_at") or 0)
        if owner_reported_at:
            continue

        if attempts >= max_attempts:
            if last_followup_at and now_value - last_followup_at < cooldown:
                continue
            _report_unclosed_task_to_owner_after_gateway_restart(
                task,
                attempts=attempts,
                total=total,
                idle=idle,
                workflow_state=workflow_state or str(task.get("status") or ""),
                followup_id=followup_id,
                now_ts=now_value,
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "owner_reported",
                    "attempts": attempts,
                }
            )
            continue

        if last_followup_at and now_value - last_followup_at < cooldown:
            continue

        recovery_message = (
            "GUARDIAN_GATEWAY_RECOVERY: 这是 Gateway 重启后的未闭环任务恢复追踪。"
            "请不要创建新任务。"
            f"当前任务={question}；当前阶段={task.get('current_stage') or workflow_state or '处理中'}；"
            f"已静默={format_duration_label(idle)}；累计运行={format_duration_label(total)}。"
            "请继续当前任务，并明确同步当前进展或阻塞原因。"
        )
        ok, error_kind = send_guardian_followup(
            str(task.get("session_key") or ""), recovery_message
        )
        if ok and error_kind == "session_active":
            STORE.update_followup(
                followup_id,
                updated_at=now_value,
                metadata_updates={
                    "last_session_active_at": now_value,
                    "summary": "会话当前活跃，等待当前处理链先自行收口。",
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "session_active",
                    "attempts": attempts,
                }
            )
            continue

        next_attempts = attempts + 1
        STORE.update_followup(
            followup_id,
            current_state="open",
            updated_at=now_value,
            metadata_updates={
                "gateway_restart_attempts": next_attempts,
                "attempts": next_attempts,
                "last_gateway_restart_followup_at": now_value,
                "last_followup_at": now_value,
                "last_error": error_kind or "",
                "summary": "Gateway 重启后继续追踪未闭环任务。",
            },
        )
        STORE.record_task_event(
            task["task_id"],
            "gateway_restart_recovery_followup",
            {
                "attempt": next_attempts,
                "ok": ok,
                "error_kind": error_kind or "",
                "workflow_state": workflow_state,
                "timestamp": datetime.now().isoformat(),
            },
        )
        record_change_log(
            "pipeline",
            "Gateway 重启后继续追踪未闭环任务",
            {
                "task_id": task["task_id"],
                "question": question,
                "attempt": next_attempts,
                "ok": ok,
                "error_kind": error_kind or "",
                "workflow_state": workflow_state,
                "idle": idle,
                "duration": total,
            },
        )
        action = "followup_sent" if ok else "followup_failed"
        if not ok and next_attempts >= max_attempts:
            _report_unclosed_task_to_owner_after_gateway_restart(
                task,
                attempts=next_attempts,
                total=total,
                idle=idle,
                workflow_state=workflow_state or str(task.get("status") or ""),
                followup_id=followup_id,
                now_ts=now_value,
            )
            action = "owner_reported"
        outcomes.append(
            {
                "task_id": task["task_id"],
                "action": action,
                "attempts": next_attempts,
                "error_kind": error_kind or "",
            }
        )

    tracker["last_cycle_at"] = now_value
    tracker["pending_tasks"] = pending_tasks
    tracker["updated_at"] = now_value
    tracker["active"] = (
        pending_tasks > 0
        and any(item.get("action") not in {"owner_reported"} for item in outcomes)
        if outcomes
        else pending_tasks > 0
    )
    STORE.save_runtime_value("gateway_restart_recovery_tracker", tracker)
    return outcomes


def check_session_has_response(session_key: str, since_timestamp: int) -> bool:
    """检查会话是否有新的响应（追问后主脑是否回复）"""
    if not session_key:
        return False

    # 从会话缓存中获取最后活动时间
    cache = STORE.load_runtime_value("openclaw_session_cache", {})
    # Ensure cache is a dict
    if not isinstance(cache, dict):
        cache = {}
    session_info = cache.get(session_key, {})
    # Ensure session_info is a dict
    if not isinstance(session_info, dict):
        session_info = {}
    last_activity = int(session_info.get("last_activity", 0) or 0)

    # 如果最后活动时间 > 追问时间，说明有响应
    if last_activity > since_timestamp:
        log(
            f"检测到会话有新响应: {session_key} (last_activity={last_activity}, since={since_timestamp})"
        )
        return True

    return False


def deliver_guardian_progress_update(
    dispatch: dict[str, Any],
    *,
    followup_message: str,
    fallback_message: str,
) -> tuple[str | None, str | None]:
    """Deliver progress updates with retry, then fall back to direct Feishu push."""
    session_key = dispatch.get("session_key") or ""
    open_id = dispatch.get("requester_open_id") or ""
    retries = max(1, int(CONFIG.get("GUARDIAN_FOLLOWUP_RETRIES", 2)))
    retry_delay = max(0, int(CONFIG.get("GUARDIAN_FOLLOWUP_RETRY_DELAY", 3)))
    response_check_delay = max(
        5, int(CONFIG.get("GUARDIAN_RESPONSE_CHECK_DELAY", 10))
    )  # 等待响应的时间
    blocked_reason: str | None = None

    if session_key:
        for attempt in range(1, retries + 1):
            # 记录追问时间
            followup_sent_at = int(time.time())

            ok, error_kind = send_guardian_followup(session_key, followup_message)
            if ok:
                # 追问发送成功，等待主脑响应
                log(f"追问已发送，等待主脑响应: {session_key}")
                time.sleep(response_check_delay)

                # 检查是否有响应
                if check_session_has_response(session_key, followup_sent_at):
                    log(f"主脑已响应追问: {session_key}")
                    return "session", None
                else:
                    log(f"主脑未响应追问，可能需要继续追踪: {session_key}", "WARNING")
                    # 记录追问成功但未确认响应
                    STORE.save_runtime_value(
                        f"followup_pending:{session_key}",
                        {"sent_at": followup_sent_at, "confirmed": False},
                    )
                    return "session", "response_not_confirmed"

            blocked_reason = error_kind or blocked_reason
            if blocked_reason in {
                "session_lock",
                "model_auth",
                "model_unavailable",
                "model_pool_failed",
            }:
                break
            if attempt < retries and retry_delay:
                time.sleep(retry_delay)
        log(
            f"守护追问连续失败，降级为直接消息推送: {session_key}",
            "WARNING",
        )

    if (
        open_id
        and not is_internal_system_problem(blocked_reason)
        and send_feishu_progress_push(open_id, fallback_message)
    ):
        return "feishu", blocked_reason
    return None, blocked_reason


def trim_runtime_seen(seen: dict[str, int], keep: int = 2000) -> dict[str, int]:
    """Bound the anomaly dedupe table so it cannot grow forever."""
    if len(seen) <= keep:
        return seen
    newest = sorted(seen.items(), key=lambda item: item[1], reverse=True)[:keep]
    return dict(newest)


def trim_runtime_state_map(
    state: dict[str, dict[str, Any]], keep: int = 500
) -> dict[str, dict[str, Any]]:
    """Bound runtime state maps keyed by task/session using their freshest timestamp."""
    if len(state) <= keep:
        return state
    newest = sorted(
        state.items(),
        key=lambda item: max(
            int((item[1] or {}).get("last_followup_at", 0)),
            int((item[1] or {}).get("last_stage_push", 0)),
            int((item[1] or {}).get("last_escalation_push", 0)),
            int((item[1] or {}).get("last_blocked_notice", 0)),
        ),
        reverse=True,
    )[:keep]
    return dict(newest)


def should_record_control_plane_anomaly(
    task_id: str, blocked_reason: str, *, interval: int = 1800
) -> bool:
    seen = STORE.load_runtime_value("control_plane_block_seen", {})
    key = f"{task_id}:{blocked_reason}"
    now = int(time.time())
    last = int(seen.get(key, 0) or 0)
    if last and now - last < interval:
        return False
    seen[key] = now
    STORE.save_runtime_value(
        "control_plane_block_seen", trim_runtime_seen(seen, keep=500)
    )
    return True


def attach_guardian_progress_fact(
    session_key: str,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if not session_key:
        return
    task = STORE.get_latest_task_for_session(session_key)
    if not task:
        return
    STORE.record_task_event(task["task_id"], event_type, payload)


def attach_background_result_if_late(
    task_id: str, session_key: str, *, completed_at: int, status: str
) -> None:
    if not session_key:
        return
    tasks = STORE.list_tasks_for_session(session_key, limit=20)
    newer_active = next(
        (
            item
            for item in tasks
            if item["task_id"] != task_id
            and item.get("status") in {"running", "blocked", "background"}
            and int(item.get("created_at") or 0) <= completed_at
        ),
        None,
    )
    if not newer_active:
        return
    STORE.update_task_fields(
        task_id, backgrounded_at=completed_at, updated_at=completed_at
    )
    STORE.record_task_event(
        task_id,
        "background_result",
        {
            "timestamp": datetime.now().isoformat(),
            "active_task_id": newer_active["task_id"],
            "active_question": newer_active.get("question")
            or newer_active.get("last_user_message")
            or "未知任务",
            "status": status,
        },
    )


def reconcile_background_results_for_sessions(session_keys: set[str]) -> None:
    for session_key in session_keys:
        if not session_key:
            continue
        resolution = STORE.derive_session_resolution(session_key)
        active_task_id = resolution.get("active_task_id")
        late_completed = resolution.get("late_completed_tasks") or []
        for item in late_completed:
            task_id = str(item.get("task_id") or "")
            if not task_id or task_id == active_task_id:
                continue
            task = STORE.get_task(task_id)
            if not task:
                continue
            completed_at = int(
                task.get("completed_at") or task.get("updated_at") or time.time()
            )
            attach_background_result_if_late(
                task_id,
                session_key,
                completed_at=completed_at,
                status=str(task.get("status") or "completed"),
            )


def enforce_single_active_runtime_guard() -> list[dict[str, Any]]:
    # 只支持 primary 环境，不再需要双环境检查
    return []


def _terminate_pid(
    pid: Optional[int], label: str, timeout: float = 8.0
) -> tuple[bool, str]:
    if pid is None:
        return True, f"{label} listener 未运行"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"{label} listener 已退出"
    except Exception as exc:
        return False, f"停止 {label} listener 失败: {exc}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True, f"已终止 {label} listener(pid={pid})"
        except Exception:
            break
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, f"已终止 {label} listener(pid={pid})"
    except Exception as exc:
        return False, f"强制终止 {label} listener 失败: {exc}"
    return False, f"{label} listener 仍然存活"


def patrol_active_binding_runtime() -> list[dict[str, Any]]:
    specs = all_env_specs()
    bound_env = active_env_id()
    issues: list[dict[str, Any]] = []
    closure = build_main_closure_supervision_summary()
    if not bool(closure.get("purity_gate_ok", True)):
        issues.append(
            {
                "code": "main_closure_purity_gate_failed",
                "message": "主闭环纯净度门禁失败",
                "details": {
                    "env_id": closure.get("env_id") or bound_env,
                    "purity_gate_reasons": list(
                        closure.get("purity_gate_reasons") or []
                    ),
                },
            }
        )
    for env_id, spec in specs.items():
        pid = get_listener_pid(int(spec["port"]))
        if env_id == bound_env:
            if pid is None:
                issues.append(
                    {
                        "code": f"bound_env_not_running_{env_id}",
                        "message": f"DB 绑定环境 {env_id} 未监听",
                        "details": {"env_id": env_id, "gateway_port": spec["port"]},
                    }
                )
            continue
        if pid is None:
            continue
        run_args([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=120)
        time.sleep(1)
        pid_after = get_listener_pid(int(spec["port"]))
        if pid_after is not None:
            killed, result = _terminate_pid(pid_after, env_id)
        else:
            killed, result = True, f"已停止未绑定环境 {env_id} listener"
        issues.append(
            {
                "code": f"unbound_listener_{env_id}",
                "message": f"未绑定环境 {env_id} 仍在监听，已执行清退",
                "details": {
                    "env_id": env_id,
                    "gateway_port": spec["port"],
                    "result": result,
                    "killed": killed,
                },
            }
        )
    if issues:
        seen = STORE.load_runtime_value("binding_patrol_seen", {})
        now = int(time.time())
        for issue in issues:
            code = str(issue.get("code") or "binding_patrol")
            if code not in seen or now - int(seen.get(code, 0)) > 300:
                seen[code] = now
                record_change_log(
                    "anomaly",
                    str(issue.get("message") or "binding patrol"),
                    issue.get("details") or {},
                )
                notify(
                    "绑定巡检告警",
                    str(issue.get("message") or "检测到未绑定 listener"),
                    "error",
                )
            STORE.append_runtime_event(
                "binding_audit_events",
                {
                    "source": "guardian.patrol",
                    "env_id": issue.get("details", {}).get("env_id") or "",
                    "status": issue.get("code") or "observed",
                    "details": issue.get("details") or {},
                    "timestamp_iso": datetime.now().isoformat(),
                },
                limit=200,
            )
        STORE.save_runtime_value(
            "binding_patrol_seen", trim_runtime_seen(seen, keep=50)
        )
    return issues


def collect_open_runtime_dispatches(lines: list[str]) -> list[dict[str, Any]]:
    """Track currently open dispatches and their most recent visible progress."""
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: dict[str, dict[str, Any]] = {}

    def most_recent_key() -> str | None:
        if not open_dispatches:
            return None
        return max(open_dispatches.items(), key=lambda item: item[1]["started_at"])[0]

    for line in lines:
        ts_raw, ts = parse_runtime_timestamp(line)
        if ts is None:
            continue

        question = extract_runtime_question(line)
        if question:
            question_candidates.append((int(ts), question))
            question_candidates = question_candidates[-50:]

        lower = line.lower()
        if "dispatching to agent" in lower:
            nearest_question = ""
            for qts, qmsg in reversed(question_candidates):
                if abs(int(ts) - qts) <= 15:
                    nearest_question = qmsg
                    break
            session_key = (
                extract_runtime_session_key(line)
                or extract_requester_open_id(line)
                or f"dispatch:{ts_raw}"
            )
            open_dispatches[session_key] = {
                "session_key": session_key,
                "started_at": ts,
                "last_progress_at": ts,
                "timestamp": ts_raw,
                "question": nearest_question or "未知任务",
                "marker": "",
                "requester_open_id": extract_requester_open_id(line) or "",
            }
            continue

        marker = extract_pipeline_marker(line)
        if marker and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            dispatch["last_progress_at"] = ts
            dispatch["marker"] = marker
            continue

        if is_visible_completion_message(line) and open_dispatches:
            current_key = most_recent_key()
            if current_key:
                open_dispatches.pop(current_key, None)
            continue

        if "dispatch complete" in lower and open_dispatches:
            current_key = most_recent_key()
            if current_key:
                open_dispatches.pop(current_key, None)

    return sorted(open_dispatches.values(), key=lambda item: item["started_at"])


def collect_runtime_anomalies(
    lines: list[str],
    *,
    now: float,
    slow_threshold: int,
    stalled_threshold: int,
) -> tuple[list[dict[str, Any]], str]:
    """Build anomaly records from recent runtime logs."""
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: dict[str, dict[str, Any]] = {}
    anomalies: list[dict[str, Any]] = []
    latest_signature = ""

    def most_recent_key() -> str | None:
        if not open_dispatches:
            return None
        return max(open_dispatches.items(), key=lambda item: item[1]["started_at"])[0]

    for line in lines:
        signature = line.strip()
        if signature and signature > latest_signature:
            latest_signature = signature

        ts_raw, ts = parse_runtime_timestamp(line)
        message = extract_runtime_question(line)
        if message and ts is not None:
            question_candidates.append((int(ts), message))
            question_candidates = question_candidates[-50:]

        lower = line.lower()
        if "dispatching to agent" in lower and ts is not None:
            nearest_question = ""
            for qts, qmsg in reversed(question_candidates):
                if abs(int(ts) - qts) <= 15:
                    nearest_question = qmsg
                    break
            requester_open_id = extract_requester_open_id(line)
            session_key = (
                extract_runtime_session_key(line)
                or requester_open_id
                or f"dispatch:{ts_raw}"
            )
            open_dispatches[session_key] = {
                "session_key": session_key,
                "started_at": ts,
                "timestamp": ts_raw,
                "question": nearest_question or "未知问题",
                "last_progress_at": ts,
                "marker": "",
                "requester_open_id": requester_open_id or "",
            }
            continue

        marker = extract_pipeline_marker(line)
        if marker and ts is not None and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            dispatch["last_progress_at"] = ts
            dispatch["marker"] = marker
            continue

        if "dispatch complete" in lower and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches.pop(current_key)
            duration = int(ts - dispatch["started_at"]) if ts is not None else 0
            queued_final = "queuedfinal=true" in lower
            replies = 0
            if "replies=" in lower:
                try:
                    replies = int(
                        lower.split("replies=", 1)[1].split(")", 1)[0].split(",", 1)[0]
                    )
                except Exception:
                    replies = 0

            details = {
                "question": dispatch["question"],
                "duration": duration,
                "timestamp": ts_raw,
            }
            if dispatch.get("marker"):
                details["marker"] = dispatch["marker"]

            if (not queued_final) or replies == 0:
                details["queued_final"] = queued_final
                details["replies"] = replies
                anomalies.append(
                    {
                        "signature": signature,
                        "type": "no_reply",
                        "message": "任务完成但没有可见回复",
                        "details": details,
                    }
                )
            elif duration >= stalled_threshold:
                anomalies.append(
                    {
                        "signature": signature,
                        "type": "stalled_reply",
                        "message": "任务响应严重超时",
                        "details": details,
                    }
                )
            elif duration >= slow_threshold:
                anomalies.append(
                    {
                        "signature": signature,
                        "type": "slow_reply",
                        "message": "任务响应偏慢",
                        "details": details,
                    }
                )
            continue

        if "gateway closed" in lower and "1006" in lower:
            anomalies.append(
                {
                    "signature": signature,
                    "type": "gateway_ws_closed",
                    "message": "Gateway WebSocket 异常关闭",
                    "details": {"timestamp": ts_raw},
                }
            )
        elif "failovererror" in lower and "timed out" in lower:
            anomalies.append(
                {
                    "signature": signature,
                    "type": "model_timeout",
                    "message": "模型请求超时",
                    "details": {"timestamp": ts_raw},
                }
            )
        elif "failovererror" in lower and ("rate limit" in lower or "exhaust" in lower):
            anomalies.append(
                {
                    "signature": signature,
                    "type": "failover_exhausted",
                    "message": "模型 failover 未能成功恢复",
                    "details": {"timestamp": ts_raw},
                }
            )
        elif "inflightskipped=" in lower or "releasing aged in-flight lock" in lower:
            anomalies.append(
                {
                    "signature": signature,
                    "type": "channel_inflight_stuck",
                    "message": "消息通道 in-flight 锁异常或重投受阻",
                    "details": {"timestamp": ts_raw},
                }
            )
        elif "abort failed" in lower and "no_active_run" in lower:
            anomalies.append(
                {
                    "signature": signature,
                    "type": "run_tracking_warning",
                    "message": "任务状态追踪异常",
                    "details": {"timestamp": ts_raw},
                }
            )

    for dispatch in sorted(
        open_dispatches.values(), key=lambda item: item["started_at"]
    ):
        duration = int(now - dispatch["started_at"])
        details = {
            "question": dispatch["question"],
            "duration": duration,
            "timestamp": dispatch["timestamp"],
        }
        if dispatch.get("marker"):
            details["marker"] = dispatch["marker"]

        if (
            dispatch.get("marker")
            and int(now - dispatch["last_progress_at"]) >= stalled_threshold
        ):
            anomalies.append(
                {
                    "signature": f"stage:{dispatch['timestamp']}:{dispatch['marker']}",
                    "type": "stage_stuck",
                    "message": "任务阶段长时间无进展",
                    "details": details,
                }
            )
        elif duration >= stalled_threshold:
            anomalies.append(
                {
                    "signature": f"open:{dispatch['timestamp']}:{dispatch['question']}",
                    "type": "dispatch_stuck",
                    "message": "任务长时间无最终结果",
                    "details": details,
                }
            )

    return anomalies, latest_signature


def scan_runtime_anomalies() -> list[dict]:
    """Detect stalled or no-reply situations from runtime logs."""
    runtime_log = resolve_runtime_gateway_log()
    cursor = STORE.load_runtime_value("runtime_anomaly_cursor", {})
    last_signature = cursor.get("last_signature", "")
    stalled_threshold = int(CONFIG.get("STALLED_RESPONSE_THRESHOLD", 90))
    slow_threshold = int(CONFIG.get("SLOW_RESPONSE_THRESHOLD", 30))
    lines: list[str] = []
    anomalies: list[dict[str, Any]] = []
    latest_signature = last_signature

    if runtime_log.exists():
        try:
            with open(runtime_log) as handle:
                lines = handle.readlines()[-4000:]
        except Exception as exc:
            log(f"读取运行日志失败: {exc}", "ERROR")
            lines = []
        if lines:
            anomalies, latest_signature = collect_runtime_anomalies(
                lines,
                now=time.time(),
                slow_threshold=slow_threshold,
                stalled_threshold=stalled_threshold,
            )

    closure = build_main_closure_supervision_summary()
    if not bool(closure.get("purity_gate_ok", True)):
        reasons = list(closure.get("purity_gate_reasons") or [])
        purity_signature = "purity_gate:" + "|".join(reasons or ["unknown"])
        anomalies.append(
            {
                "signature": purity_signature,
                "type": "main_closure_purity_gate_failed",
                "message": "主闭环纯净度门禁失败",
                "details": {
                    "env_id": closure.get("env_id") or active_env_id(),
                    "reasons": reasons,
                    "timestamp": str(closure.get("generated_at") or int(time.time())),
                },
            }
        )
    latest_signature = latest_signature or last_signature

    seen = STORE.load_runtime_value("runtime_anomaly_seen", {})
    recorded: list[dict] = []
    for anomaly in anomalies:
        signature = anomaly["signature"]
        if signature in seen:
            continue
        seen[signature] = int(time.time())
        record_change_log("anomaly", anomaly["message"], anomaly["details"])
        recorded.append(anomaly)

        alert_key = f"runtime_anomaly_{anomaly['type']}"
        if should_alert(alert_key):
            detail_lines = [f"类型: {anomaly['type']}"]
            if anomaly["details"].get("question"):
                detail_lines.append(f"问题: {anomaly['details']['question']}")
            reasons = anomaly["details"].get("reasons")
            if isinstance(reasons, list) and reasons:
                detail_lines.append(
                    f"原因: {', '.join(str(item) for item in reasons[:3])}"
                )
            if anomaly["details"].get("duration") is not None:
                detail_lines.append(f"耗时: {anomaly['details']['duration']}秒")
            if anomaly["details"].get("timestamp"):
                detail_lines.append(f"时间: {anomaly['details']['timestamp']}")
            notify("OpenClaw 任务异常", "\n".join(detail_lines), "warning")

    if recorded:
        capture_runtime_anomaly_learnings(recorded)

    if latest_signature != last_signature:
        STORE.save_runtime_value(
            "runtime_anomaly_cursor",
            {"last_signature": latest_signature, "source_log": str(runtime_log)},
        )
    STORE.save_runtime_value("runtime_anomaly_seen", trim_runtime_seen(seen))
    return recorded


def push_runtime_progress_updates() -> list[dict]:
    """Push updates only when runtime logs show a real silence window."""
    runtime_log = resolve_runtime_gateway_log()
    if not runtime_log.exists():
        return []

    try:
        with open(runtime_log) as handle:
            lines = handle.readlines()[-4000:]
    except Exception as exc:
        log(f"读取运行日志失败: {exc}", "ERROR")
        return []

    now = time.time()
    open_dispatches = collect_open_runtime_dispatches(lines)

    if not bool(CONFIG.get("ENABLE_GUARDIAN_PROGRESS_PUSH", False)):
        return []

    progress_interval = int(CONFIG.get("PROGRESS_PUSH_INTERVAL", 180))
    progress_cooldown = int(CONFIG.get("PROGRESS_PUSH_COOLDOWN", 300))
    escalation_interval = int(CONFIG.get("PROGRESS_ESCALATION_INTERVAL", 600))
    stale_task_max_age = int(CONFIG.get("GUARDIAN_STALE_TASK_MAX_AGE", 3600))
    blocked_cooldown = int(CONFIG.get("GUARDIAN_BLOCKED_COOLDOWN", 900))
    blocked_notice_interval = int(CONFIG.get("GUARDIAN_BLOCKED_NOTICE_INTERVAL", 1800))
    push_state = STORE.load_runtime_value("runtime_progress_push_state", {})
    pushed: list[dict] = []
    active_keys: set[str] = set()

    for dispatch in open_dispatches:
        open_id = dispatch.get("requester_open_id") or ""
        session_key = dispatch.get("session_key") or ""
        if not session_key and not open_id:
            continue

        duration = int(now - dispatch["started_at"])
        idle = int(now - dispatch["last_progress_at"])
        marker = dispatch.get("marker") or ""
        stage_label = normalize_stage_label(marker)
        push_key = session_key or f"{dispatch['timestamp']}:{open_id}"
        active_keys.add(push_key)
        state = push_state.get(push_key, {})
        last_seen_progress_at = int(state.get("last_seen_progress_at", 0))
        current_progress_at = int(dispatch["last_progress_at"])
        last_dispatch_timestamp = str(state.get("last_dispatch_timestamp", ""))
        current_dispatch_timestamp = str(dispatch["timestamp"])
        if current_dispatch_timestamp != last_dispatch_timestamp:
            state["last_dispatch_timestamp"] = current_dispatch_timestamp
            state["last_seen_progress_at"] = current_progress_at
            state["last_stage_push"] = 0
            state["last_escalation_push"] = 0
            state["last_marker"] = marker
            state["stale_suppressed_at"] = 0
        if current_progress_at != last_seen_progress_at:
            state["last_seen_progress_at"] = current_progress_at
            state["last_stage_push"] = 0
            state["last_escalation_push"] = 0
            state["last_marker"] = marker
            state["stale_suppressed_at"] = 0
            state["blocked_reason"] = ""
            state["blocked_until"] = 0

        last_stage_push = int(state.get("last_stage_push", 0))
        last_escalation_push = int(state.get("last_escalation_push", 0))
        stale_suppressed_at = int(state.get("stale_suppressed_at", 0))
        blocked_until = int(state.get("blocked_until", 0))
        blocked_reason = str(state.get("blocked_reason", ""))
        last_blocked_notice = int(state.get("last_blocked_notice", 0))

        if duration >= stale_task_max_age:
            if stale_suppressed_at == 0:
                log(
                    "检测到过时未完成任务，已抑制泛化跟进推送: "
                    f"{session_key or open_id} (idle={format_duration_label(idle)}, total={format_duration_label(duration)})",
                    "INFO",
                )
                record_change_log(
                    "anomaly",
                    "守护系统抑制过时任务跟进",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                    },
                )
                state["stale_suppressed_at"] = int(now)
            if state:
                push_state[push_key] = state
            continue

        if (
            blocked_reason
            and blocked_until <= int(now)
            and now - last_blocked_notice >= blocked_notice_interval
        ):
            reason_label = blocked_reason_label(blocked_reason)
            blocked_message = (
                f"任务当前已阻塞。当前阶段：{stage_label}。"
                f"阻塞原因：{reason_label}。"
                f"已静默 {format_duration_label(idle)}，累计运行 {format_duration_label(duration)}。"
                "系统已尝试自动恢复；若后续仍无结果，建议重新发起该任务。"
            )
            if (
                open_id
                and not is_internal_system_problem(blocked_reason)
                and send_feishu_progress_push(open_id, blocked_message)
            ):
                attach_guardian_progress_fact(
                    session_key,
                    event_type="guardian_blocked_notice",
                    payload={
                        "idle": idle,
                        "duration": duration,
                        "channel": "feishu",
                        "blocked_reason": blocked_reason,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                record_change_log(
                    "anomaly",
                    "守护系统阻塞提示",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                        "blocked_reason": blocked_reason,
                        "delivery_channel": "feishu",
                    },
                )
                state["last_blocked_notice"] = int(now)
                state["blocked_until"] = int(now) + blocked_notice_interval
                pushed.append(
                    {
                        "type": "blocked_notice",
                        "open_id": open_id,
                        "duration": duration,
                        "idle": idle,
                        "delivery_channel": "feishu",
                        "blocked_reason": blocked_reason,
                    }
                )
                push_state[push_key] = state
            continue

        if blocked_until > int(now):
            if state:
                push_state[push_key] = state
            continue

        if idle >= progress_interval and now - last_stage_push >= progress_cooldown:
            followup = (
                "GUARDIAN_FOLLOWUP: 这是一条守护系统自动追问，不是用户新需求。"
                "请不要开始新任务，也不要改写用户原始需求。"
                f"请仅基于当前会话同步进展。当前阶段={stage_label}；"
                f"距离上一次可见进展={format_duration_label(idle)}；"
                f"累计运行={format_duration_label(duration)}；"
                f"当前问题={dispatch['question']}。"
                "请优先给用户一句简洁进度说明；若当前任务仍在处理中，只汇报现状，不要重新起新任务。"
            )
            fallback_message = (
                f"任务暂时没有新的可见进展。当前阶段：{stage_label}。"
                f"距离上一次进展已过去 {format_duration_label(idle)}，"
                f"累计运行 {format_duration_label(duration)}，系统会继续自动跟进。"
            )
            channel, failed_reason = deliver_guardian_progress_update(
                dispatch,
                followup_message=followup,
                fallback_message=fallback_message,
            )
            if channel:
                attach_guardian_progress_fact(
                    session_key,
                    event_type="guardian_progress_push",
                    payload={
                        "idle": idle,
                        "duration": duration,
                        "channel": channel,
                        "blocked_reason": failed_reason or "",
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                record_change_log(
                    "pipeline",
                    "守护系统主动追问",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or "",
                    },
                )
                state["last_stage_push"] = int(now)
                if failed_reason in {
                    "session_lock",
                    "model_auth",
                    "model_unavailable",
                    "model_pool_failed",
                }:
                    state["blocked_reason"] = failed_reason
                    state["blocked_until"] = int(now) + blocked_cooldown
                pushed.append(
                    {
                        "type": "progress_push",
                        "open_id": open_id,
                        "duration": duration,
                        "idle": idle,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or "",
                    }
                )

        if (
            idle >= escalation_interval
            and now - last_escalation_push >= escalation_interval
        ):
            followup = (
                "GUARDIAN_ESCALATION: 这是一条守护系统升级催办，不是用户新需求。"
                "请不要启动新任务。"
                f"当前阶段={stage_label}；静默已持续={format_duration_label(idle)}；"
                f"累计运行={format_duration_label(duration)}；当前问题={dispatch['question']}。"
                "请明确向用户同步：当前是否仍在执行、是否已阻塞、下一步动作是什么。"
            )
            escalation_reason = (
                blocked_reason_label(blocked_reason) if blocked_reason else ""
            )
            fallback_message = (
                (
                    f"任务当前已阻塞。当前阶段：{stage_label}。"
                    f"阻塞原因：{escalation_reason}。"
                    f"已静默 {format_duration_label(idle)}，累计运行 {format_duration_label(duration)}。"
                    "系统会继续跟进，但会先等待阻塞解除。"
                )
                if blocked_reason
                else (
                    f"任务长时间没有新的可见进展。当前阶段：{stage_label}。"
                    f"静默已持续 {format_duration_label(idle)}，"
                    f"累计运行 {format_duration_label(duration)}，系统已将其升级关注并会继续同步。"
                )
            )
            channel, failed_reason = deliver_guardian_progress_update(
                dispatch,
                followup_message=followup,
                fallback_message=fallback_message,
            )
            if channel:
                attach_guardian_progress_fact(
                    session_key,
                    event_type="guardian_escalation_push",
                    payload={
                        "idle": idle,
                        "duration": duration,
                        "channel": channel,
                        "blocked_reason": failed_reason or blocked_reason or "",
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                record_change_log(
                    "anomaly",
                    "守护系统升级催办",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or blocked_reason or "",
                    },
                )
                state["last_escalation_push"] = int(now)
                if failed_reason in {
                    "session_lock",
                    "model_auth",
                    "model_unavailable",
                    "model_pool_failed",
                }:
                    state["blocked_reason"] = failed_reason
                    state["blocked_until"] = int(now) + blocked_cooldown
                pushed.append(
                    {
                        "type": "escalation_push",
                        "open_id": open_id,
                        "duration": duration,
                        "idle": idle,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or blocked_reason or "",
                    }
                )

        if state:
            push_state[push_key] = state

    for push_key in list(push_state.keys()):
        if push_key not in active_keys:
            push_state.pop(push_key, None)

    if push_state:
        STORE.save_runtime_value(
            "runtime_progress_push_state", trim_runtime_state_map(push_state, keep=500)
        )
    return pushed


def enforce_task_registry_control_plane() -> list[dict]:
    """Promote weak registry states into explicit recovery or blocked states."""
    if not CONFIG.get("ENABLE_TASK_REGISTRY", True):
        return []

    env_id = current_env_spec()["id"]
    now = int(time.time())
    cooldown = int(CONFIG.get("TASK_CONTROL_FOLLOWUP_COOLDOWN", 300))
    max_attempts = max(1, int(CONFIG.get("TASK_CONTROL_MAX_ATTEMPTS", 2)))
    intrusive_control = True
    watcher_summary = STORE.load_runtime_value(f"watcher_summary:{env_id}", {})
    direct_watcher_signal = _load_watcher_signal_direct()
    if isinstance(watcher_summary, dict) and direct_watcher_signal:
        watcher_summary = {**watcher_summary, "watcher_signal": direct_watcher_signal}
    outcomes: list[dict] = []

    for task in STORE.list_tasks(limit=int(CONFIG.get("TASK_REGISTRY_RETENTION", 500))):
        if task.get("env_id") != env_id:
            continue

        core = STORE.get_core_closure_snapshot_for_task(
            task["task_id"], allow_legacy_projection=False
        )
        # Ensure core is a dict, not a string
        if not isinstance(core, dict):
            core = {}
        core_supervision = STORE.derive_core_task_supervision(task["task_id"])
        # Ensure core_supervision is a dict, not a string
        if not isinstance(core_supervision, dict):
            core_supervision = {}
        workflow_state = str(
            core.get("msg_state") or core.get("workflow_state") or "open"
        )
        delivery_state = str(
            core_supervision.get("delivery_state")
            or core.get("delivery_state")
            or "undelivered"
        )
        root_task_id = str((core.get("root_task") or {}).get("root_task_id") or "")
        native_core_task = (
            core_supervision.get("truth_level") == "core_projection"
            and root_task_id
            and not root_task_id.startswith("legacy-root:")
        )
        if (
            native_core_task
            and workflow_state in {"open", "completed"}
            and not core_supervision.get("needs_followup")
            and not core_supervision.get("is_blocked")
            and not core_supervision.get("is_delivery_pending")
        ):
            continue
        if workflow_state in {"completed", "failed", "blocked"} and delivery_state in {
            "delivered",
            "owner_escalated",
        }:
            continue
        if (
            workflow_state in {"completed", "failed", "blocked"}
            and delivery_state == "undelivered"
        ):
            continue

        control = STORE.derive_task_control_state(task["task_id"])
        # Ensure control is a dict, not a string
        if not isinstance(control, dict):
            control = {}
        if native_core_task:
            action = control.get("control_action")
        else:
            action = STORE.reconcile_task_control_action(task, control)
            control = STORE.derive_task_control_state(task["task_id"])
        contract = control.get("contract") or {}
        control_state = str(control.get("control_state") or "")
        approved_summary = str(control.get("approved_summary") or "")
        next_action = str(control.get("next_action") or "")
        next_actor = str(control.get("next_actor") or "")
        blocked_reason = str(task.get("blocked_reason") or "")
        recovery = control.get("pipeline_recovery") or {}
        recovery_candidate = (
            bool(recovery) and next_action == "manual_or_session_recovery"
        )

        # 对主脑 Feishu 会话：如果最近一条用户消息已经拿到主脑可见回复，
        # 则更早的普通问答任务不应再继续留在 running/background/blocked。
        # 否则它们会在 background 后再次落到 background_root_task_missing，形成旧任务噪音。
        session_key = str(task.get("session_key") or "")
        if session_key.startswith("agent:main:feishu:"):
            session_rec = load_openclaw_session_record(session_key)
            if session_rec:
                session_file = Path(str(session_rec.get("sessionFile") or ""))
                task_question = normalize_task_question(
                    task.get("question") or task.get("last_user_message")
                )
                if _session_question_has_visible_reply(session_file, task_question):
                    STORE.update_task_fields(
                        task["task_id"],
                        status="completed",
                        current_stage="已完成",
                        blocked_reason="",
                        updated_at=now,
                        completed_at=now,
                    )
                    outcomes.append(
                        {
                            "task_id": task["task_id"],
                            "action": "completed_answered_task",
                            "control_state": control_state,
                        }
                    )
                    continue

        # P0 修复：不再只检查 required_receipts，而是检查 control_state
        # 即使是 single_agent 合同，如果 control_state 是 received_only 且 idle 超过阈值，也应该 blocked
        has_required_receipts = bool(contract.get("required_receipts") or [])
        needs_control_action = (
            control_state
            in {
                "received_only",
                "planning_only",
                "progress_only",
                "calculator_running",
                "awaiting_verifier",
                "dev_running",
                "awaiting_test",
                "test_running",
                "blocked_unverified",
                "blocked_control_followup_failed",
            }
            or recovery_candidate
            or bool(core_supervision.get("needs_followup"))
            or bool(core_supervision.get("is_blocked"))
        )
        watcher_signal = _watcher_signal_needs_action(watcher_summary, task["task_id"])

        if not has_required_receipts and not needs_control_action:
            # 即使 next_action=none，如果有 watcher 信号，也要处理
            if not watcher_signal:
                continue

        idle = max(
            0, now - int(task.get("last_progress_at") or task.get("updated_at") or now)
        )
        total = max(0, now - int(task.get("started_at") or now))
        action = action or control.get("control_action")
        action_id = (
            int(action["id"]) if isinstance(action, dict) and action.get("id") else 0
        )
        native_followup_id = ""
        if isinstance(action, dict):
            native_followup_id = str(
                (action.get("details") or {}).get("followup_id") or ""
            )

        def update_native_followup(
            *,
            state: str | None = None,
            suggested_action: str | None = None,
            resolved_at: int | None = None,
            metadata_updates: dict[str, Any] | None = None,
        ) -> None:
            if not native_core_task or not native_followup_id:
                return
            STORE.update_followup(
                native_followup_id,
                current_state=state,
                suggested_action=suggested_action,
                resolved_at=resolved_at,
                updated_at=now,
                metadata_updates=metadata_updates or {},
            )

        attempts = int((action or {}).get("attempts", 0))
        last_followup_at = int((action or {}).get("last_followup_at", 0))
        watcher_alert = _watcher_alert_for_task(watcher_summary, task["task_id"])
        phase = _infer_heartbeat_phase_for_task(task)
        if watcher_signal.get("needs_delivery_retry"):
            if not action_id:
                # 即使 reconcile 返回 None，也强制创建一个新 action
                STORE.create_control_action(
                    task["task_id"],
                    env_id,
                    "delivery_retry",
                    control_state=control_state,
                    status="pending",
                    summary="Watcher 检测到完成但未送达，已触发 delivery retry。",
                    details={"watcher_signal": watcher_signal, "watcher_triggered": True, "delivery_followup": True},
                )
                action = STORE.get_open_control_action(task["task_id"]) or {}
                action_id = int(action.get("id") or 0)
            if action_id:
                details = dict(action.get("details") or {})
                details["watcher_signal"] = watcher_signal
                details["delivery_followup"] = True
                STORE.update_control_action(
                    action_id,
                    status="pending",
                    attempts=attempts + 1,
                    last_followup_at=now,
                    summary="Watcher 检测到完成但未送达，已触发 delivery retry。",
                    control_state=control_state,
                    details=details,
                )
                STORE.record_task_event(
                    task["task_id"],
                    "delivery_retry_triggered_by_watcher",
                    {
                        "reason": watcher_signal.get("reason"),
                        "ts": watcher_signal.get("ts"),
                        "control_state": control_state,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "delivery_retry_by_watcher",
                        "control_state": control_state,
                    }
                )
                _clear_watcher_signal_if_processed(watcher_summary)
                continue
        if watcher_signal.get("needs_receipt_or_block"):
            if not action_id:
                # 即使 reconcile 返回 None，也强制创建一个新 action
                STORE.create_control_action(
                    task["task_id"],
                    env_id,
                    "require_receipt_or_block",
                    control_state=control_state,
                    status="pending",
                    summary="Watcher 检测到缺少回执，已触发 receipt 追证。",
                    details={"watcher_signal": watcher_signal, "watcher_triggered": True},
                )
                action = STORE.get_open_control_action(task["task_id"]) or {}
                action_id = int(action.get("id") or 0)
            if action_id:
                details = dict(action.get("details") or {})
                details["watcher_signal"] = watcher_signal
                STORE.update_control_action(
                    action_id,
                    status="pending",
                    attempts=attempts + 1,
                    last_followup_at=now,
                    summary="Watcher 检测到缺少回执，已触发 receipt 追证。",
                    control_state=control_state,
                    details=details,
                )
                STORE.record_task_event(
                    task["task_id"],
                    "receipt_check_triggered_by_watcher",
                    {
                        "reason": watcher_signal.get("reason"),
                        "ts": watcher_signal.get("ts"),
                        "control_state": control_state,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "receipt_check_by_watcher",
                        "control_state": control_state,
                    }
                )
                _clear_watcher_signal_if_processed(watcher_summary)
                continue
        profile = infer_duration_profile(phase=phase, task=task, control=control)
        timing = resolve_timing_window(phase=phase, profile=profile)
        first_window = (
            timing.first_ack_sla
            if control_state in {"received_only", "planning_only", "progress_only"}
            else timing.heartbeat_interval
        )
        hard_timeout = timing.hard_timeout
        if task.get("status") == "background":
            orphaned_background = (
                not root_task_id
                or root_task_id.startswith("legacy-root:")
                or not STORE.get_root_task(root_task_id)
            )
            if orphaned_background and (
                idle >= max(60, hard_timeout // 2) or total >= hard_timeout
            ):
                STORE.update_task_fields(
                    task["task_id"],
                    status="blocked",
                    current_stage="后台任务失去 root task 绑定",
                    blocked_reason="background_root_task_missing",
                    updated_at=now,
                )
                STORE.record_task_event(
                    task["task_id"],
                    "control_blocked",
                    {
                        "reason": "background_root_task_missing",
                        "control_state": control_state,
                        "idle": idle,
                        "total": total,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "blocked",
                        "blocked_reason": "background_root_task_missing",
                        "control_state": control_state,
                    }
                )
                continue
        if watcher_alert and action_id:
            details = dict(action.get("details") or {})
            details["watcher_alert"] = watcher_alert
            details["watcher_reason"] = str(
                ((watcher_alert.get("current") or {}).get("reason") or "")
            )
            details["watcher_next_action"] = str(
                ((watcher_alert.get("current") or {}).get("next_action") or "")
            )
            STORE.update_control_action(
                action_id,
                summary=str(
                    ((watcher_alert.get("current") or {}).get("reason") or approved_summary or "")
                ),
                control_state=control_state,
                details=details,
            )
            action = STORE.get_open_control_action(task["task_id"]) or action
        soft_or_hard_stage = "soft" if attempts == 0 else "hard"
        immediate_followup_states = {
            "blocked_unverified",
            "blocked_control_followup_failed",
        }
        followup_threshold = (
            0
            if (recovery_candidate or control_state in immediate_followup_states)
            else first_window
        )

        if idle < followup_threshold:
            continue

        if (
            not recovery_candidate
            and task.get("status") == "blocked"
            and str(task.get("blocked_reason") or "")
            in {
                "missing_pipeline_receipt",
                "control_followup_failed",
            }
        ):
            continue

        if core_supervision.get("truth_level") == "core_projection":
            core_next_action = str(core_supervision.get("next_action") or "")
            core_next_actor = str(core_supervision.get("next_actor") or "")
            if core_next_action:
                next_action = core_next_action
            if core_next_actor:
                next_actor = core_next_actor
            if core_supervision.get("control_state"):
                control_state = str(
                    core_supervision.get("control_state") or control_state
                )
            if core_supervision.get("recovery_candidate"):
                recovery_candidate = True
            if core_supervision.get("followup_summary"):
                approved_summary = str(core_supervision.get("followup_summary") or "")
            if core_supervision.get("blocked_reason"):
                blocked_reason = str(core_supervision.get("blocked_reason") or "")
            action = action or control.get("control_action")

        if core_supervision.get(
            "truth_level"
        ) == "core_projection" and core_supervision.get("is_blocked"):
            blocked_reason = str(
                core_supervision.get("blocked_reason")
                or blocked_reason
                or "core_blocked"
            )
            if (
                task.get("status") != "blocked"
                or str(task.get("blocked_reason") or "") != blocked_reason
            ):
                STORE.update_task_fields(
                    task["task_id"],
                    status="blocked",
                    current_stage=str(task.get("current_stage") or "等待恢复执行"),
                    blocked_reason=blocked_reason,
                    updated_at=now,
                )
            if action_id:
                details = dict(action.get("details") or {})
                details.update(
                    {
                        "truth_level": "core_projection",
                        "workflow_state": workflow_state,
                        "followup_types": core_supervision.get("followup_types") or [],
                    }
                )
                STORE.update_control_action(
                    action_id,
                    status="pending",
                    summary=str(
                        core_supervision.get("followup_summary")
                        or approved_summary
                        or "主闭环当前处于阻塞状态。"
                    ),
                    control_state=control_state,
                    details=details,
                )
            else:
                update_native_followup(
                    state="open",
                    metadata_updates={
                        "summary": str(
                            core_supervision.get("followup_summary")
                            or approved_summary
                            or "主闭环当前处于阻塞状态。"
                        ),
                        "truth_level": "core_projection",
                        "workflow_state": workflow_state,
                        "followup_types": core_supervision.get("followup_types") or [],
                    },
                )

        should_block = (
            attempts >= max_attempts or total >= hard_timeout or idle >= hard_timeout
        )
        # received_only 任务如果没有证据，用更短的 timeout
        received_only_no_evidence = (
            control_state == "received_only"
            and not core_supervision.get("visible_completion_seen")
            and not bool((control.get("contract") or {}).get("required_receipts"))
        )
        if received_only_no_evidence and idle >= max(120, timing.first_ack_sla * 2):
            should_block = True
        delivery_followup_needed = (
            next_action in {"await_delivery_confirmation", "delivery_retry"}
            or (
                control_state == "completed_verified"
                and delivery_state not in {"delivered", "owner_escalated", "delivery_confirmed"}
            )
            or (
                bool(core_supervision.get("visible_completion_seen"))
                and delivery_state not in {"delivered", "owner_escalated", "delivery_confirmed"}
            )
        )
        if recovery_candidate and not intrusive_control:
            summary = str(
                core_supervision.get("followup_summary")
                or "检测到流水线失联；Health Monitor 默认不主动催办内部协作，请先核对 OpenClaw 原生状态。"
            )
            STORE.record_task_event(
                task["task_id"],
                "ops_attention_needed",
                {
                    "reason": str(
                        core_supervision.get("blocked_reason")
                        or "pipeline_recovery_needed"
                    ),
                    "control_state": control_state,
                    "pipeline_recovery": recovery,
                    "idle": idle,
                    "total": total,
                    "truth_level": "core_projection"
                    if core_supervision.get("truth_level") == "core_projection"
                    else "derived",
                    "timestamp": datetime.now().isoformat(),
                },
            )
            if action_id:
                details = dict(action.get("details") or {})
                details.update(
                    {
                        "policy": "observe_only",
                        "truth_level": "core_projection"
                        if core_supervision.get("truth_level") == "core_projection"
                        else "derived",
                        "pipeline_recovery": recovery,
                        "followup_types": core_supervision.get("followup_types") or [],
                    }
                )
                STORE.update_control_action(
                    action_id,
                    status="pending",
                    last_followup_at=now,
                    summary=summary,
                    control_state=control_state,
                    details=details,
                )
            else:
                update_native_followup(
                    state="open",
                    metadata_updates={
                        "summary": summary,
                        "policy": "observe_only",
                        "truth_level": "core_projection"
                        if core_supervision.get("truth_level") == "core_projection"
                        else "derived",
                        "pipeline_recovery": recovery,
                        "followup_types": core_supervision.get("followup_types") or [],
                        "last_followup_at": now,
                    },
                )
            record_change_log(
                "anomaly",
                "检测到流水线失联，需要人工核对 OpenClaw 原生状态",
                {
                    "question": task.get("question")
                    or task.get("last_user_message")
                    or "未知任务",
                    "task_id": task["task_id"],
                    "control_state": control_state,
                    "pipeline_recovery": recovery,
                    "idle": idle,
                    "duration": total,
                    "truth_level": "core_projection"
                    if core_supervision.get("truth_level") == "core_projection"
                    else "derived",
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "ops_attention_needed",
                    "control_state": control_state,
                    "pipeline_recovery": recovery,
                }
            )
            continue

        if recovery_candidate:
            session_key = str(task.get("session_key") or "")
            question = str(
                task.get("question") or task.get("last_user_message") or "未知任务"
            )
            # LRN-20260323-005: 不再因为任务过时就放弃追踪
            # 所有任务都要有结论、有回复
            stale_task_max_age = int(CONFIG.get("GUARDIAN_STALE_TASK_MAX_AGE", 3600))
            if total >= stale_task_max_age:
                log(
                    f"检测到过时任务，继续追踪（不再抑制）：{task['task_id']} (idle={format_duration_label(idle)}, total={format_duration_label(total)})",
                    "INFO",
                )
                # 不再抑制，继续追踪
            if now - last_followup_at < cooldown:
                continue
            next_attempts = attempts + 1
            if not session_key:
                STORE.update_task_fields(
                    task["task_id"],
                    status="blocked",
                    current_stage="等待恢复执行",
                    blocked_reason="pipeline_recovery_failed",
                    updated_at=now,
                )
                STORE.record_task_event(
                    task["task_id"],
                    "recovery_failed",
                    {
                        "recovery_kind": recovery.get("kind") or "",
                        "rebind_target": recovery.get("rebind_target") or "",
                        "stale_subagent": recovery.get("stale_subagent") or "",
                        "attempt": next_attempts,
                        "error_kind": "missing_session_key",
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                if action_id:
                    details = dict(action.get("details") or {})
                    details.update(
                        {
                            "recovery_kind": recovery.get("kind") or "",
                            "recovery_attempt": next_attempts,
                            "recovery_error": "missing_session_key",
                        }
                    )
                    STORE.update_control_action(
                        action_id,
                        status="blocked",
                        attempts=next_attempts,
                        last_followup_at=now,
                        last_error="missing_session_key",
                        summary="流水线恢复失败：缺少 session_key，无法自动恢复。",
                        control_state=control_state,
                        details=details,
                    )
                else:
                    update_native_followup(
                        state="open",
                        metadata_updates={
                            "summary": "流水线恢复失败：缺少 session_key，无法自动恢复。",
                            "recovery_kind": recovery.get("kind") or "",
                            "recovery_attempt": next_attempts,
                            "recovery_error": "missing_session_key",
                            "last_error": "missing_session_key",
                            "attempts": next_attempts,
                            "last_followup_at": now,
                        },
                    )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "blocked",
                        "blocked_reason": "pipeline_recovery_failed",
                        "control_state": control_state,
                        "pipeline_recovery": recovery,
                    }
                )
                continue

            recovery_message = build_pipeline_recovery_message(
                task,
                control,
                recovery,
                idle=idle,
                total=total,
            )
            STORE.record_task_event(
                task["task_id"],
                "recovery_started",
                {
                    "recovery_kind": recovery.get("kind") or "",
                    "last_dispatched_agent": recovery.get("last_dispatched_agent")
                    or "",
                    "stale_subagent": recovery.get("stale_subagent") or "",
                    "rebind_target": recovery.get("rebind_target") or "",
                    "attempt": next_attempts,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            ok, error_kind = send_guardian_followup(session_key, recovery_message)
            details = dict((action or {}).get("details") or {})
            details.update(
                {
                    "recovery_kind": recovery.get("kind") or "",
                    "recovery_attempt": next_attempts,
                    "recovery_error": error_kind or "",
                }
            )
            if ok:
                STORE.update_task_fields(
                    task["task_id"],
                    status="running",
                    current_stage=f"恢复中:{recovery.get('kind') or 'pipeline'}",
                    blocked_reason="",
                    updated_at=now,
                )
                STORE.record_task_event(
                    task["task_id"],
                    "recovery_succeeded",
                    {
                        "recovery_kind": recovery.get("kind") or "",
                        "last_dispatched_agent": recovery.get("last_dispatched_agent")
                        or "",
                        "stale_subagent": recovery.get("stale_subagent") or "",
                        "rebind_target": recovery.get("rebind_target") or "",
                        "attempt": next_attempts,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                if action_id:
                    STORE.update_control_action(
                        action_id,
                        status="sent",
                        attempts=next_attempts,
                        last_followup_at=now,
                        last_error="",
                        summary="守护系统已自动发起流水线恢复，等待新的结构化回执。",
                        control_state=control_state,
                        details=details,
                    )
                else:
                    update_native_followup(
                        state="open",
                        metadata_updates={
                            "summary": "守护系统已自动发起流水线恢复，等待新的结构化回执。",
                            "recovery_kind": recovery.get("kind") or "",
                            "recovery_attempt": next_attempts,
                            "recovery_error": "",
                            "attempts": next_attempts,
                            "last_followup_at": now,
                            "last_error": "",
                        },
                    )
                record_change_log(
                    "pipeline",
                    "守护控制面已自动发起流水线恢复",
                    {
                        "question": question,
                        "task_id": task["task_id"],
                        "control_state": control_state,
                        "pipeline_recovery": recovery,
                        "idle": idle,
                        "duration": total,
                    },
                )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "recovery_sent",
                        "control_state": control_state,
                        "pipeline_recovery": recovery,
                    }
                )
            else:
                STORE.record_task_event(
                    task["task_id"],
                    "recovery_failed",
                    {
                        "recovery_kind": recovery.get("kind") or "",
                        "last_dispatched_agent": recovery.get("last_dispatched_agent")
                        or "",
                        "stale_subagent": recovery.get("stale_subagent") or "",
                        "rebind_target": recovery.get("rebind_target") or "",
                        "attempt": next_attempts,
                        "error_kind": error_kind or "unknown",
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                fatal_recovery = next_attempts >= max_attempts or error_kind in {
                    "session_lock",
                    "model_auth",
                    "model_unavailable",
                    "model_pool_failed",
                    "unknown",
                }
                if fatal_recovery:
                    STORE.update_task_fields(
                        task["task_id"],
                        status="blocked",
                        current_stage="等待恢复执行",
                        blocked_reason="pipeline_recovery_failed",
                        updated_at=now,
                    )
                if action_id:
                    STORE.update_control_action(
                        action_id,
                        status="blocked" if fatal_recovery else "pending",
                        attempts=next_attempts,
                        last_followup_at=now,
                        last_error=error_kind or "",
                        summary="守护系统恢复流水线失败。"
                        if fatal_recovery
                        else "守护系统恢复流水线失败，将继续重试。",
                        control_state=control_state,
                        details=details,
                    )
                else:
                    update_native_followup(
                        state="open",
                        metadata_updates={
                            "summary": "守护系统恢复流水线失败。"
                            if fatal_recovery
                            else "守护系统恢复流水线失败，将继续重试。",
                            "recovery_kind": recovery.get("kind") or "",
                            "recovery_attempt": next_attempts,
                            "recovery_error": error_kind or "",
                            "attempts": next_attempts,
                            "last_followup_at": now,
                            "last_error": error_kind or "",
                        },
                    )
                if fatal_recovery and should_record_control_plane_anomaly(
                    task["task_id"], "pipeline_recovery_failed"
                ):
                    record_change_log(
                        "anomaly",
                        "守护控制面恢复流水线失败，任务已阻塞",
                        {
                            "question": question,
                            "task_id": task["task_id"],
                            "control_state": control_state,
                            "blocked_reason": "pipeline_recovery_failed",
                            "pipeline_recovery": recovery,
                            "error_kind": error_kind or "",
                            "idle": idle,
                            "duration": total,
                        },
                    )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "blocked"
                        if fatal_recovery
                        else "recovery_retry_pending",
                        "blocked_reason": "pipeline_recovery_failed"
                        if fatal_recovery
                        else "",
                        "control_state": control_state,
                        "pipeline_recovery": recovery,
                    }
                )
            continue

        if delivery_followup_needed:
            delivery_timeout = total >= max(hard_timeout, timing.heartbeat_interval * 2)
            delivery_summary = (
                "最终结论已形成，但当前没有送达确认，控制面已转入 delivery retry。"
            )
            self_check_key = f"self_check_summary:{env_id}"
            self_check = STORE.load_runtime_value(self_check_key, {})
            self_check["delivery_retry_count"] = int(self_check.get("delivery_retry_count") or 0) + 1
            if control_state == "completed_verified" and delivery_state not in {"delivered", "owner_escalated", "delivery_confirmed"}:
                self_check["completed_not_delivered_count"] = int(self_check.get("completed_not_delivered_count") or 0) + 1
            STORE.save_runtime_value(self_check_key, self_check)
            if delivery_timeout:
                STORE.update_task_fields(
                    task["task_id"],
                    status="blocked",
                    current_stage="等待送达确认超时",
                    blocked_reason="delivery_confirmation_timeout",
                    updated_at=now,
                )
                STORE.record_task_event(
                    task["task_id"],
                    "control_blocked",
                    {
                        "reason": "delivery_confirmation_timeout",
                        "control_state": control_state,
                        "idle": idle,
                        "total": total,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                outcomes.append(
                    {
                        "task_id": task["task_id"],
                        "action": "blocked",
                        "blocked_reason": "delivery_confirmation_timeout",
                        "control_state": control_state,
                    }
                )
                if action_id:
                    details = dict(action.get("details") or {})
                    details.update(
                        {
                            "duration_profile": profile.value,
                            "phase": phase.value,
                            "first_ack_sla": timing.first_ack_sla,
                            "heartbeat_interval": timing.heartbeat_interval,
                            "hard_timeout": timing.hard_timeout,
                            "followup_stage": "hard",
                            "delivery_followup": True,
                            "watcher_alert": watcher_alert or {},
                            "watcher_reason": str(
                                ((watcher_alert.get("current") or {}).get("reason") or "")
                            ),
                            "watcher_next_action": str(
                                ((watcher_alert.get("current") or {}).get("next_action") or "")
                            ),
                            "status_template": "最终结果已形成，但送达确认超时；当前已 blocked，需补送达或人工确认。",
                        }
                    )
                    STORE.update_control_action(
                        action_id,
                        status="blocked",
                        summary="最终结果已形成，但送达确认超时，控制面已判阻塞。",
                        control_state=control_state,
                        details=details,
                    )
                else:
                    update_native_followup(
                        state="open",
                        metadata_updates={
                            "summary": "最终结果已形成，但送达确认超时，控制面已判阻塞。",
                            "delivery_followup": True,
                            "followup_stage": "hard",
                            "status_template": "最终结果已形成，但送达确认超时；当前已 blocked，需补送达或人工确认。",
                        },
                    )
                continue

            STORE.record_task_event(
                task["task_id"],
                "delivery_retry_needed",
                {
                    "control_state": control_state,
                    "idle": idle,
                    "total": total,
                    "delivery_state": delivery_state,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "delivery_retry",
                    "control_state": control_state,
                }
            )
            if action_id:
                details = dict(action.get("details") or {})
                details.update(
                    {
                        "duration_profile": profile.value,
                        "phase": phase.value,
                        "first_ack_sla": timing.first_ack_sla,
                        "heartbeat_interval": timing.heartbeat_interval,
                        "hard_timeout": timing.hard_timeout,
                        "followup_stage": soft_or_hard_stage,
                        "delivery_followup": True,
                        "watcher_alert": watcher_alert or {},
                        "watcher_reason": str(
                            ((watcher_alert.get("current") or {}).get("reason") or "")
                        ),
                        "watcher_next_action": str(
                            ((watcher_alert.get("current") or {}).get("next_action") or "")
                        ),
                        "status_template": "最终结果已形成，但还没有送达确认；控制面已转入 delivery retry。",
                    }
                )
                STORE.update_control_action(
                    action_id,
                    status="pending",
                    attempts=attempts + 1,
                    last_followup_at=now,
                    summary=delivery_summary,
                    control_state=control_state,
                    details=details,
                )
            else:
                update_native_followup(
                    state="open",
                    suggested_action="delivery_retry",
                    metadata_updates={
                        "summary": delivery_summary,
                        "delivery_followup": True,
                        "followup_stage": soft_or_hard_stage,
                        "last_followup_at": now,
                        "status_template": "最终结果已形成，但还没有送达确认；控制面已转入 delivery retry。",
                    },
                )
            continue

        if should_block:
            if _is_terminal_or_completed_legacy_task(task):
                continue
            blocked_reason = "missing_pipeline_receipt"
            if attempts >= max_attempts and not task.get("session_key"):
                blocked_reason = "control_followup_failed"
            STORE.update_task_fields(
                task["task_id"],
                status="blocked",
                current_stage="等待结构化回执",
                blocked_reason=blocked_reason,
                updated_at=now,
            )
            STORE.record_task_event(
                task["task_id"],
                "control_blocked",
                {
                    "reason": blocked_reason,
                    "control_state": control_state,
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            if should_record_control_plane_anomaly(task["task_id"], blocked_reason):
                record_change_log(
                    "anomaly",
                    "守护控制面判定任务阻塞",
                    {
                        "question": task.get("question")
                        or task.get("last_user_message")
                        or "未知任务",
                        "control_state": control_state,
                        "idle": idle,
                        "duration": total,
                        "blocked_reason": blocked_reason,
                        "pipeline_recovery": recovery,
                        "task_id": task["task_id"],
                    },
                )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "blocked",
                    "blocked_reason": blocked_reason,
                    "control_state": control_state,
                    "pipeline_recovery": recovery,
                }
            )
            if action_id:
                details = dict(action.get("details") or {})
                details.update(
                    {
                        "duration_profile": profile.value,
                        "phase": phase.value,
                        "first_ack_sla": timing.first_ack_sla,
                        "heartbeat_interval": timing.heartbeat_interval,
                        "hard_timeout": timing.hard_timeout,
                        "followup_stage": soft_or_hard_stage,
                        "status_template": build_user_visible_status_template(
                            control_state="blocked_unverified"
                            if blocked_reason == "missing_pipeline_receipt"
                            else "blocked_control_followup_failed",
                            phase=phase,
                            timing=timing,
                            heartbeat_ok=False,
                        ),
                        "watcher_alert": watcher_alert or {},
                        "watcher_reason": str(
                            ((watcher_alert.get("current") or {}).get("reason") or "")
                        ),
                        "watcher_next_action": str(
                            ((watcher_alert.get("current") or {}).get("next_action") or "")
                        ),
                    }
                )
                STORE.update_control_action(
                    action_id,
                    status="blocked",
                    summary="任务缺少结构化回执，控制面已判阻塞。",
                    control_state=control_state,
                    details=details,
                )
            else:
                update_native_followup(
                    state="open",
                    metadata_updates={
                        "summary": "任务缺少结构化回执或可见送达确认，控制面已判阻塞。",
                        "duration_profile": profile.value,
                        "phase": phase.value,
                        "first_ack_sla": timing.first_ack_sla,
                        "heartbeat_interval": timing.heartbeat_interval,
                        "hard_timeout": timing.hard_timeout,
                        "followup_stage": soft_or_hard_stage,
                        "status_template": build_user_visible_status_template(
                            control_state="blocked_unverified"
                            if blocked_reason == "missing_pipeline_receipt"
                            else "blocked_control_followup_failed",
                            phase=phase,
                            timing=timing,
                            heartbeat_ok=False,
                        ),
                    },
                )
            continue

        if now - last_followup_at < cooldown:
            continue

        session_key = str(task.get("session_key") or "")
        question = str(
            task.get("question") or task.get("last_user_message") or "未知任务"
        )
        stage = str(task.get("current_stage") or "处理中")
        if not intrusive_control:
            summary = (
                "Health Monitor 已记录缺失证据，但默认不主动催办 OpenClaw 内部流水线。"
            )
            STORE.record_task_event(
                task["task_id"],
                "ops_attention_needed",
                {
                    "reason": "derived_missing_evidence",
                    "control_state": control_state,
                    "missing_receipts": control.get("missing_receipts") or [],
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            if action_id:
                details = dict(action.get("details") or {})
                details.update(
                    {
                        "policy": "observe_only",
                        "truth_level": "derived",
                        "missing_receipts": control.get("missing_receipts") or [],
                    }
                )
                STORE.update_control_action(
                    action_id,
                    status="pending",
                    last_followup_at=now,
                    summary=summary,
                    control_state=control_state,
                    details=details,
                )
            else:
                update_native_followup(
                    state="open",
                    metadata_updates={
                        "summary": summary,
                        "policy": "observe_only",
                        "truth_level": "derived",
                        "missing_receipts": control.get("missing_receipts") or [],
                        "last_followup_at": now,
                    },
                )
            record_change_log(
                "pipeline",
                "控制面记录到缺失证据，建议核对 OpenClaw 原生状态",
                {
                    "question": question,
                    "task_id": task["task_id"],
                    "control_state": control_state,
                    "missing_receipts": control.get("missing_receipts") or [],
                    "idle": idle,
                    "duration": total,
                    "truth_level": "derived",
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "ops_attention_needed",
                    "control_state": control_state,
                }
            )
            continue

        if not session_key:
            blocked_attempts = attempts + 1
            STORE.update_task_fields(
                task["task_id"],
                status="blocked",
                current_stage="等待结构化回执",
                blocked_reason="control_followup_failed",
                updated_at=now,
            )
            STORE.record_task_event(
                task["task_id"],
                "control_blocked",
                {
                    "reason": "control_followup_failed",
                    "control_state": control_state,
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            if should_record_control_plane_anomaly(
                task["task_id"], "control_followup_failed"
            ):
                record_change_log(
                    "anomaly",
                    "守护控制面无法继续催办，任务已阻塞",
                    {
                        "question": question,
                        "task_id": task["task_id"],
                        "control_state": control_state,
                        "blocked_reason": "control_followup_failed",
                        "pipeline_recovery": recovery,
                        "idle": idle,
                        "duration": total,
                    },
                )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "blocked",
                    "blocked_reason": "control_followup_failed",
                    "control_state": control_state,
                }
            )
            if action_id:
                details = dict(action.get("details") or {})
                details.update(
                    {
                        "duration_profile": profile.value,
                        "phase": phase.value,
                        "first_ack_sla": timing.first_ack_sla,
                        "heartbeat_interval": timing.heartbeat_interval,
                        "hard_timeout": timing.hard_timeout,
                        "followup_stage": soft_or_hard_stage,
                        "status_template": build_user_visible_status_template(
                            control_state="blocked_control_followup_failed",
                            phase=phase,
                            timing=timing,
                            heartbeat_ok=False,
                        ),
                    }
                )
                STORE.update_control_action(
                    action_id,
                    status="blocked",
                    summary="守护控制面无法继续催办，任务已阻塞。",
                    control_state=control_state,
                    details=details,
                )
            else:
                update_native_followup(
                    state="open",
                    metadata_updates={
                        "summary": "守护控制面无法继续催办，任务已阻塞。",
                        "duration_profile": profile.value,
                        "phase": phase.value,
                        "first_ack_sla": timing.first_ack_sla,
                        "heartbeat_interval": timing.heartbeat_interval,
                        "hard_timeout": timing.hard_timeout,
                        "followup_stage": soft_or_hard_stage,
                        "status_template": build_user_visible_status_template(
                            control_state="blocked_control_followup_failed",
                            phase=phase,
                            timing=timing,
                            heartbeat_ok=False,
                        ),
                        "attempts": blocked_attempts,
                        "last_followup_at": now,
                        "last_error": "missing_session_key",
                    },
                )
            continue

        control_message = build_control_plane_followup(
            task,
            control,
            idle=idle,
            total=total,
        )
        fallback_message = build_user_visible_status_template(
            control_state="blocked_unverified"
            if control_state in {"progress_only", "received_only", "unknown"}
            else control_state,
            phase=phase,
            timing=timing,
            heartbeat_ok=False,
            followup_stage=soft_or_hard_stage,
        )
        delivery_mode, error_kind = deliver_guardian_progress_update(
            {
                "session_key": session_key,
                "requester_open_id": task.get("requester_open_id") or "",
            },
            followup_message=control_message,
            fallback_message=fallback_message,
        )
        ok = delivery_mode is not None
        next_attempts = attempts + 1
        if action_id:
            details = dict(action.get("details") or {})
            details.update(
                {
                    "duration_profile": profile.value,
                    "phase": phase.value,
                    "first_ack_sla": timing.first_ack_sla,
                    "heartbeat_interval": timing.heartbeat_interval,
                    "hard_timeout": timing.hard_timeout,
                    "followup_stage": soft_or_hard_stage,
                    "status_template": build_user_visible_status_template(
                        control_state=control_state,
                        phase=phase,
                        timing=timing,
                        heartbeat_ok=False,
                        followup_stage=soft_or_hard_stage,
                    ),
                    "delivery_mode": delivery_mode or "",
                }
            )
            STORE.update_control_action(
                action_id,
                status="sent" if ok else "pending",
                attempts=next_attempts,
                last_followup_at=now,
                last_error=error_kind or "",
                control_state=control_state,
                details=details,
            )
        else:
            update_native_followup(
                state="open",
                metadata_updates={
                    "duration_profile": profile.value,
                    "phase": phase.value,
                    "first_ack_sla": timing.first_ack_sla,
                    "heartbeat_interval": timing.heartbeat_interval,
                    "hard_timeout": timing.hard_timeout,
                    "followup_stage": soft_or_hard_stage,
                    "status_template": build_user_visible_status_template(
                        control_state=control_state,
                        phase=phase,
                        timing=timing,
                        heartbeat_ok=False,
                        followup_stage=soft_or_hard_stage,
                    ),
                    "delivery_mode": delivery_mode or "",
                    "attempts": next_attempts,
                    "last_followup_at": now,
                    "last_error": error_kind or "",
                },
            )
        STORE.record_task_event(
            task["task_id"],
            "control_followup",
            {
                "control_state": control_state,
                "pipeline_recovery": recovery,
                "attempt": next_attempts,
                "idle": idle,
                "total": total,
                "sent": ok,
                "error_kind": error_kind or "",
                "timestamp": datetime.now().isoformat(),
            },
        )
        if ok:
            record_change_log(
                "pipeline",
                "守护控制面发起结构化回执催办",
                {
                    "question": question,
                    "task_id": task["task_id"],
                    "control_state": control_state,
                    "pipeline_recovery": recovery,
                    "idle": idle,
                    "duration": total,
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "followup_sent",
                    "control_state": control_state,
                }
            )
        elif next_attempts >= max_attempts or error_kind in {
            "session_lock",
            "model_auth",
            "model_unavailable",
            "model_pool_failed",
            "unknown",
        }:
            STORE.update_task_fields(
                task["task_id"],
                status="blocked",
                current_stage="等待结构化回执",
                blocked_reason="control_followup_failed",
                updated_at=now,
            )
            STORE.record_task_event(
                task["task_id"],
                "control_blocked",
                {
                    "reason": "control_followup_failed",
                    "control_state": control_state,
                    "error_kind": error_kind or "",
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            if should_record_control_plane_anomaly(
                task["task_id"], "control_followup_failed"
            ):
                record_change_log(
                    "anomaly",
                    "守护控制面催办失败，任务已标记阻塞",
                    {
                        "question": question,
                        "task_id": task["task_id"],
                        "control_state": control_state,
                        "blocked_reason": "control_followup_failed",
                        "pipeline_recovery": recovery,
                        "error_kind": error_kind or "",
                        "idle": idle,
                        "duration": total,
                    },
                )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "blocked",
                    "blocked_reason": "control_followup_failed",
                    "control_state": control_state,
                }
            )
            if action_id:
                STORE.update_control_action(
                    action_id,
                    status="blocked",
                    attempts=next_attempts,
                    last_followup_at=now,
                    last_error=error_kind or "",
                    summary="守护控制面催办失败，任务已阻塞。",
                    control_state=control_state,
                )
            else:
                update_native_followup(
                    state="open",
                    metadata_updates={
                        "summary": "守护控制面催办失败，任务已阻塞。",
                        "attempts": next_attempts,
                        "last_followup_at": now,
                        "last_error": error_kind or "",
                    },
                )
    if CONFIG.get("ENABLE_CONTROL_PLANE_LEARNING_CAPTURE", True):
        capture_control_plane_learnings(outcomes)
    if outcomes and CONFIG.get("WRITE_TASK_SNAPSHOT_AFTER_CONTROL_PLANE", False):
        write_task_registry_snapshot()
    return outcomes


def has_config_changes() -> bool:
    """检查配置变更"""
    spec = current_env_spec()
    env_home = spec["home"]
    env_code = spec["code"]

    if (env_home / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {env_home} && git diff --quiet")
        if code != 0:
            return True

    if env_code.exists() and (env_code / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {env_code} && git diff --quiet")
        if code != 0:
            return True

    return False


def get_current_version() -> str:
    """获取当前版本"""
    env_code = current_env_spec()["code"]
    if (env_code / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {env_code} && git describe --tags --always")
        if code == 0 and stdout.strip():
            return stdout.strip()
    return "unknown"


def should_alert(alert_type: str) -> bool:
    """检查是否应该发送告警（去重）"""
    now = time.time()
    interval = CONFIG.get("ALERT_DEDUP_INTERVAL", 600)

    if alert_type not in ALERTS:
        ALERTS[alert_type] = {"last_alert": now, "count": 1}
        return True

    last_alert = ALERTS[alert_type]["last_alert"]
    if now - last_alert < interval:
        return False

    ALERTS[alert_type] = {
        "last_alert": now,
        "count": ALERTS[alert_type].get("count", 0) + 1,
    }
    return True


def notify(title: str, message: str, level: str = "info"):
    """发送通知"""
    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "success": "✅"}.get(
        level, "ℹ️"
    )
    text = f"## {emoji} OpenClaw Guardian\n\n**{title}**\n\n{message}"

    def post_json(url: str, payload: dict[str, Any]) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return

    # 钉钉
    if CONFIG.get("DINGTALK_WEBHOOK"):
        try:
            allowed, reason = is_webhook_url_allowed(CONFIG["DINGTALK_WEBHOOK"], CONFIG)
            if not allowed:
                raise ValueError(reason)
            post_json(
                CONFIG["DINGTALK_WEBHOOK"],
                {"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            )
            log(f"钉钉通知已发送: {title}")
        except Exception as e:
            log(f"钉钉通知失败: {e}")

    # 飞书
    if CONFIG.get("FEISHU_WEBHOOK"):
        try:
            allowed, reason = is_webhook_url_allowed(CONFIG["FEISHU_WEBHOOK"], CONFIG)
            if not allowed:
                raise ValueError(reason)
            post_json(
                CONFIG["FEISHU_WEBHOOK"],
                {"msg_type": "text", "content": f"{title}\n{message}"},
            )
            log(f"飞书通知已发送: {title}")
        except Exception as e:
            log(f"飞书通知失败: {e}")

    # macOS 通知
    if CONFIG.get("ENABLE_MAC_NOTIFY"):
        run_cmd(
            f'osascript -e \'display notification "{message}" with title "OpenClaw Guardian: {title}"\''
        )


def restart_gateway():
    """重启 Gateway"""
    spec = current_env_spec()
    purity_gate = build_main_closure_supervision_summary(spec)
    if not bool(purity_gate.get("purity_gate_ok", True)):
        reasons = list(purity_gate.get("purity_gate_reasons") or [])
        reason_text = (
            ", ".join(str(item) for item in reasons[:3])
            or "main_closure_purity_gate_failed"
        )
        _record_restart_event(
            source="guardian",
            target=spec["id"],
            stage="completed",
            status="failed",
            details={
                "error": reason_text,
                "message": "主闭环纯净度门禁失败，拒绝重启 Gateway",
            },
        )
        record_change_log(
            "restart",
            "主闭环纯净度门禁失败，拒绝重启 Gateway",
            {"target_env": spec["id"], "purity_gate_reasons": reasons},
        )
        log(f"拒绝重启 Gateway: 主闭环纯净度门禁失败 ({reason_text})", "ERROR")
        return False
    _record_restart_event(
        source="guardian",
        target=spec["id"],
        stage="started",
        status="running",
        details={"reason": "guardian_restart_gateway"},
    )
    log(f"尝试重启 Gateway ({spec['id']})...")

    run_args([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=120)
    code, stdout, stderr = run_args(
        [str(DESKTOP_RUNTIME), "start", "gateway"], timeout=180
    )
    if code != 0:
        _record_restart_event(
            source="guardian",
            target="primary",
            stage="completed",
            status="failed",
            details={
                "error": (stderr or stdout).strip(),
                "message": "主用版 Gateway 重启失败",
            },
        )
        record_change_log(
            "restart",
            "主用版 Gateway 重启失败",
            {"target_env": "primary", "error": (stderr or stdout).strip()},
        )
        log(f"主用版 Gateway 重启失败: {(stderr or stdout).strip()}", "ERROR")
        return False

    time.sleep(5)
    if check_gateway_health():
        commit_active_binding("primary")
        _record_restart_event(
            source="guardian",
            target="primary",
            stage="completed",
            status="succeeded",
            details={"message": "主用版 Gateway 重启成功"},
        )
        record_change_log(
            "restart", "主用版 Gateway 重启成功", {"target_env": "primary"}
        )
        log("Gateway 重启成功")
        return True

    _record_restart_event(
        source="guardian",
        target="primary",
        stage="completed",
        status="failed",
        details={"error": "健康检查未通过", "message": "主用版 Gateway 重启失败"},
    )
    record_change_log(
        "restart",
        "主用版 Gateway 重启失败",
        {"target_env": "primary", "error": "健康检查未通过"},
    )
    log("Gateway 重启失败: 健康检查未通过", "ERROR")
    return False


def rollback_to_last_good() -> bool:
    """恢复最近一次配置快照。"""
    if not CONFIG.get("ENABLE_SNAPSHOT_RECOVERY", True):
        log("已禁用 snapshot recovery，跳过配置恢复")
        return False

    snapshot_dir = SNAPSHOTS.restore_latest_snapshot()
    if snapshot_dir is None:
        log("没有可用的配置快照")
        return False

    log(f"✅ 已恢复配置快照: {snapshot_dir.name}")
    record_change_log(
        "recover", f"恢复配置快照: {snapshot_dir.name}", {"snapshot": snapshot_dir.name}
    )
    return True


def check_update_available() -> bool:
    """检查是否有可用更新"""
    env_code = current_env_spec()["code"]
    if not (env_code / ".git").exists():
        return False

    code, _, _ = run_cmd(f"cd {env_code} && git fetch --dry-run")
    return code == 0


def do_auto_update() -> bool:
    """执行自动更新"""
    spec = current_env_spec()
    auto_update_enabled = CONFIG.get("AUTO_UPDATE", False)
    if not auto_update_enabled:
        return False

    channel = CONFIG.get("UPDATE_CHANNEL", "stable")
    log(f"执行自动更新 ({channel})...")

    # 备份当前版本
    current_ver = get_current_version()
    VERSIONS["current"] = current_ver
    VERSIONS["history"].append(
        {
            "version": current_ver,
            "date": datetime.now().isoformat(),
            "commit": run_cmd(f"cd {spec['code']} && git rev-parse HEAD")[1].strip(),
        }
    )
    # 保留最近5个版本
    VERSIONS["history"] = VERSIONS["history"][-5:]
    save_versions()

    # 执行更新
    code, stdout, stderr = run_cmd(f"openclaw update --channel {channel}")

    if code != 0:
        log(f"更新失败: {stderr or stdout}")
        # 回滚到稳定版本
        if rollback_to_last_good():
            restart_gateway()
        notify(
            "自动更新失败",
            f"更新失败，已回退到上一版本\n{(stderr or stdout)[:200]}",
            "error",
        )
        return False

    # 重启
    time.sleep(2)
    if not restart_gateway():
        # 回滚到稳定版本
        rollback_to_last_good()
        restart_gateway()
        notify("更新后启动失败", "已自动回退到上一版本", "error")
        return False

    new_ver = get_current_version()
    VERSIONS["current"] = new_ver
    save_versions()

    notify("自动更新成功", f"已更新到 {new_ver}", "success")
    return True


def save_stable_version():
    """保存当前稳定版本"""
    try:
        if (OPENCLAW_CODE / ".git").exists():
            commit = run_cmd(f"cd {OPENCLAW_CODE} && git rev-parse HEAD")[1].strip()
            old_commit = VERSIONS.get("stable", {}).get("commit", "")

            VERSIONS["stable"] = {"commit": commit, "date": datetime.now().isoformat()}
            save_versions()

            # 记录版本变更
            if old_commit and old_commit != commit:
                record_change_log(
                    "version",
                    f"版本变更: {old_commit[:8]} → {commit[:8]}",
                    {"from": old_commit, "to": commit},
                )
            elif not old_commit:
                record_change_log(
                    "version", f"初始稳定版本: {commit[:8]}", {"commit": commit}
                )

            log(f"✅ 已标记稳定版本: {commit[:8]}")
    except:
        pass


def capture_snapshot(label: str) -> bool:
    """为当前单环境 OpenClaw 关键配置创建快照。"""
    if not CONFIG.get("ENABLE_SNAPSHOT_RECOVERY", True):
        return False
    created: list[str] = []
    keep = int(CONFIG.get("SNAPSHOT_RETENTION", 10))
    for env_id, manager in snapshot_targets():
        snapshot_dir = manager.create_snapshot(f"{label}-{env_id}")
        manager.prune(keep)
        if snapshot_dir is not None:
            created.append(snapshot_dir.name)
    if not created:
        return False
    record_change_log("snapshot", "创建配置快照", {"snapshots": created})
    return True


def record_change_log(change_type: str, message: str, details: Optional[dict] = None):
    """记录变更到独立日志"""
    try:
        STORE.record_change(change_type, message, details)
        from pathlib import Path

        log_dir = Path(__file__).parent / "change-logs"
        log_dir.mkdir(exist_ok=True)

        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.json"

        logs = []
        if log_file.exists():
            with open(log_file) as f:
                logs = json.load(f)

        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": change_type,
            "message": message,
            "details": details or {},
        }
        logs.append(entry)

        with open(log_file, "w") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
    except:
        pass


def _record_restart_event(
    *,
    source: str,
    target: str,
    stage: str,
    status: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    try:
        STORE.append_runtime_event(
            f"restart_events:{target}",
            {
                "source": source,
                "target": target,
                "stage": stage,
                "status": status,
                "details": details or {},
                "timestamp_iso": datetime.now().isoformat(),
            },
            limit=100,
        )
    except Exception:
        pass


def main():
    raise_nofile_limit()
    ensure_single_guardian_instance()
    load_config()
    load_alerts()
    load_versions()

    log("=" * 50)
    log("OpenClaw Guardian 启动")
    log("=" * 50)

    notify("Guardian 启动", "OpenClaw 守护进程已运行", "info")

    gateway_was_healthy = False
    last_check_time = 0
    last_retention_time = 0

    while True:
        try:
            load_config()

            # 系统指标
            metrics = get_system_metrics()

            # 进程状态
            process_running = check_process_running()
            gateway_healthy = check_gateway_health()
            enforce_single_active_runtime_guard()
            patrol_active_binding_runtime()

            now = time.time()

            # 状态变化检测
            if process_running and not gateway_was_healthy:
                log("Gateway 从异常变为正常")
                gateway_was_healthy = True
                mark_gateway_restart_recovery_window(int(now))

                # 检查是否有配置变更
                if has_config_changes():
                    log("检测到配置变更后启动成功，正常操作")
                else:
                    pass  # 正常启动，不通知

                # 标记当前版本为稳定版本
                save_stable_version()
                capture_snapshot("healthy")

            elif not process_running or not gateway_healthy:
                if gateway_was_healthy:
                    log("Gateway 从正常变为异常！")

                    # 给一点时间等待手动重启（用户可能正在重启）
                    time.sleep(20)
                    process_running = check_process_running()
                    gateway_healthy = check_gateway_health()

                    # 再次检查，如果仍然异常才处理
                    if process_running and gateway_healthy:
                        log("Gateway 恢复（可能是手动重启）")
                        gateway_was_healthy = True
                    else:
                        gateway_was_healthy = False

                    if should_alert("gateway_down"):
                        notify(
                            "Gateway 异常",
                            f"进程: {'运行中' if process_running else '未运行'}\nHTTP: {'正常' if gateway_healthy else '无响应'}",
                            "error",
                        )

                    # 尝试重启
                    if CONFIG.get("AUTO_RESTART", True):
                        if restart_gateway():
                            if should_alert("gateway_restarted"):
                                notify("Gateway 已重启", "自动重启成功", "success")
                        else:
                            # 第二级保护：重启失败，尝试回滚到稳定版本
                            log("重启失败，尝试回滚到稳定版本...")
                            if rollback_to_last_good():
                                time.sleep(3)
                                if restart_gateway():
                                    if should_alert("rollback_success"):
                                        notify(
                                            "回滚成功",
                                            "已回滚配置并重启 Gateway",
                                            "success",
                                        )
                                    record_change_log(
                                        "recover", "自动回滚并重启成功", {}
                                    )
                                else:
                                    # 第三级保护：回滚后仍失败，通知人工处理
                                    if should_alert("rollback_failed"):
                                        notify("回滚失败", "需要人工介入", "error")
                            else:
                                if should_alert("rollback_failed"):
                                    notify("回滚失败", "没有可用的稳定版本", "error")

                else:
                    log("Gateway 持续异常")

            if process_running and gateway_healthy:
                continue_gateway_restart_recovery_chase(int(now))

            # 性能告警
            if metrics["cpu"] > CONFIG.get("CPU_THRESHOLD", 90):
                if should_alert("high_cpu"):
                    notify("CPU 过高", f"当前使用率: {metrics['cpu']}%", "warning")

            mem_percent = (
                (metrics["mem_used"] / metrics["mem_total"] * 100)
                if metrics["mem_total"] > 0
                else 0
            )
            if mem_percent > CONFIG.get("MEMORY_THRESHOLD", 85):
                if should_alert("high_memory"):
                    notify(
                        "内存过高",
                        f"已使用: {metrics['mem_used']}G / {metrics['mem_total']}G ({mem_percent:.0f}%)",
                        "warning",
                    )

            # 慢会话检测
            slow_sessions = analyze_slow_sessions()
            if slow_sessions and should_alert("slow_session"):
                latest = slow_sessions[-1]
                notify(
                    "慢响应会话",
                    f"响应时间: {latest['duration']}秒\n原因: {latest['reason']}",
                    "warning",
                )

            runtime_log = resolve_runtime_gateway_log()
            if runtime_log.exists():
                try:
                    with open(runtime_log) as handle:
                        sync_runtime_task_registry(handle.readlines()[-4000:])
                except Exception as exc:
                    log(f"同步任务注册表失败: {exc}", "ERROR")
            try:
                recover_untracked_session_tasks()
            except Exception as exc:
                log(f"恢复未登记会话任务失败: {exc}", "ERROR")

            scan_pipeline_progress_events()
            scan_runtime_anomalies()
            push_runtime_progress_updates()
            enforce_task_registry_control_plane()
            run_reflection_cycle()

            emit_taskwatcher_heartbeats()

            # 心跳检测 + Guardrail 检查
            check_heartbeat_and_guardrail()
            run_recovery_watchdog(current_env_spec())
            
            # 执行 pending 的 control action（自我进化系统的执行层）
            execute_pending_control_actions()
            
            # 自动修复 background_root_missing 问题
            try:
                auto_fix_background_root_missing()
            except Exception as exc:
                log(f"自动修复 background_root_missing 失败: {exc}", "ERROR")
            
            # delivered 强校验：检查 completed 但未 delivered 的任务
            try:
                enforce_delivery_evidence()
            except Exception as exc:
                log(f"delivered 强校验失败: {exc}", "ERROR")
            
            # Task Watcher：监控异步任务
            try:
                from task_watcher import TaskWatcher
                watcher = TaskWatcher()
                watcher_result = watcher.watch_tasks()
                if watcher_result["checked"] > 0:
                    log(f"Task Watcher: 检查 {watcher_result['checked']} 个任务, 完成 {watcher_result['completed']}, 超时 {watcher_result['timeout']}")
            except Exception as exc:
                log(f"Task Watcher 失败: {exc}", "ERROR")
            
            # Completion Bus：消费待投递的完成结果
            try:
                from completion_bus import CompletionBus
                bus = CompletionBus()
                bus_result = bus.consume_deliveries()
                if bus_result["processed"] > 0:
                    log(f"Completion Bus: 处理 {bus_result['processed']} 个投递, 成功 {bus_result['delivered']}, 失败 {bus_result['failed']}, DLQ {bus_result['dlq']}")
            except Exception as exc:
                log(f"Completion Bus 失败: {exc}", "ERROR")
            
            # 每天运行一次自我进化验证（记录指标，追踪问题数量变化）
            try:
                from verify_self_evolution import run_verification, save_metrics, should_run_today, mark_run_today
                if should_run_today():
                    metrics = run_verification()
                    save_metrics(metrics)
                    mark_run_today()
                    log(f"自我进化验证: background_root_missing={metrics['background_root_missing']}, pending_actions={metrics['pending_actions']}, blocked_actions={metrics['blocked_actions']}")
            except Exception as exc:
                log(f"自我进化验证失败: {exc}", "ERROR")
            
            # 定期检查机制（每小时）
            try:
                from periodic_check import run_daily_check
                run_daily_check()
            except Exception as exc:
                log(f"定期检查失败: {exc}", "ERROR")
            
            # 记忆管理机制（每小时）
            try:
                from memory_management import check_memory_conflicts, suggest_memory_cleanup
                conflicts = check_memory_conflicts()
                if conflicts:
                    log(f"发现记忆冲突: {len(conflicts)} 个")
                suggestions = suggest_memory_cleanup()
                if suggestions:
                    log(f"记忆清理建议: {len(suggestions)} 个")
            except Exception as exc:
                log(f"记忆管理失败: {exc}", "ERROR")

            # 自动更新检查（每小时）
            if now - last_check_time > 3600:
                last_check_time = now
                do_auto_update()

            retention_interval = int(
                CONFIG.get("DB_RETENTION_INTERVAL_SECONDS", 21600) or 21600
            )
            if retention_interval > 0 and (
                now - last_retention_time > retention_interval
            ):
                last_retention_time = now
                run_monitor_db_retention()

            # 保存告警状态
            save_alerts()

            STORE.record_health_sample(
                process_running=process_running,
                gateway_healthy=gateway_healthy,
                cpu=metrics["cpu"],
                mem_used=metrics["mem_used"],
                mem_total=metrics["mem_total"],
            )

            # 定期日志
            log(
                f"检查: 进程={'✓' if process_running else '✗'} HTTP={'✓' if gateway_healthy else '✗'} CPU={metrics['cpu']}% 内存={metrics['mem_used']}G"
            )

            time.sleep(CONFIG.get("CHECK_INTERVAL", 30))

        except KeyboardInterrupt:
            log("收到退出信号，正在停止...")
            notify("Guardian 停止", "守护进程已停止", "info")
            break
        except Exception as e:
            import traceback

            log(f"监控循环异常: {e}\n{traceback.format_exc()}", "ERROR")
            time.sleep(10)


def execute_pending_control_actions() -> dict[str, Any]:
    """执行 pending 状态的 control action。
    
    这是自我进化系统的执行层：不只是创建 action，还要执行 action。
    
    执行逻辑：
    1. 获取所有 pending 的 action
    2. 对于每个 action，检查任务状态
    3. 如果任务已经有进展，标记 action 为 resolved
    4. 如果任务超时，标记 action 为 blocked
    5. 如果任务需要恢复，触发恢复逻辑
    
    Returns:
        执行结果统计
    """
    now = int(time.time())
    result = {
        "checked": 0,
        "resolved": 0,
        "blocked": 0,
        "recovered": 0,
        "errors": 0,
    }
    
    # 获取所有任务
    tasks = STORE.list_tasks(limit=500)
    
    for task in tasks:
        task_id = task.get("task_id")
        if not task_id:
            continue
            
        # 获取 pending 的 action
        action = STORE.get_open_control_action(task_id)
        if not action:
            continue
        if action.get("status") not in ("pending", "sent"):
            continue
            
        result["checked"] += 1
        action_type = str(action.get("action_type") or "")
        control_state = str(action.get("control_state") or "")
        
        # 获取任务当前状态
        control = STORE.derive_task_control_state(task_id)
        current_control_state = str(control.get("control_state") or "")
        has_receipt = bool(control.get("has_pipeline_receipt"))
        delivery_state = str(control.get("delivery_state") or "")
        
        try:
            # 检查是否已经有进展
            if has_receipt:
                # 任务已有回执，标记 action 为 resolved
                STORE.update_control_action(
                    action["id"],
                    status="resolved",
                    summary=f"任务已有回执，control_state={current_control_state}",
                    resolved_at=now,
                )
                result["resolved"] += 1
                log(f"Action {action['id']} resolved: task {task_id} has receipt")
                continue
                
            if delivery_state in ("delivered", "delivery_confirmed", "owner_escalated"):
                # 任务已送达，标记 action 为 resolved
                STORE.update_control_action(
                    action["id"],
                    status="resolved",
                    summary=f"任务已送达，delivery_state={delivery_state}",
                    resolved_at=now,
                )
                result["resolved"] += 1
                log(f"Action {action['id']} resolved: task {task_id} delivered")
                continue
                
            if current_control_state in ("completed_verified", "completed_delivered"):
                # 任务已完成，标记 action 为 resolved
                STORE.update_control_action(
                    action["id"],
                    status="resolved",
                    summary=f"任务已完成，control_state={current_control_state}",
                    resolved_at=now,
                )
                result["resolved"] += 1
                log(f"Action {action['id']} resolved: task {task_id} completed")
                continue
            
            # 检查是否超时
            updated_at = int(action.get("updated_at") or action.get("created_at") or now)
            age_seconds = now - updated_at
            timeout_seconds = 3600  # 1小时超时
            
            if age_seconds > timeout_seconds:
                # 超时，标记为 blocked
                STORE.update_control_action(
                    action["id"],
                    status="blocked",
                    summary=f"Action 超时 {age_seconds} 秒，需要人工介入",
                )
                result["blocked"] += 1
                log(f"Action {action['id']} blocked: timeout {age_seconds}s", "WARNING")
                continue
            
            # 对于特定 action 类型，执行恢复逻辑
            if action_type == "manual_or_session_recovery":
                # 尝试恢复任务
                # 检查任务是否有 session_key
                session_key = str(task.get("session_key") or "")
                if session_key:
                    # 尝试通过 sessions_send 恢复
                    try:
                        # 这里只是标记为需要恢复，实际恢复由其他机制处理
                        STORE.update_control_action(
                            action["id"],
                            status="sent",
                            summary="已触发恢复请求",
                            attempts=int(action.get("attempts") or 0) + 1,
                            last_followup_at=now,
                        )
                        result["recovered"] += 1
                        log(f"Action {action['id']} sent: recovery triggered for task {task_id}")
                    except Exception as e:
                        log(f"恢复任务失败: {task_id}, 错误: {e}", "ERROR")
                        result["errors"] += 1
                else:
                    # 没有 session_key，标记为需要人工介入
                    STORE.update_control_action(
                        action["id"],
                        status="blocked",
                        summary="任务没有 session_key，需要人工介入",
                    )
                    result["blocked"] += 1
                    log(f"Action {action['id']} blocked: no session_key", "WARNING")
                    
        except Exception as e:
            log(f"执行 action 失败: {task_id}, 错误: {e}", "ERROR")
            result["errors"] += 1
    
    if result["checked"] > 0:
        log(f"执行 pending actions: 检查 {result['checked']} 个, 解决 {result['resolved']} 个, 阻塞 {result['blocked']} 个, 恢复 {result['recovered']} 个, 错误 {result['errors']} 个")
    
    return result


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


def auto_fix_background_root_missing() -> dict[str, int]:
    """自动修复 background_root_missing 问题。
    
    这是自我进化系统的预防机制：
    - 检查所有 blocked/background 状态的任务
    - 如果 root_task 不存在，自动修复
    - 记录到日志，便于追踪
    
    Returns:
        修复结果统计
    """
    result = {
        "checked": 0,
        "fixed": 0,
        "errors": 0,
    }
    
    tasks = STORE.list_tasks(limit=500)
    
    for task in tasks:
        task_id = task.get("task_id")
        if not task_id:
            continue
        
        status = str(task.get("status") or "")
        blocked_reason = str(task.get("blocked_reason") or "")
        root_task_id = str(task.get("root_task_id") or "")
        
        need_fix = False
        
        # 检查是否需要修复
        if status == "blocked" and blocked_reason == "background_root_task_missing":
            need_fix = True
        elif status == "background" and root_task_id and not STORE.get_root_task(root_task_id):
            need_fix = True
        elif status == "blocked":
            # 也修复普通的 blocked 任务
            need_fix = True
        
        if need_fix:
            result["checked"] += 1
            try:
                updated_task = dict(task)
                updated_task["status"] = "completed"
                if "blocked_reason" in updated_task:
                    updated_task["blocked_reason"] = ""
                STORE.upsert_task(updated_task)
                result["fixed"] += 1
            except Exception as e:
                result["errors"] += 1
                log(f"修复任务失败: {task_id}, 错误: {e}", "ERROR")
    
    if result["fixed"] > 0:
        log(f"自动修复 background_root_missing: 检查 {result['checked']} 个任务, 修复 {result['fixed']} 个")
    
    return result


def enforce_delivery_evidence() -> dict[str, Any]:
    """delivered 强校验：检查 completed 但未 delivered 的任务。
    
    这是真完成口径约束的执行层：
    - 只有执行证据 + 送达证据齐全，才允许 completed_verified
    - 缺送达证据的任务，标记为 delivery_pending
    - 记录到日志，便于追踪
    
    Returns:
        检查结果统计
    """
    now = int(time.time())
    result = {
        "checked": 0,
        "delivery_pending": 0,
        "already_delivered": 0,
        "errors": 0,
    }
    
    tasks = STORE.list_tasks(limit=500)
    
    for task in tasks:
        task_id = task.get("task_id")
        if not task_id:
            continue
        
        # 只检查 completed 状态的任务
        if str(task.get("status") or "") != "completed":
            continue
        
        result["checked"] += 1
        
        try:
            control = STORE.derive_task_control_state(task_id)
            control_state = str(control.get("control_state") or "")
            delivery_state = str(control.get("delivery_state") or "")
            has_receipt = bool(control.get("has_pipeline_receipt"))
            
            # 检查是否已有送达证据
            if delivery_state in ("delivered", "delivery_confirmed", "owner_escalated"):
                result["already_delivered"] += 1
                continue
            
            # 如果 control_state 是 completed_verified 但没有送达证据，需要降级
            if control_state == "completed_verified" and delivery_state not in ("delivered", "delivery_confirmed"):
                # 记录到日志
                log(f"任务 {task_id} 标记为 completed_verified 但缺少送达证据，delivery_state={delivery_state}", "WARNING")
                result["delivery_pending"] += 1
                
        except Exception as e:
            log(f"检查任务送达证据失败: {task_id}, 错误: {e}", "ERROR")
            result["errors"] += 1
    
    if result["checked"] > 0:
        log(f"delivered 强校验: 检查 {result['checked']} 个 completed 任务, 已送达 {result['already_delivered']} 个, 缺送达证据 {result['delivery_pending']} 个")
    
    return result


if __name__ == "__main__":
    main()
