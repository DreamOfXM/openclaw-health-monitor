"""
数据收集服务
为 dashboard_v2 统一适配后端兼容层的真实数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import importlib
import json
import re
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHARED_STATE_DIR = PROJECT_ROOT / "data" / "shared-state"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class CacheEntry:
    """缓存条目"""

    data: Any
    timestamp: datetime
    ttl: int  # seconds

    def is_expired(self) -> bool:
        return (datetime.now() - self.timestamp).total_seconds() > self.ttl


class DataCache:
    """数据缓存管理器"""

    def __init__(self):
        self._cache: Dict[str, CacheEntry] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._cache[key]
            return None
        return entry.data

    def set(self, key: str, data: Any, ttl: int):
        self._cache[key] = CacheEntry(data=data, timestamp=datetime.now(), ttl=ttl)

    def invalidate(self, key: str):
        if key in self._cache:
            del self._cache[key]

    def invalidate_all(self):
        self._cache.clear()


@lru_cache(maxsize=1)
def _legacy_dashboard():
    return importlib.import_module("dashboard_backend")


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _iso_from_timestamp(value: Any) -> Optional[str]:
    try:
        ts = int(value or 0)
    except Exception:
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts).isoformat()


def _coerce_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _extract_agent_id_from_log_line(line: str) -> Optional[str]:
    match = re.search(r"session=agent:([^:]+):", line)
    if match:
        return match.group(1).strip() or None
    match = re.search(r"\bagent=([a-zA-Z0-9_-]+)\b", line)
    if match:
        return match.group(1).strip() or None
    return None


def _extract_log_timestamp(line: str) -> int:
    if len(line) < 19:
        return 0
    chunk = line[:32]
    try:
        if "+" in chunk[19:] or "Z" in chunk:
            return int(datetime.fromisoformat(chunk.replace("Z", "+00:00")).timestamp())
        return int(datetime.fromisoformat(chunk).timestamp())
    except Exception:
        return 0


def _clean_log_detail(line: str) -> str:
    text = re.sub(r"^\S+\s+\[[^\]]+\]\s+", "", line).strip()
    text = re.sub(r"^\{.*?\}\s*", "", text).strip()
    text = re.sub(r"session=agent:[^ ]+\s*", "", text).strip()
    return text[:220]


def _extract_text_items(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    items: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                items.append(text)
    return items


def _strip_timestamp_prefix(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^\[[^\]]+\]\s*", "", value)
    value = re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s*", "", value)
    return value.strip()


def _is_placeholder_task_text(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return True
    return lowered in {
        "-",
        "--",
        "[粘贴用户原始需求]",
        "粘贴用户原始需求",
        "未命名任务",
        "暂无",
    }


def _compress_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _shorten(text: str, limit: int = 120) -> str:
    value = _compress_whitespace(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _extract_task_hint_from_text(text: str) -> str:
    raw = _strip_timestamp_prefix(text)
    if not raw:
        return ""
    patterns = (
        r"\[Subagent Task\]:(.+)",
        r"主人需求[：:](.+)",
        r"任务标题[：:](.+)",
        r"目标[：:](.+)",
        r"^task[：:](.+)",
        r"\ntask[：:](.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.DOTALL)
        if not match:
            continue
        chunk = match.group(1).strip()
        for line in chunk.splitlines():
            candidate = _shorten(line, 120)
            if candidate and not _is_placeholder_task_text(candidate):
                return candidate
    first_line = _shorten(raw.splitlines()[0], 120)
    if _is_placeholder_task_text(first_line):
        return ""
    return first_line


def _classify_log_signal(detail: str) -> tuple[str, str]:
    text = _compress_whitespace(detail).lower()
    if not text:
        return "idle", "没有新的运行信号"
    if "blocked" in text or "failed" in text:
        return "blocked", "最近运行信号显示代理被阻塞"
    if (
        "dispatching to agent" in text
        or "spawn" in text
        or "execut" in text
        or "toolcall" in text
    ):
        return "processing", "最近日志显示代理正在执行或派发任务"
    if "dispatch complete" in text or "announce_skip" in text:
        return "waiting_downstream", "最近日志显示任务已继续下发，正在等待后续结果"
    if "no_reply" in text or "timeout" in text:
        return "silent_waiting", "最近日志显示代理在等待内部链路继续推进"
    return "observed", "最近日志记录到代理活动，但未看到明确执行状态"


def _runtime_value(legacy: Any, key: str, default: Any) -> Any:
    try:
        store = getattr(legacy, "STORE", None)
        if store is None:
            return default
        return store.load_runtime_value(key, default)
    except Exception:
        return default


class DataCollector:
    """数据收集器"""

    REFRESH_INTERVALS = {
        "health_score": 5,
        "metrics": 5,
        "events": 5,
        "environment": 10,
        "agents": 30,
        "tasks": 10,
        "learnings": 60,
        "config": -1,
        "snapshots": 30,
    }

    def __init__(self):
        self.cache = DataCache()

    def _get_cached_or_fetch(
        self,
        key: str,
        fetch_func: Callable[[], Any],
        force_refresh: bool = False,
    ) -> Any:
        if not force_refresh:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        data = fetch_func()
        ttl = self.REFRESH_INTERVALS.get(key, 60)
        if ttl > 0:
            self.cache.set(key, data, ttl)
        return data

    def _shared_state(self, name: str, default: Any) -> Any:
        return _read_json_file(SHARED_STATE_DIR / name, default)

    def _shared_state_fresh(
        self, name: str, default: Any, *, max_age_seconds: int
    ) -> Any:
        payload = self._shared_state(name, default)
        if not isinstance(payload, dict):
            return default
        generated_at = int(payload.get("generated_at") or 0)
        if generated_at <= 0:
            return default
        if (int(time.time()) - generated_at) > max_age_seconds:
            return default
        return payload

    def _load_runtime_context(self) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        config = legacy.load_config()
        runtime_binding = _runtime_value(legacy, "active_openclaw_env", {})
        # 单环境模式：即使历史 runtime 里残留 official，也只返回 primary。
        active_env = "primary"
        selected_env = legacy.env_spec(active_env, config)
        return {
            "legacy": legacy,
            "config": config,
            "binding": {
                "active_env": active_env,
                "switch_state": str(runtime_binding.get("switch_state") or "committed"),
                "updated_at": runtime_binding.get("updated_at") or 0,
                "source": "runtime_db",
            },
            "active_env": active_env,
            "selected_env": selected_env,
        }

    def get_health_score_data(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "health_score",
            self._fetch_health_score_data,
            force_refresh,
        )

    def _fetch_health_score_data(self) -> Dict[str, Any]:
        environment = self._fetch_environment_data()
        task_registry = self._shared_state("task-registry-snapshot.json", {})
        learning_runtime = self._shared_state("learning-runtime-status.json", {})
        control_plane = self._shared_state("control-plane-summary.json", {})
        main_closure_purity_gate = self._shared_state(
            "main-closure-purity-gate.json", {}
        )
        task_summary = (
            (task_registry or {}).get("summary")
            if isinstance(task_registry, dict)
            else {}
        )
        learning_freshness = 0
        if isinstance(learning_runtime, dict):
            learning_freshness = int(
                learning_runtime.get("reflection_freshness")
                or learning_runtime.get("learning_freshness")
                or 0
            )
        error_categories: list[str] = []
        if isinstance(control_plane, dict):
            actions = control_plane.get("actions") or {}
            tasks = control_plane.get("tasks") or {}
            if int(actions.get("pending") or 0) > 0:
                error_categories.append("pending_actions")
            if int(tasks.get("protocol_violations") or 0) > 0:
                error_categories.append("protocol_violations")
            if int(tasks.get("blocked") or 0) > 0:
                error_categories.append("blocked_tasks")
        if isinstance(main_closure_purity_gate, dict) and not bool(
            main_closure_purity_gate.get("ok", True)
        ):
            error_categories.append("main_closure_purity_gate_failed")
        return {
            "environment": {
                "gateway_healthy": bool(environment.get("gateway_healthy")),
            },
            "metrics": self.get_metrics(),
            "tasks": {
                "blocked_count": int((task_summary or {}).get("blocked", 0) or 0),
            },
            "learning": {
                "is_fresh": bool(learning_freshness == 0 or learning_freshness < 86400),
            },
            "errors": {
                "categories": error_categories,
                "count": len(error_categories),
            },
        }

    def get_metrics(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "metrics",
            self._fetch_metrics_data,
            force_refresh,
        )

    def _fetch_metrics_data(self) -> Dict[str, Any]:
        cpu_percent = 0.0
        memory_percent = 0.0
        memory_used_gb = 0.0
        memory_total_gb = 0.0
        runtime_health = self._shared_state_fresh(
            "runtime-health.json", {}, max_age_seconds=20
        )
        runtime_metrics = (
            (runtime_health or {}).get("metrics")
            if isinstance(runtime_health, dict)
            else {}
        )
        if isinstance(runtime_metrics, dict) and runtime_metrics:
            cpu_percent = _coerce_number(runtime_metrics.get("cpu"))
            memory_used_gb = _coerce_number(runtime_metrics.get("mem_used"))
            memory_total_gb = _coerce_number(runtime_metrics.get("mem_total"))
            if memory_total_gb > 0:
                memory_percent = (memory_used_gb / memory_total_gb) * 100
        if (
            not runtime_metrics
            or (cpu_percent <= 0 and memory_used_gb <= 0)
            or memory_total_gb <= 0
        ):
            try:
                import psutil

                cpu_percent = psutil.cpu_percent(interval=None)
                memory = psutil.virtual_memory()
                memory_percent = memory.percent
                memory_used_gb = memory.used / (1024**3)
                memory_total_gb = memory.total / (1024**3)
            except Exception:
                try:
                    legacy_metrics = _legacy_dashboard().get_system_metrics()
                    cpu_percent = _coerce_number(legacy_metrics.get("cpu"))
                    memory_used_gb = _coerce_number(legacy_metrics.get("mem_used"))
                    memory_total_gb = _coerce_number(legacy_metrics.get("mem_total"))
                    if memory_total_gb > 0:
                        memory_percent = (memory_used_gb / memory_total_gb) * 100
                except Exception:
                    pass

        gateway_pid = None
        gateway_healthy = (
            bool(runtime_health.get("gateway_healthy"))
            if isinstance(runtime_health, dict)
            else False
        )
        sessions = 0
        guardian_pid = None
        try:
            context = self._load_runtime_context()
            legacy = context["legacy"]
            selected_env = context["selected_env"]
            gateway = legacy.get_gateway_process_for_env(selected_env)
            guardian = legacy.get_guardian_process_info()
            gateway_pid = (gateway or {}).get("pid")
            guardian_pid = (guardian or {}).get("pid")
            if not gateway_healthy:
                gateway_healthy = bool(
                    legacy.check_gateway_health_for_env(selected_env)
                )
            task_registry = self._shared_state("task-registry-snapshot.json", {})
            summary = (
                (task_registry or {}).get("summary")
                if isinstance(task_registry, dict)
                else {}
            )
            sessions = int(summary.get("running", 0) or 0) + int(
                summary.get("background", 0) or 0
            )
        except Exception:
            pass

        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "memory_used_gb": memory_used_gb,
            "memory_total_gb": memory_total_gb,
            "sessions": sessions,
            "pid": gateway_pid,
            "guardian_pid": guardian_pid,
            "gateway_healthy": gateway_healthy,
            "timestamp": datetime.now().isoformat(),
        }

    def get_environment(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "environment",
            self._fetch_environment_data,
            force_refresh,
        )

    def _normalize_environment(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "active": bool(item.get("active")),
            "running": bool(item.get("running")),
            "healthy": bool(item.get("healthy")),
            "port": item.get("port"),
            "code_path": item.get("code"),
            "state_path": item.get("home"),
            "git_head": item.get("git_head") or "",
            "target_head": item.get("target_head") or "",
            "pid": item.get("listener_pid"),
            "listener_pid": item.get("listener_pid"),
            "token": item.get("token_prefix") or "",
            "token_prefix": item.get("token_prefix") or "",
            "control_ui_ready": bool(item.get("control_ui_ready")),
            "dashboard_url": item.get("dashboard_url") or "",
            "dashboard_open_link": item.get("dashboard_open_link") or "",
            "auto_update_enabled": bool(item.get("auto_update_enabled")),
            "auto_update_expected": bool(item.get("auto_update_expected")),
            "auto_update_installed": bool(item.get("auto_update_installed")),
            "auto_update_drift": bool(item.get("auto_update_drift")),
            "update_hour": item.get("update_hour"),
            "update_minute": item.get("update_minute"),
            "channel_readiness": item.get("channel_readiness") or {},
        }

    def _fetch_environment_data(self) -> Dict[str, Any]:
        try:
            context = self._load_runtime_context()
            legacy = context["legacy"]
            active_env = context["active_env"]
            selected_env = context["selected_env"]
            runtime_binding = context.get("binding") or {}
            runtime_health = self._shared_state_fresh(
                "runtime-health.json", {}, max_age_seconds=20
            )
            bootstrap_status = self._shared_state("bootstrap-status.json", {})
            watcher_summary = self._shared_state("watcher-summary.json", {})
            restart_runtime_status = self._shared_state(
                "restart-runtime-status.json", {}
            )
            openclaw_version = self._shared_state("openclaw-version.json", {})
            recovery_profile = self._shared_state("openclaw-recovery-profile.json", {})
            watchdog_recovery_status = self._shared_state(
                "watchdog-recovery-status.json", {}
            )
            watchdog_recovery_incidents = self._shared_state(
                "watchdog-recovery-incidents.json", []
            )
            watchdog_recovery_hints = self._shared_state(
                "watchdog-recovery-hints.json", []
            )

            if (
                not isinstance(bootstrap_status, dict)
                or str(bootstrap_status.get("env_id") or active_env) != active_env
            ):
                bootstrap_status = (
                    _runtime_value(legacy, f"bootstrap_status:{active_env}", {}) or {}
                )

            context_readiness = {}
            if isinstance(bootstrap_status, dict):
                context_readiness = bootstrap_status.get("context_readiness") or {}
            if not context_readiness:
                baseline = self._shared_state("context-lifecycle-baseline.json", {})
                if isinstance(baseline, dict):
                    bootstrap_snapshot = baseline.get("bootstrap_status") or {}
                    if isinstance(bootstrap_snapshot, dict):
                        context_readiness = (
                            bootstrap_snapshot.get("context_readiness") or {}
                        )

            gateway_process = None
            listener_pid = None
            try:
                gateway_process = legacy.get_gateway_process_for_env(selected_env)
                listener_pid = (gateway_process or {}).get("pid")
            except Exception:
                gateway_process = None

            runtime_matches = (
                isinstance(runtime_health, dict)
                and str(runtime_health.get("env_id") or active_env) == active_env
            )
            running = (
                bool(runtime_health.get("gateway_running"))
                if runtime_matches
                else False
            )
            if listener_pid:
                running = True

            # 当前实例一旦有 listener，就优先信实时 /health 探测，而不是旧 shared-state。
            healthy = False
            if listener_pid or running:
                try:
                    healthy = bool(legacy.check_gateway_health_for_env(selected_env))
                except Exception:
                    healthy = (
                        bool(runtime_health.get("gateway_healthy"))
                        if runtime_matches
                        else False
                    )

            try:
                control_ui_ready = bool(legacy.env_has_control_ui_assets(selected_env))
            except Exception:
                control_ui_ready = False

            channel_readiness = (
                _runtime_value(legacy, f"channel_readiness:{active_env}", {}) or {}
            )
            expected_config_path = str(
                Path(str(selected_env.get("home") or "")) / "openclaw.json"
            )
            if not isinstance(channel_readiness, dict):
                channel_readiness = {}
            checked_at = (
                int(channel_readiness.get("checked_at") or 0)
                if isinstance(channel_readiness, dict)
                else 0
            )
            if (
                not channel_readiness
                or str(channel_readiness.get("config_path") or "")
                != expected_config_path
                or checked_at <= 0
                or (int(time.time()) - checked_at) > 120
            ):
                channel_readiness = {}

            selected = self._normalize_environment(
                {
                    "id": active_env,
                    "name": selected_env.get("name") or "OpenClaw",
                    "description": selected_env.get("description")
                    or "当前唯一运行环境",
                    "active": True,
                    "running": running,
                    "healthy": healthy,
                    "port": selected_env.get("port"),
                    "code": str(selected_env.get("code") or ""),
                    "home": str(selected_env.get("home") or ""),
                    "git_head": str(selected_env.get("git_head") or ""),
                    "target_head": str(selected_env.get("git_head") or ""),
                    "listener_pid": listener_pid,
                    "token_prefix": "",
                    "control_ui_ready": control_ui_ready,
                    "dashboard_url": legacy.env_dashboard_url(selected_env)
                    if hasattr(legacy, "env_dashboard_url")
                    else "",
                    "dashboard_open_link": legacy.env_open_link(selected_env)
                    if hasattr(legacy, "env_open_link") and running and control_ui_ready
                    else "",
                    "auto_update_enabled": False,
                    "auto_update_expected": False,
                    "auto_update_installed": False,
                    "auto_update_drift": False,
                    "update_hour": None,
                    "update_minute": None,
                    "channel_readiness": channel_readiness,
                }
            )
            environments = [selected]

            recent_binding_events = _runtime_value(legacy, "binding_audit_events", [])
            if not isinstance(recent_binding_events, list):
                recent_binding_events = []
            recent_binding_events = [
                item
                for item in recent_binding_events
                if str((item or {}).get("env_id") or "primary") == "primary"
            ]
            binding_audit = {
                "active_env": "primary",
                "switch_state": runtime_binding.get("switch_state") or "committed",
                "updated_at": runtime_binding.get("updated_at") or 0,
                "source": "runtime_db",
                "recent_events": recent_binding_events,
            }
            environment_integrity = []
            return {
                "gateway_healthy": bool(healthy),
                "active_environment": active_env,
                "environments": environments,
                "active": selected,
                "code_path": selected.get("code_path"),
                "state_path": selected.get("state_path"),
                "bootstrap_status": bootstrap_status,
                "context_readiness": context_readiness,
                "config_drift": {
                    "mode": "merge_missing",
                    "applied": (bootstrap_status.get("config_merge") or {}).get(
                        "applied"
                    )
                    or [],
                    "preserved": (bootstrap_status.get("config_merge") or {}).get(
                        "preserved"
                    )
                    or [],
                    "status": context_readiness.get("status") or "unknown",
                },
                "watcher_summary": watcher_summary,
                "restart_runtime_status": restart_runtime_status,
                "version_info": openclaw_version,
                "recovery_profile": recovery_profile,
                "watchdog_recovery_status": watchdog_recovery_status,
                "watchdog_recovery_incidents": watchdog_recovery_incidents,
                "watchdog_recovery_hints": watchdog_recovery_hints,
                "binding_audit": binding_audit,
                "environment_integrity": environment_integrity,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {
                "gateway_healthy": False,
                "active_environment": "unknown",
                "environments": [],
                "active": {},
                "code_path": None,
                "state_path": None,
                "bootstrap_status": {},
                "context_readiness": {"status": "not_ready", "headline": str(exc)},
                "config_drift": {
                    "mode": "merge_missing",
                    "applied": [],
                    "preserved": [],
                    "status": "error",
                },
                "watcher_summary": {},
                "restart_runtime_status": {},
                "version_info": {},
                "recovery_profile": {},
                "watchdog_recovery_status": {},
                "watchdog_recovery_incidents": [],
                "watchdog_recovery_hints": [],
                "binding_audit": {},
                "environment_integrity": [],
                "error": str(exc),
                "timestamp": datetime.now().isoformat(),
            }

    def get_tasks(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "tasks",
            self._fetch_task_data,
            force_refresh,
        )

    def _normalize_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        control = task.get("control") or {}
        if not control and any(
            key in task
            for key in (
                "control_state",
                "approved_summary",
                "next_action",
                "claim_level",
            )
        ):
            control = {
                "control_state": task.get("control_state") or "",
                "approved_summary": task.get("approved_summary") or "",
                "next_action": task.get("next_action") or "",
                "next_actor": task.get("next_actor") or "",
                "claim_level": task.get("claim_level") or "",
                "evidence_level": task.get("evidence_level") or "",
                "evidence_summary": task.get("evidence_summary") or "",
                "missing_receipts": list(task.get("missing_receipts") or []),
                "phase_statuses": list(task.get("phase_statuses") or []),
                "control_action": task.get("control_action"),
                "protocol": task.get("protocol") or {},
                "pipeline_recovery": task.get("pipeline_recovery") or {},
            }
        latest_receipt = (
            task.get("latest_receipt")
            or (control.get("latest_receipt") if isinstance(control, dict) else {})
            or {}
        )
        task_id = str(task.get("task_id") or "")
        root_task = task.get("current_root_task") or task.get("root_task") or {}
        current_workflow = task.get("current_workflow_run") or {}
        current_finalizer = task.get("current_finalizer") or {}
        current_delivery = task.get("current_delivery_attempt") or {}
        current_followups = list(task.get("current_followups") or [])
        question = str(
            task.get("question")
            or task.get("last_user_message")
            or task_id
            or "未命名任务"
        ).strip()
        raw_status = str(task.get("status") or "unknown")
        status = raw_status
        control_state = str(control.get("control_state") or "")
        workflow_state = str(
            current_workflow.get("current_state")
            or root_task.get("workflow_state")
            or ""
        )
        blocked_reason = str(task.get("blocked_reason") or "")
        if control_state.startswith("blocked") or blocked_reason:
            status = "blocked"
        elif workflow_state in {
            "blocked",
            "delivery_failed",
            "dlq",
            "failed",
            "cancelled",
        }:
            status = "blocked"
        elif workflow_state == "delivered":
            status = "completed"
        elif workflow_state in {
            "accepted",
            "routed",
            "queued",
            "started",
            "completed",
            "delivery_pending",
            "ambiguous_success",
        }:
            status = "running"
        elif raw_status == "background":
            status = "running"
        current_stage = str(task.get("current_stage") or "").strip()
        if workflow_state and not current_stage:
            current_stage = workflow_state
        truth_level = "core_projection" if root_task or current_workflow else "derived"
        return {
            "id": task_id,
            "task_id": task_id,
            "status": status,
            "raw_status": raw_status,
            "name": question,
            "question": question,
            "created": _iso_from_timestamp(task.get("created_at")),
            "updated": _iso_from_timestamp(
                task.get("updated_at") or task.get("last_progress_at")
            ),
            "completed": _iso_from_timestamp(task.get("completed_at")),
            "backgrounded": _iso_from_timestamp(task.get("backgrounded_at")),
            "agent": latest_receipt.get("agent") or control.get("next_actor") or "-",
            "env_id": task.get("env_id") or "",
            "channel": task.get("channel") or "",
            "root_task": {
                "root_task_id": root_task.get("root_task_id") or "",
                "user_goal_summary": root_task.get("user_goal_summary") or "",
                "workflow_state": root_task.get("workflow_state") or "",
                "foreground": bool(root_task.get("foreground")),
                "finalization_state": root_task.get("finalization_state") or "",
                "final_status": root_task.get("final_status") or "",
                "delivery_state": root_task.get("delivery_state") or "",
                "delivery_confirmation_level": root_task.get(
                    "delivery_confirmation_level"
                )
                or "",
                "open_followup_count": int(root_task.get("open_followup_count") or 0),
                "followup_types": list(root_task.get("followup_types") or []),
            },
            "current_workflow_run": {
                "workflow_run_id": current_workflow.get("workflow_run_id") or "",
                "current_state": current_workflow.get("current_state") or "",
                "state_reason": current_workflow.get("state_reason") or "",
                "terminal_at": current_workflow.get("terminal_at") or 0,
            },
            "current_finalizer": {
                "finalization_id": current_finalizer.get("finalization_id") or "",
                "decision_state": current_finalizer.get("decision_state") or "",
                "final_status": current_finalizer.get("final_status") or "",
                "delivery_state": current_finalizer.get("delivery_state") or "",
                "user_visible_summary": current_finalizer.get("user_visible_summary")
                or "",
            },
            "current_delivery_attempt": {
                "delivery_attempt_id": current_delivery.get("delivery_attempt_id")
                or "",
                "current_state": current_delivery.get("current_state") or "",
                "confirmation_level": current_delivery.get("confirmation_level") or "",
                "channel": current_delivery.get("channel") or "",
                "target": current_delivery.get("target") or "",
            },
            "current_followups": [
                {
                    "followup_id": item.get("followup_id") or "",
                    "followup_type": item.get("followup_type") or "",
                    "trigger_reason": item.get("trigger_reason") or "",
                    "current_state": item.get("current_state") or "",
                    "suggested_action": item.get("suggested_action") or "",
                }
                for item in current_followups[:10]
            ],
            "followup_count": len(current_followups),
            "current_stage": current_stage,
            "blocked_reason": task.get("blocked_reason") or "",
            "last_progress_label": task.get("last_progress_label") or "-",
            "latest_receipt": {
                "agent": latest_receipt.get("agent") or "",
                "phase": latest_receipt.get("phase") or "",
                "action": latest_receipt.get("action") or "",
            },
            "control": {
                "control_state": control.get("control_state") or "",
                "approved_summary": control.get("approved_summary") or "",
                "next_action": control.get("next_action") or "",
                "next_actor": control.get("next_actor") or "",
                "claim_level": control.get("claim_level") or "",
                "user_visible_progress": task.get("user_visible_progress")
                or control.get("user_visible_progress")
                or "",
                "followup_stage": task.get("followup_stage")
                or control.get("followup_stage")
                or "",
                "heartbeat_age_seconds": task.get("heartbeat_age_seconds")
                if task.get("heartbeat_age_seconds") is not None
                else control.get("heartbeat_age_seconds"),
                "heartbeat_ok": task.get("heartbeat_ok")
                if task.get("heartbeat_ok") is not None
                else control.get("heartbeat_ok"),
                "timing": task.get("timing") or control.get("timing") or {},
            },
            "session_key": task.get("session_key") or "",
            "truth_level": truth_level,
        }

    def _fetch_task_data(self) -> Dict[str, Any]:
        try:
            current_facts = _read_json_file(
                PROJECT_ROOT / "data" / "current-task-facts.json", {}
            )
            payload = self._shared_state("task-registry-snapshot.json", {})
            main_closure = self._shared_state("main-closure-runtime-status.json", {})
            has_current_facts = isinstance(current_facts, dict) and isinstance(
                current_facts.get("current_task"), dict
            )
            if not isinstance(payload, dict) or (
                not payload.get("summary") and not has_current_facts
            ):
                payload = _legacy_dashboard().get_task_registry_payload(limit=120)
            closure_roots = (
                list((main_closure or {}).get("roots") or [])
                if isinstance(main_closure, dict)
                else []
            )
            closure_by_session = {
                str(item.get("session_key") or ""): item
                for item in closure_roots
                if item.get("session_key")
            }
            raw_tasks = list(payload.get("tasks") or [])
            for item in raw_tasks:
                session_key = str(item.get("session_key") or "")
                if session_key and session_key in closure_by_session:
                    item["root_task"] = closure_by_session[session_key]
            raw_tasks.sort(
                key=lambda item: int(
                    item.get("updated_at")
                    or item.get("last_progress_at")
                    or item.get("created_at")
                    or 0
                ),
                reverse=True,
            )
            tasks = [self._normalize_task(item) for item in raw_tasks[:50]]
            current = payload.get("current")
            summary = payload.get("summary") or {}
            if (not isinstance(current, dict) or not current) and isinstance(
                current_facts, dict
            ):
                current = current_facts.get("current_task") or None
            current_root = (
                current_facts.get("current_root_task")
                if isinstance(current_facts, dict)
                else None
            )
            if not current and isinstance(current_root, dict):
                current = next(
                    (
                        item
                        for item in raw_tasks
                        if str(
                            ((item.get("root_task") or {}).get("root_task_id") or "")
                        )
                        == str(current_root.get("root_task_id") or "")
                    ),
                    None,
                )
            if isinstance(current, dict) and isinstance(current_facts, dict):
                if isinstance(current_root, dict):
                    current["current_root_task"] = current_root
                for key in (
                    "current_workflow_run",
                    "current_finalizer",
                    "current_delivery_attempt",
                ):
                    value = current_facts.get(key)
                    if isinstance(value, dict):
                        current[key] = value
                followups = current_facts.get("current_followups")
                if isinstance(followups, list):
                    current["current_followups"] = followups
            return {
                "blocked_count": int(summary.get("blocked", 0) or 0),
                "total_count": int(summary.get("total", 0) or 0),
                "running_count": int(summary.get("running", 0) or 0),
                "current": self._normalize_task(current)
                if isinstance(current, dict)
                else None,
                "tasks": tasks,
                "summary": summary,
                "control_queue": list(payload.get("control_queue") or [])[:20],
                "current_root_task": current_root
                if isinstance(current_root, dict)
                else None,
                "session_resolution": payload.get("session_resolution")
                or (
                    current_facts.get("session_resolution")
                    if isinstance(current_facts, dict)
                    else {}
                )
                or {},
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {
                "blocked_count": 0,
                "total_count": 0,
                "running_count": 0,
                "current": None,
                "tasks": [],
                "summary": {},
                "control_queue": [],
                "current_root_task": None,
                "session_resolution": {},
                "error": str(exc),
                "timestamp": datetime.now().isoformat(),
            }

    def get_agents(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "agents",
            self._fetch_agents_data,
            force_refresh,
        )

    def _normalize_agent(self, item: Dict[str, Any]) -> Dict[str, Any]:
        updated_at = int(item.get("updated_at") or 0)
        status_code = str(item.get("status_code") or "idle")
        is_processing = status_code == "processing"
        is_active = bool(item.get("is_active", False))
        return {
            "id": item.get("agent_id"),
            "name": item.get("display_name") or item.get("agent_id"),
            "emoji": item.get("emoji") or "",
            "is_active": is_active,
            # Deprecated compatibility field. UI/highlight logic should use is_active.
            "is_processing": is_processing,
            "last_activity": _iso_from_timestamp(updated_at),
            "last_activity_label": item.get("updated_label") or "-",
            "status_code": status_code,
            "state_label": item.get("state_label") or "待机",
            "state_reason": item.get("state_reason") or "",
            "detail": item.get("detail") or "",
            "task_hint": item.get("task_hint") or "",
            "task_title": item.get("task_title") or item.get("task_hint") or "",
            "sessions": int(item.get("sessions") or 0),
            "recent_sessions": item.get("recent_sessions") or [],
            "activity_source": item.get("activity_source") or "session",
            "activity_excerpt": item.get("activity_excerpt") or "",
        }

    def _load_agent_log_activity(
        self, env_home: Path, *, lookback_seconds: int
    ) -> Dict[str, Dict[str, Any]]:
        log_path = env_home / "logs" / "gateway.log"
        if not log_path.exists():
            return {}
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[
                -1800:
            ]
        except Exception:
            return {}
        cutoff = int(time.time()) - max(60, lookback_seconds)
        signals: Dict[str, Dict[str, Any]] = {}
        for line in lines:
            agent_id = _extract_agent_id_from_log_line(line)
            if not agent_id:
                continue
            ts = _extract_log_timestamp(line)
            if ts and ts < cutoff:
                continue
            current = signals.get(agent_id)
            if current and ts and current.get("updated_at", 0) > ts:
                continue
            detail = _clean_log_detail(line)
            status_code, state_reason = _classify_log_signal(detail)
            signals[agent_id] = {
                "updated_at": ts or int(log_path.stat().st_mtime),
                "updated_label": datetime.fromtimestamp(
                    ts or int(log_path.stat().st_mtime)
                ).strftime("%m-%d %H:%M:%S"),
                "status_code": status_code,
                "state_label": self._status_label(status_code),
                "state_reason": state_reason,
                "detail": detail,
                "activity_source": "gateway_log",
                "activity_excerpt": detail,
            }
        return signals

    def _status_label(self, status_code: str) -> str:
        return {
            "processing": "正在处理",
            "waiting_downstream": "等待下游",
            "silent_waiting": "静默等待",
            "blocked": "处理受阻",
            "completed": "已完成",
            "observed": "有活动",
            "idle": "待机",
        }.get(status_code, "待机")

    def _summarize_session_entries(
        self, entries: list[dict], session_path: Path
    ) -> Dict[str, Any]:
        title = ""
        context_lines: list[str] = []
        status_code = "idle"
        state_reason = "最近没有明确的执行信号"
        detail = "暂无最近会话"
        task_hint = ""
        for entry in entries:
            message = entry.get("message") or {}
            texts = _extract_text_items(message.get("content") or [])
            for text in texts:
                cleaned = _strip_timestamp_prefix(text)
                if cleaned:
                    context_lines.append(_shorten(cleaned, 200))
                if not title:
                    candidate = _extract_task_hint_from_text(cleaned)
                    if candidate and not _is_placeholder_task_text(candidate):
                        title = candidate
                if not task_hint:
                    candidate = _extract_task_hint_from_text(cleaned)
                    if candidate and not _is_placeholder_task_text(candidate):
                        task_hint = candidate
        recent_context = context_lines[-6:]
        for entry in reversed(entries):
            message = entry.get("message") or {}
            role = str(message.get("role") or "")
            content = message.get("content") or []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "toolCall":
                    continue
                name = str(item.get("name") or "")
                args = item.get("arguments") or {}
                if name == "exec":
                    detail = "正在执行命令、检查或代码修改"
                    status_code = "processing"
                    state_reason = "最近一条会话记录显示代理正在执行本地动作"
                    return {
                        "status_code": status_code,
                        "state_label": self._status_label(status_code),
                        "state_reason": state_reason,
                        "detail": detail,
                        "task_title": title,
                        "task_hint": task_hint,
                        "recent_context": recent_context,
                        "context_preview": " | ".join(recent_context[-3:]),
                        "has_full_context": bool(recent_context),
                    }
                if name == "sessions_spawn":
                    downstream = str(args.get("agentId") or "").strip()
                    label = _shorten(str(args.get("label") or ""), 80)
                    suffix = f"：{label}" if label else ""
                    detail = (
                        f"已把任务派发给下游代理 {downstream}{suffix}"
                        if downstream
                        else "已把任务继续派发给下游代理"
                    )
                    status_code = "waiting_downstream"
                    state_reason = "当前代理已经把工作下发，正在等待下游回执"
                    return {
                        "status_code": status_code,
                        "state_label": self._status_label(status_code),
                        "state_reason": state_reason,
                        "detail": detail,
                        "task_title": title,
                        "task_hint": task_hint,
                        "recent_context": recent_context,
                        "context_preview": " | ".join(recent_context[-3:]),
                        "has_full_context": bool(recent_context),
                    }
                if name == "sessions_send":
                    detail = "正在向上游回传进度或结构化回执"
                    status_code = "processing"
                    state_reason = "当前代理正在回传阶段结果"
                    return {
                        "status_code": status_code,
                        "state_label": self._status_label(status_code),
                        "state_reason": state_reason,
                        "detail": detail,
                        "task_title": title,
                        "task_hint": task_hint,
                        "recent_context": recent_context,
                        "context_preview": " | ".join(recent_context[-3:]),
                        "has_full_context": bool(recent_context),
                    }
            texts = _extract_text_items(content)
            if texts:
                last_text = _strip_timestamp_prefix(texts[-1])
                if "PIPELINE_RECEIPT:" in last_text:
                    lowered = last_text.lower()
                    if "action=blocked" in lowered:
                        status_code = "blocked"
                        detail = "最近结构化回执显示任务被阻塞"
                        state_reason = "当前代理已明确报告 blocked"
                    elif "action=completed" in lowered:
                        status_code = "completed"
                        detail = "最近结构化回执显示当前阶段已完成"
                        state_reason = "当前代理已明确报告 completed"
                    elif "action=started" in lowered:
                        status_code = "processing"
                        detail = "最近结构化回执显示当前阶段已经开始处理"
                        state_reason = "当前代理已明确报告 started"
                    else:
                        status_code = "observed"
                        detail = _shorten(last_text, 160)
                        state_reason = "最近会话记录包含结构化阶段回执"
                    break
                if last_text == "ANNOUNCE_SKIP":
                    status_code = "waiting_downstream"
                    detail = "当前阶段已继续下发，等待后续代理回执"
                    state_reason = "当前代理已把任务继续交给下游处理"
                    break
                if last_text == "NO_REPLY":
                    status_code = "silent_waiting"
                    detail = "当前代理收到内部更新，但当前无需立刻对外回复"
                    state_reason = "当前代理在等待内部链路继续推进"
                    break
                if role == "assistant":
                    status_code = "processing"
                    detail = _shorten(last_text, 160) or "最近会话显示代理正在处理"
                    state_reason = "最近一条可见回复来自代理本身"
                    break
                if role == "user":
                    status_code = "processing"
                    detail = _shorten(last_text, 160) or "最近收到新的任务输入"
                    state_reason = "最近会话显示代理刚收到新任务或补充要求"
                    break
        return {
            "status_code": status_code,
            "state_label": self._status_label(status_code),
            "state_reason": state_reason,
            "detail": detail,
            "task_title": title or task_hint or session_path.stem,
            "task_hint": task_hint,
            "recent_context": recent_context,
            "context_preview": " | ".join(recent_context[-3:]),
            "has_full_context": bool(recent_context),
        }

    def _load_agent_sessions(
        self,
        env_home: Path,
        legacy: Any,
        catalog: Dict[str, Any],
        *,
        recent_limit: int = 5,
    ) -> Dict[str, Dict[str, Any]]:
        agents_dir = env_home / "agents"
        payload: Dict[str, Dict[str, Any]] = {}
        known_ids = set(catalog.keys())
        if agents_dir.exists():
            known_ids.update(
                item.name for item in agents_dir.iterdir() if item.is_dir()
            )
        for agent_id in sorted(known_ids):
            sessions_dir = agents_dir / agent_id / "sessions"
            files = []
            if sessions_dir.exists():
                files = sorted(
                    [path for path in sessions_dir.glob("*.jsonl") if path.is_file()],
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            recent_files = files[:recent_limit]
            recent_sessions = []
            for path in recent_files:
                try:
                    lines = path.read_text(
                        encoding="utf-8", errors="ignore"
                    ).splitlines()[-60:]
                    entries = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(record, dict):
                            entries.append(record)
                except Exception:
                    entries = []
                summary = (
                    self._summarize_session_entries(entries, path) if entries else {}
                )
                recent_sessions.append(
                    {
                        "session_file": path.name,
                        "updated_at": int(path.stat().st_mtime),
                        "updated_label": datetime.fromtimestamp(
                            int(path.stat().st_mtime)
                        ).strftime("%m-%d %H:%M:%S"),
                        "status_code": summary.get("status_code") or "idle",
                        "state_label": summary.get("state_label") or "待机",
                        "state_reason": summary.get("state_reason") or "",
                        "detail": summary.get("detail") or "",
                        "task_hint": summary.get("task_hint") or "",
                        "task_title": summary.get("task_title") or "",
                        "recent_context": summary.get("recent_context") or [],
                        "context_preview": summary.get("context_preview") or "",
                        "has_full_context": bool(summary.get("has_full_context")),
                    }
                )
            latest_session = recent_sessions[0] if recent_sessions else {}
            payload[agent_id] = {
                "agent_id": agent_id,
                "display_name": (catalog.get(agent_id) or {}).get("name") or agent_id,
                "emoji": (catalog.get(agent_id) or {}).get("emoji") or "",
                "updated_at": latest_session.get("updated_at") or 0,
                "updated_label": latest_session.get("updated_label") or "-",
                "status_code": latest_session.get("status_code") or "idle",
                "state_label": latest_session.get("state_label") or "待机",
                "state_reason": latest_session.get("state_reason")
                or "最近没有新的代理动作",
                "detail": latest_session.get("detail") or "暂无最近会话",
                "task_hint": latest_session.get("task_hint") or "",
                "task_title": latest_session.get("task_title") or "",
                "sessions": len(files),
                "recent_sessions": recent_sessions,
                "is_active": False,
                "activity_source": "session",
                "activity_excerpt": latest_session.get("context_preview")
                or latest_session.get("detail")
                or "",
            }
        return payload

    def _fetch_agents_data(self) -> Dict[str, Any]:
        try:
            context = self._load_runtime_context()
            legacy = context["legacy"]
            selected_env = context["selected_env"]
            config = context["config"]
            env_home = Path(str(selected_env.get("home") or Path.home() / ".openclaw"))
            catalog = legacy.load_agent_catalog(selected_env)
            lookback_seconds = int(config.get("AGENT_ACTIVITY_LOOKBACK_SECONDS", 1800))
            active_window = int(
                config.get(
                    "AGENT_ACTIVITY_ACTIVE_WINDOW_SECONDS", min(lookback_seconds, 900)
                )
            )
            sessions_by_agent = self._load_agent_sessions(env_home, legacy, catalog)
            log_activity = self._load_agent_log_activity(
                env_home, lookback_seconds=lookback_seconds
            )
            now_ts = int(time.time())
            merged: list[Dict[str, Any]] = []
            for agent_id, item in sessions_by_agent.items():
                log_signal = log_activity.get(agent_id) or {}
                updated_at = max(
                    int(item.get("updated_at") or 0),
                    int(log_signal.get("updated_at") or 0),
                )
                preferred_status = str(
                    log_signal.get("status_code") or item.get("status_code") or "idle"
                )
                status_source_ts = int(log_signal.get("updated_at") or 0)
                if status_source_ts < int(item.get("updated_at") or 0):
                    preferred_status = str(item.get("status_code") or preferred_status)
                is_active = bool(
                    preferred_status == "processing"
                    and updated_at
                    and (now_ts - updated_at) <= max(60, active_window)
                )
                merged_item = {
                    **item,
                    "updated_at": updated_at,
                    "updated_label": (
                        log_signal.get("updated_label")
                        if int(log_signal.get("updated_at") or 0)
                        >= int(item.get("updated_at") or 0)
                        else item.get("updated_label")
                    )
                    or "-",
                    "status_code": preferred_status,
                    "state_label": (
                        log_signal.get("state_label")
                        if int(log_signal.get("updated_at") or 0)
                        >= int(item.get("updated_at") or 0)
                        else item.get("state_label")
                    )
                    or self._status_label(preferred_status),
                    "state_reason": (
                        log_signal.get("state_reason")
                        if int(log_signal.get("updated_at") or 0)
                        >= int(item.get("updated_at") or 0)
                        else item.get("state_reason")
                    )
                    or "",
                    "detail": log_signal.get("detail") or item.get("detail") or "",
                    "activity_source": log_signal.get("activity_source")
                    or item.get("activity_source")
                    or "session",
                    "activity_excerpt": log_signal.get("activity_excerpt")
                    or item.get("activity_excerpt")
                    or "",
                    "is_active": is_active,
                }
                merged.append(self._normalize_agent(merged_item))

            merged.sort(
                key=lambda item: (
                    0 if item.get("is_active") else 1,
                    -(
                        int(datetime.fromisoformat(item["last_activity"]).timestamp())
                        if item.get("last_activity")
                        else 0
                    ),
                    item.get("name") or item.get("id") or "",
                )
            )
            return {
                "active_count": len([item for item in merged if item.get("is_active")]),
                "recent_sessions": sum(
                    1 for item in merged if item.get("recent_sessions")
                ),
                "agents": merged,
                "active_agent_id": next(
                    (item.get("id") for item in merged if item.get("is_active")), None
                ),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {
                "active_count": 0,
                "recent_sessions": 0,
                "agents": [],
                "active_agent_id": None,
                "error": str(exc),
                "timestamp": datetime.now().isoformat(),
            }

    def get_learnings(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "learnings",
            self._fetch_learning_data,
            force_refresh,
        )

    def _normalize_learning(self, item: Dict[str, Any]) -> Dict[str, Any]:
        ts = int(
            item.get("updated_at")
            or item.get("created_at")
            or item.get("last_seen_at")
            or 0
        )
        category = str(item.get("category") or "misc")
        title = str(item.get("title") or item.get("summary") or "未命名 learning")
        description = str(item.get("detail") or item.get("action") or "")
        promoted_target = str(item.get("promoted_target") or "")
        internal_control = self._is_internal_control_learning(item)
        if internal_control:
            capability_title = "任务闭环保护规则"
            capability_summary = self._summarize_internal_control_learning(
                title, description
            )
            audience = "internal"
        else:
            capability_title = title
            capability_summary = description
            audience = "user"
        return {
            "id": item.get("id") or item.get("learning_key") or "",
            "title": title,
            "description": description,
            "status": item.get("status") or "pending",
            "category": category,
            "occurrences": int(item.get("occurrences") or 0),
            "timestamp": _iso_from_timestamp(ts),
            "promoted_target": promoted_target,
            "audience": audience,
            "capability_title": capability_title,
            "capability_summary": capability_summary,
            "internal_control": internal_control,
        }

    def _is_internal_control_learning(self, item: Dict[str, Any]) -> bool:
        category = str(item.get("category") or "")
        title = str(item.get("title") or item.get("summary") or "")
        detail = str(item.get("detail") or item.get("action") or "")
        combined = f"{title} {detail}".lower()
        if category == "control_plane":
            return True
        return any(
            marker in combined
            for marker in (
                "missing_pipeline_receipt",
                "control_followup_failed",
                "blocked_reason=",
                "control=",
                "task=",
            )
        )

    def _summarize_internal_control_learning(self, title: str, detail: str) -> str:
        lowered = detail.lower()
        if "missing_pipeline_receipt" in lowered:
            return "系统学到：当结构化回执缺失时，任务不能被视为真正完成，必须先补齐回执再允许收口。"
        if "control_followup_failed" in lowered:
            return "系统学到：当自动追问链路失败时，不能假设任务仍在稳定推进，需要显式记录异常并触发人工关注。"
        if "blocked_reason=" in lowered:
            return "系统学到：任务阻塞必须形成结构化原因，不能只靠自然语言模糊描述。"
        if title:
            return f"系统学到：{title} 已被提升为内部控制规则，用于约束后续任务闭环。"
        return "系统学到一条新的内部控制规则，用于约束后续任务闭环。"

    def _normalize_reflection(self, item: Dict[str, Any]) -> Dict[str, Any]:
        ts = int(item.get("created_at") or item.get("updated_at") or 0)
        return {
            "run_type": item.get("run_type") or "scheduled",
            "summary": item.get("summary") or {},
            "created_at": _iso_from_timestamp(ts),
        }

    def _fetch_learning_data(self) -> Dict[str, Any]:
        try:
            payload = _legacy_dashboard().get_learning_center_payload(limit=20)
            raw_items = [
                self._normalize_learning(item)
                for item in (payload.get("learnings") or [])
            ]
            reflections = [
                self._normalize_reflection(item)
                for item in (payload.get("reflections") or [])
            ]
            visible_items = [
                item for item in raw_items if not item.get("internal_control")
            ]
            internal_items = [
                item for item in raw_items if item.get("internal_control")
            ]
            promoted = [
                item for item in visible_items if item.get("status") == "promoted"
            ]
            internal_promoted = [
                item for item in internal_items if item.get("status") == "promoted"
            ]
            latest_ts = max(
                [
                    value
                    for value in [
                        *(
                            int(
                                entry.get("updated_at")
                                or entry.get("created_at")
                                or entry.get("last_seen_at")
                                or 0
                            )
                            for entry in (payload.get("learnings") or [])
                        ),
                        *(
                            int(entry.get("created_at") or 0)
                            for entry in (payload.get("reflections") or [])
                        ),
                    ]
                    if value > 0
                ]
                or [0]
            )
            is_fresh = False
            if latest_ts > 0:
                is_fresh = (
                    datetime.now() - datetime.fromtimestamp(latest_ts)
                ).total_seconds() < 86400
            return {
                "is_fresh": is_fresh,
                "last_update": _iso_from_timestamp(latest_ts),
                "summary": payload.get("summary") or {},
                "source_mode": payload.get("source_mode") or "legacy_store",
                "suggestions": payload.get("suggestions") or [],
                "items": visible_items,
                "reflections": reflections,
                "promoted": promoted,
                "internal_items": internal_items,
                "internal_promoted": internal_promoted,
                "internal_summary": {
                    "learning_count": len(internal_items),
                    "promoted_count": len(internal_promoted),
                },
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {
                "is_fresh": True,
                "last_update": None,
                "summary": {},
                "source_mode": "unknown",
                "suggestions": [],
                "items": [],
                "reflections": [],
                "promoted": [],
                "internal_items": [],
                "internal_promoted": [],
                "internal_summary": {"learning_count": 0, "promoted_count": 0},
                "error": str(exc),
                "timestamp": datetime.now().isoformat(),
            }

    def get_events(self, limit: int = 20, force_refresh: bool = False) -> list:
        return self._get_cached_or_fetch(
            f"events:{limit}",
            lambda: self._fetch_events_data(limit),
            force_refresh,
        )

    def _normalize_event(self, item: Dict[str, Any]) -> Dict[str, Any]:
        timestamp = None
        date_text = item.get("date") or ""
        time_text = item.get("time") or ""
        if date_text and time_text:
            try:
                timestamp = datetime.fromisoformat(
                    f"{date_text}T{time_text}"
                ).isoformat()
            except Exception:
                timestamp = None
        details = item.get("details") or {}
        return {
            "type": item.get("type") or "anomaly",
            "message": item.get("message") or "未命名事件",
            "details": details,
            "timestamp": timestamp,
            "truth_level": details.get("truth_level") or "derived",
        }

    def _fetch_events_data(self, limit: int = 20) -> list:
        try:
            legacy = _legacy_dashboard()
            return [
                self._normalize_event(item)
                for item in legacy.get_recent_anomalies(limit=limit, days=7)
            ]
        except Exception:
            return []

    def _fetch_error_data(self) -> Dict[str, Any]:
        events = self._fetch_events_data(limit=20)
        categories = sorted({str(item.get("type") or "unknown") for item in events})
        return {
            "categories": categories,
            "count": len(events),
            "items": events[:8],
            "timestamp": datetime.now().isoformat(),
        }

    def get_snapshots(
        self, *, limit: int = 20, offset: int = 0, force_refresh: bool = False
    ) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            f"snapshots:{limit}:{offset}",
            lambda: self._fetch_snapshot_data(limit=limit, offset=offset),
            force_refresh,
        )

    def _fetch_snapshot_data(
        self, *, limit: int = 20, offset: int = 0
    ) -> Dict[str, Any]:
        snapshots = _legacy_dashboard().list_snapshots(limit=max(limit + offset, 20))
        page = snapshots[offset : offset + limit]
        return {
            "count": len(snapshots),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(snapshots),
            "snapshots": page,
            "timestamp": datetime.now().isoformat(),
        }

    def create_snapshot(self, label: str) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        snapshot_dirs = legacy.create_config_snapshots(label)
        self.invalidate_cache()
        return {
            "count": len(snapshot_dirs),
            "snapshots": [path.name for path in snapshot_dirs],
            "timestamp": datetime.now().isoformat(),
        }

    def restore_snapshot(self, name: str) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        success, message = legacy.restore_snapshot_and_restart(name)
        self.invalidate_cache()
        return {
            "success": success,
            "message": message,
            "snapshot": name,
            "timestamp": datetime.now().isoformat(),
        }

    def switch_environment(self, env_id: str) -> Dict[str, Any]:
        if env_id != "primary":
            return {
                "success": False,
                "message": "单环境模式，仅支持 primary",
                "environment": env_id,
                "timestamp": datetime.now().isoformat(),
            }
        legacy = _legacy_dashboard()
        success, message = legacy.switch_openclaw_environment(env_id)
        self.invalidate_cache()
        return {
            "success": success,
            "message": message,
            "environment": env_id,
            "timestamp": datetime.now().isoformat(),
        }

    def restart_environment(self) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        success, message, old_pid, new_pid, env_id = (
            legacy.restart_active_openclaw_environment()
        )
        self.invalidate_cache()
        return {
            "success": success,
            "message": message,
            "environment": env_id,
            "old_pid": old_pid,
            "new_pid": new_pid,
            "timestamp": datetime.now().isoformat(),
        }

    def emergency_recover(self) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        config = legacy.load_config()
        if not config.get("ENABLE_SNAPSHOT_RECOVERY", True):
            return {
                "success": False,
                "message": "当前已禁用 snapshot recovery。请先开启 ENABLE_SNAPSHOT_RECOVERY=true。",
                "timestamp": datetime.now().isoformat(),
            }
        snapshot_dir = legacy.SNAPSHOTS.restore_latest_snapshot()
        if snapshot_dir is None:
            versions = legacy.load_versions()
            known_good = dict(versions.get("known_good") or {})
            target = str(known_good.get("describe") or known_good.get("commit") or "")
            return {
                "success": False,
                "message": "没有可恢复的配置快照",
                "rollback_guidance": {
                    "config_snapshot_first": True,
                    "code_rollback_manual": True,
                    "target": target,
                },
                "timestamp": datetime.now().isoformat(),
            }
        success, message = legacy.restore_snapshot_and_restart(snapshot_dir.name)
        versions = legacy.load_versions()
        known_good = dict(versions.get("known_good") or {})
        target = str(known_good.get("describe") or known_good.get("commit") or "")
        self.invalidate_cache()
        return {
            "success": success,
            "message": message if success else f"恢复失败: {message}",
            "snapshot": snapshot_dir.name,
            "rollback_guidance": {
                "config_snapshot_first": True,
                "code_rollback_manual": True,
                "target": target,
            },
            "timestamp": datetime.now().isoformat(),
        }

    def invalidate_cache(self, key: Optional[str] = None):
        if key:
            self.cache.invalidate(key)
        else:
            self.cache.invalidate_all()


_collector = None


def get_collector() -> DataCollector:
    global _collector
    if _collector is None:
        _collector = DataCollector()
    return _collector
