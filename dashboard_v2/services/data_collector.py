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
import sys


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


class DataCollector:
    """数据收集器"""

    REFRESH_INTERVALS = {
        "health_score": 5,
        "metrics": 5,
        "events": 5,
        "environment": 10,
        "agents": 30,
        "tasks": -1,
        "learnings": 60,
        "config": -1,
        "snapshots": -1,
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

    def _load_runtime_context(self) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        config = legacy.load_config()
        binding = {}
        try:
            binding = legacy.active_binding(config)
        except Exception:
            binding = {}
        active_env = str(
            binding.get("active_env")
            or config.get("ACTIVE_OPENCLAW_ENV")
            or "primary"
        )
        if active_env not in {"primary", "official"}:
            active_env = "primary"
        selected_env = legacy.env_spec(active_env, config)
        task_registry = legacy.get_task_registry_payload(limit=20)
        return {
            "legacy": legacy,
            "config": config,
            "binding": binding,
            "active_env": active_env,
            "selected_env": selected_env,
            "task_registry": task_registry,
        }

    def get_health_score_data(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "health_score",
            self._fetch_health_score_data,
            force_refresh,
        )

    def _fetch_health_score_data(self) -> Dict[str, Any]:
        return {
            "environment": self._fetch_environment_data(),
            "metrics": self._fetch_metrics_data(),
            "tasks": self._fetch_task_data(),
            "learning": self._fetch_learning_data(),
            "errors": self._fetch_error_data(),
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
        try:
            import psutil

            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            memory_percent = memory.percent
            memory_used_gb = memory.used / (1024 ** 3)
            memory_total_gb = memory.total / (1024 ** 3)
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
        gateway_healthy = False
        sessions = 0
        guardian_pid = None
        try:
            context = self._load_runtime_context()
            legacy = context["legacy"]
            selected_env = context["selected_env"]
            task_registry = context["task_registry"]
            gateway = legacy.get_gateway_process_for_env(selected_env)
            guardian = legacy.get_guardian_process_info()
            gateway_pid = (gateway or {}).get("pid")
            guardian_pid = (guardian or {}).get("pid")
            gateway_healthy = bool(legacy.check_gateway_health_for_env(selected_env))
            summary = task_registry.get("summary") or {}
            sessions = int(summary.get("running", 0) or 0) + int(summary.get("background", 0) or 0)
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
            "update_hour": item.get("update_hour"),
            "update_minute": item.get("update_minute"),
        }

    def _fetch_environment_data(self) -> Dict[str, Any]:
        try:
            context = self._load_runtime_context()
            legacy = context["legacy"]
            config = context["config"]
            active_env = context["active_env"]
            selected_env = context["selected_env"]
            task_registry = context["task_registry"]

            raw_environments = legacy.list_openclaw_environments(config)
            environments = [self._normalize_environment(item) for item in raw_environments]
            for item in environments:
                item["active"] = item.get("id") == active_env
            selected = next((item for item in environments if item.get("id") == active_env), {})
            bootstrap_status = legacy.build_bootstrap_status(config)
            context_readiness = legacy.build_context_lifecycle_readiness(config)
            watcher_summary = self._shared_state("watcher-summary.json", {})
            restart_runtime_status = self._shared_state("restart-runtime-status.json", {})
            binding_audit = self._shared_state(
                "active-binding.json",
                {
                    "active_env": active_env,
                    "switch_state": (context.get("binding") or {}).get("switch_state") or "committed",
                    "updated_at": (context.get("binding") or {}).get("updated_at") or 0,
                },
            )
            if not isinstance(binding_audit, dict):
                binding_audit = {}
            recent_binding_events = self._shared_state("binding-audit-events.json", [])
            if not isinstance(recent_binding_events, list):
                recent_binding_events = []
            binding_audit = {
                "active_env": binding_audit.get("active_env") or active_env,
                "switch_state": binding_audit.get("switch_state") or (context.get("binding") or {}).get("switch_state") or "committed",
                "updated_at": binding_audit.get("updated_at") or (context.get("binding") or {}).get("updated_at") or 0,
                "recent_events": recent_binding_events,
            }
            environment_integrity = []
            detect_integrity = getattr(legacy, "detect_environment_inconsistencies", None)
            if callable(detect_integrity):
                environment_integrity = detect_integrity(raw_environments, active_env)
            promotion_summary = legacy.build_environment_promotion_summary(raw_environments, task_registry)

            return {
                "gateway_healthy": bool(legacy.check_gateway_health_for_env(selected_env)),
                "active_environment": active_env,
                "environments": environments,
                "active": selected,
                "code_path": selected.get("code_path"),
                "state_path": selected.get("state_path"),
                "bootstrap_status": bootstrap_status,
                "context_readiness": context_readiness,
                "config_drift": {
                    "mode": "merge_missing",
                    "applied": (bootstrap_status.get("config_merge") or {}).get("applied") or [],
                    "preserved": (bootstrap_status.get("config_merge") or {}).get("preserved") or [],
                    "status": context_readiness.get("status") or "unknown",
                },
                "watcher_summary": watcher_summary,
                "restart_runtime_status": restart_runtime_status,
                "binding_audit": binding_audit,
                "environment_integrity": environment_integrity,
                "promotion_summary": promotion_summary,
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
                "config_drift": {"mode": "merge_missing", "applied": [], "preserved": [], "status": "error"},
                "watcher_summary": {},
                "restart_runtime_status": {},
                "binding_audit": {},
                "environment_integrity": [],
                "promotion_summary": {},
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
        latest_receipt = task.get("latest_receipt") or {}
        task_id = str(task.get("task_id") or "")
        question = str(task.get("question") or task.get("last_user_message") or task_id or "未命名任务").strip()
        raw_status = str(task.get("status") or "unknown")
        status = raw_status
        control_state = str(control.get("control_state") or "")
        blocked_reason = str(task.get("blocked_reason") or "")
        if control_state.startswith("blocked") or blocked_reason:
            status = "blocked"
        elif raw_status == "background":
            status = "running"
        return {
            "id": task_id,
            "task_id": task_id,
            "status": status,
            "raw_status": raw_status,
            "name": question,
            "question": question,
            "created": _iso_from_timestamp(task.get("created_at")),
            "updated": _iso_from_timestamp(task.get("updated_at") or task.get("last_progress_at")),
            "completed": _iso_from_timestamp(task.get("completed_at")),
            "backgrounded": _iso_from_timestamp(task.get("backgrounded_at")),
            "agent": latest_receipt.get("agent") or control.get("next_actor") or "-",
            "env_id": task.get("env_id") or "",
            "channel": task.get("channel") or "",
            "current_stage": task.get("current_stage") or "",
            "blocked_reason": task.get("blocked_reason") or "",
            "last_progress_label": task.get("last_progress_label") or "-",
            "latest_receipt": latest_receipt,
            "control": control,
            "control_actions": task.get("control_actions") or [],
            "session_key": task.get("session_key") or "",
            "truth_level": "derived",
        }

    def _fetch_task_data(self) -> Dict[str, Any]:
        try:
            payload = _legacy_dashboard().get_task_registry_payload(limit=200)
            tasks = [self._normalize_task(item) for item in payload.get("tasks") or []]
            current = payload.get("current")
            summary = payload.get("summary") or {}
            return {
                "blocked_count": int(summary.get("blocked", 0) or 0),
                "total_count": int(summary.get("total", 0) or 0),
                "running_count": int(summary.get("running", 0) or 0),
                "current": self._normalize_task(current) if isinstance(current, dict) else None,
                "tasks": tasks,
                "summary": summary,
                "control_queue": payload.get("control_queue") or [],
                "session_resolution": payload.get("session_resolution") or {},
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
        return {
            "id": item.get("agent_id"),
            "name": item.get("display_name") or item.get("agent_id"),
            "emoji": item.get("emoji") or "",
            "is_active": True,
            "last_activity": _iso_from_timestamp(updated_at),
            "last_activity_label": item.get("updated_label") or "-",
            "state_label": item.get("state_label") or "活动中",
            "detail": item.get("detail") or "",
            "task_hint": item.get("task_hint") or "",
            "sessions": 1,
        }

    def _fetch_agents_data(self) -> Dict[str, Any]:
        try:
            context = self._load_runtime_context()
            activity = context["legacy"].get_active_agent_activity(context["selected_env"], context["config"])
            agents = [self._normalize_agent(item) for item in (activity.get("agents") or [])]
            summary = activity.get("summary") or {}
            return {
                "active_count": int(summary.get("active_agents", len(agents)) or 0),
                "recent_sessions": int(summary.get("recent_sessions", 0) or 0),
                "agents": agents,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {
                "active_count": 0,
                "recent_sessions": 0,
                "agents": [],
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
        ts = int(item.get("updated_at") or item.get("created_at") or item.get("last_seen_at") or 0)
        return {
            "id": item.get("id") or item.get("learning_key") or "",
            "title": item.get("title") or item.get("summary") or "未命名 learning",
            "description": item.get("detail") or item.get("action") or "",
            "status": item.get("status") or "pending",
            "category": item.get("category") or "misc",
            "occurrences": int(item.get("occurrences") or 0),
            "timestamp": _iso_from_timestamp(ts),
            "promoted_target": item.get("promoted_target") or "",
        }

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
            items = [self._normalize_learning(item) for item in (payload.get("learnings") or [])]
            reflections = [self._normalize_reflection(item) for item in (payload.get("reflections") or [])]
            promoted = [item for item in items if item.get("status") == "promoted"]
            latest_ts = max(
                [
                    value
                    for value in [
                        *(int(entry.get("updated_at") or entry.get("created_at") or entry.get("last_seen_at") or 0) for entry in (payload.get("learnings") or [])),
                        *(int(entry.get("created_at") or 0) for entry in (payload.get("reflections") or [])),
                    ]
                    if value > 0
                ]
                or [0]
            )
            is_fresh = False
            if latest_ts > 0:
                is_fresh = (datetime.now() - datetime.fromtimestamp(latest_ts)).total_seconds() < 86400
            return {
                "is_fresh": is_fresh,
                "last_update": _iso_from_timestamp(latest_ts),
                "summary": payload.get("summary") or {},
                "source_mode": payload.get("source_mode") or "legacy_store",
                "suggestions": payload.get("suggestions") or [],
                "items": items,
                "reflections": reflections,
                "promoted": promoted,
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
                timestamp = datetime.fromisoformat(f"{date_text}T{time_text}").isoformat()
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
            return [self._normalize_event(item) for item in legacy.get_recent_anomalies(limit=limit, days=7)]
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

    def get_snapshots(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self._get_cached_or_fetch(
            "snapshots",
            self._fetch_snapshot_data,
            force_refresh,
        )

    def _fetch_snapshot_data(self) -> Dict[str, Any]:
        snapshots = _legacy_dashboard().list_snapshots(limit=20)
        return {
            "count": len(snapshots),
            "snapshots": snapshots,
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
        legacy = _legacy_dashboard()
        success, message = legacy.switch_openclaw_environment(env_id)
        self.invalidate_cache()
        return {
            "success": success,
            "message": message,
            "environment": env_id,
            "timestamp": datetime.now().isoformat(),
        }

    def promote_environment(self) -> Dict[str, Any]:
        legacy = _legacy_dashboard()
        result = legacy.execute_official_promotion()
        self.invalidate_cache()
        return result

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
