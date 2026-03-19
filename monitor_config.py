#!/usr/bin/env python3
"""Shared configuration helpers for openclaw-health-monitor."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = {
    "DINGTALK_WEBHOOK": "",
    "FEISHU_WEBHOOK": "",
    "WEBHOOK_ALLOWED_HOSTS": "oapi.dingtalk.com,api.dingtalk.com,open.feishu.cn",
    "ENABLE_MAC_NOTIFY": True,
    "CHECK_INTERVAL": 30,
    "HEALTH_CHECK_RETRIES": 3,
    "HEALTH_CHECK_DELAY": 5,
    "GATEWAY_PORT": 18789,
    "SLOW_RESPONSE_THRESHOLD": 30,
    "STALLED_RESPONSE_THRESHOLD": 90,
    "PROGRESS_PUSH_INTERVAL": 180,
    "PROGRESS_PUSH_COOLDOWN": 300,
    "PROGRESS_ESCALATION_INTERVAL": 600,
    "GUARDIAN_STALE_TASK_MAX_AGE": 3600,
    "GUARDIAN_FOLLOWUP_TIMEOUT": 120,
    "GUARDIAN_FOLLOWUP_RETRIES": 2,
    "GUARDIAN_FOLLOWUP_RETRY_DELAY": 3,
    "GUARDIAN_BLOCKED_COOLDOWN": 900,
    "GUARDIAN_BLOCKED_NOTICE_INTERVAL": 1800,
    "ENABLE_TASK_REGISTRY": True,
    "TASK_REGISTRY_MAX_ACTIVE": 1,
    "TASK_REGISTRY_RETENTION": 100,
    "ENABLE_BOOTSTRAP_INIT": True,
    "BOOTSTRAP_WRITE_MISSING": False,
    "TASK_CONTROL_RECEIPT_GRACE": 180,
    "TASK_CONTROL_FOLLOWUP_COOLDOWN": 300,
    "TASK_CONTROL_MAX_ATTEMPTS": 2,
    "TASK_CONTROL_BLOCK_TIMEOUT": 900,
    "ENABLE_INTRUSIVE_TASK_CONTROL": True,
    "ENABLE_RECOVERY_WATCHDOG": True,
    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
    "RECOVERY_WATCHDOG_USE_OLLAMA": True,
    "RECOVERY_WATCHDOG_OLLAMA_MODEL": "qwen2.5:3b-instruct",
    "RECOVERY_WATCHDOG_OLLAMA_URL": "http://127.0.0.1:11434/api/generate",
    "RECOVERY_WATCHDOG_OLLAMA_TIMEOUT_SECONDS": 8,
    "RECOVERY_WATCHDOG_COOLDOWN_SECONDS": 600,
    "RECOVERY_WATCHDOG_MAX_ATTEMPTS": 3,
    "RECOVERY_WATCHDOG_RECEIPT_STALE_SECONDS": 180,
    "RECOVERY_WATCHDOG_DELIVERY_PENDING_SECONDS": 180,
    "TASK_CONTRACTS_FILE": "",
    "ENABLE_EVOLUTION_PLANE": True,
    "LEARNING_PROMOTION_THRESHOLD": 3,
    "REFLECTION_INTERVAL_SECONDS": 3600,
    "AGENT_ACTIVITY_LOOKBACK_SECONDS": 1800,
    "AGENT_ACTIVITY_SCAN_LIMIT": 12,
    "DB_RETENTION_ENABLED": True,
    "DB_RETENTION_INTERVAL_SECONDS": 21600,
    "DB_RETENTION_HEARTBEATS_DAYS": 1,
    "DB_RETENTION_HEALTH_SAMPLES_DAYS": 3,
    "DB_RETENTION_CHANGE_EVENTS_DAYS": 7,
    "DB_RETENTION_TASK_EVENTS_DAYS": 7,
    "DB_RETENTION_WATCHER_TASKS_DAYS": 7,
    "DB_RETENTION_TASK_CONTROL_ACTIONS_DAYS": 7,
    "DB_RETENTION_FOLLOWUPS_DAYS": 7,
    "DB_RETENTION_CORE_EVENTS_DAYS": 14,
    "DB_RETENTION_MANAGED_TASKS_DAYS": 14,
    "DB_RETENTION_ROOT_TASKS_DAYS": 14,
    "DB_RETENTION_WORKFLOW_RUNS_DAYS": 14,
    "DB_RETENTION_STEP_RUNS_DAYS": 14,
    "DB_RETENTION_REFLECTION_RUNS_DAYS": 30,
    "AUTO_UPDATE": False,
    "AUTO_RESTART": True,
    "ENABLE_DESTRUCTIVE_RECOVERY": False,
    "ENABLE_SNAPSHOT_RECOVERY": True,
    "SNAPSHOT_RETENTION": 10,
    "UPDATE_CHANNEL": "stable",
    "CPU_THRESHOLD": 90,
    "MEMORY_THRESHOLD": 85,
    "ALERT_DEDUP_INTERVAL": 600,
    "OPENCLAW_HOME": str(Path.home() / ".openclaw"),
    "OPENCLAW_CODE": str(Path.home() / "openclaw-workspace" / "openclaw"),
    "ACTIVE_OPENCLAW_ENV": "primary",
}

SECRET_KEYS = {"DINGTALK_WEBHOOK", "FEISHU_WEBHOOK"}
WEBHOOK_CONFIG_KEYS = {"DINGTALK_WEBHOOK", "FEISHU_WEBHOOK"}
ACTIVE_BINDING_RELATIVE_PATH = Path("data") / "shared-state" / "active-binding.json"


def _coerce_value(raw: str) -> Any:
    value = raw.strip().strip('"').strip("'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.isdigit():
        return int(value)
    if "$HOME" in value:
        return value.replace("$HOME", str(Path.home()))
    return value


def _parse_config_file(path: Path, config: dict[str, Any]) -> None:
    if not path.exists():
        return
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key] = _coerce_value(value)


def load_config(base_dir: Path) -> dict[str, Any]:
    """Load layered config from tracked file, local override, then env."""
    config = DEFAULT_CONFIG.copy()
    config_file = base_dir / "config.conf"
    local_config_file = base_dir / "config.local.conf"

    _parse_config_file(config_file, config)
    _parse_config_file(local_config_file, config)

    for key in DEFAULT_CONFIG:
        env_value = os.environ.get(key)
        if env_value:
            config[key] = _coerce_value(env_value)

    return config


def save_local_config_value(base_dir: Path, key: str, value: str) -> bool:
    """Persist a config key into config.local.conf only."""
    config_file = base_dir / "config.local.conf"
    try:
        lines: list[str] = []
        if config_file.exists():
            with open(config_file) as handle:
                lines = handle.readlines()

        found = False
        new_lines: list[str] = []
        for line in lines:
            if line.strip().startswith(key + "="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            new_lines.append(f"{key}={value}\n")

        with open(config_file, "w") as handle:
            handle.writelines(new_lines)
        return True
    except Exception:
        return False


def parse_webhook_allowlist(config: dict[str, Any]) -> set[str]:
    raw = str(config.get("WEBHOOK_ALLOWED_HOSTS", "") or "").strip()
    if not raw:
        return set()
    return {
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    }


def is_webhook_url_allowed(url: str, config: dict[str, Any]) -> tuple[bool, str]:
    value = str(url or "").strip().strip('"').strip("'")
    if not value:
        return True, ""
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False, "Webhook URL 格式无效"

    allowlist = parse_webhook_allowlist(config)
    if not allowlist:
        return True, ""
    if host in allowlist:
        return True, ""
    return False, f"Webhook 域名未在白名单中: {host}"


def validate_config_update(key: str, value: str, config: dict[str, Any]) -> tuple[bool, str]:
    if key not in WEBHOOK_CONFIG_KEYS:
        return True, ""
    return is_webhook_url_allowed(value, config)


def get_env_specs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the static environment registry."""
    primary_port = int(config.get("GATEWAY_PORT", 18789))
    primary_home = Path(str(config.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))))
    primary_code = Path(str(config.get("OPENCLAW_CODE", str(Path.home() / "openclaw-workspace" / "openclaw"))))
    return {
        "primary": {
            "env_id": "primary",
            "role": "stable",
            "name": "当前主用版",
            "code_root": str(primary_code),
            "state_root": str(primary_home),
            "config_path": str(primary_home / "openclaw.json"),
            "gateway_label": "ai.openclaw.gateway",
            "gateway_port": primary_port,
            "manager_kind": "launchagent",
        },
    }


def active_binding_path(base_dir: Path) -> Path:
    return base_dir / ACTIVE_BINDING_RELATIVE_PATH


def read_active_binding(base_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    specs = get_env_specs(config)
    selected = str(config.get("ACTIVE_OPENCLAW_ENV", "primary")).strip() or "primary"
    selected = selected if selected in specs else "primary"
    default_binding = {
        "active_env": selected,
        "switch_state": "committed",
        "binding_version": 1,
        "updated_at": int(time.time()),
        "expected": dict(specs[selected]),
    }
    path = active_binding_path(base_dir)
    if not path.exists():
        return default_binding
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        env_id = str(data.get("active_env") or selected).strip() or selected
        if env_id not in specs:
            return default_binding
        expected = data.get("expected") if isinstance(data.get("expected"), dict) else {}
        merged_expected = dict(specs[env_id])
        merged_expected.update(expected)
        return {
            "active_env": env_id,
            "switch_state": str(data.get("switch_state") or "committed"),
            "binding_version": int(data.get("binding_version") or 1),
            "updated_at": int(data.get("updated_at") or int(time.time())),
            "expected": merged_expected,
        }
    except Exception:
        return default_binding


def write_active_binding(base_dir: Path, config: dict[str, Any], env_id: str, *, switch_state: str = "committed") -> dict[str, Any]:
    specs = get_env_specs(config)
    if env_id not in specs:
        raise ValueError(f"unknown env_id: {env_id}")
    current = read_active_binding(base_dir, config)
    binding = {
        "active_env": env_id,
        "switch_state": switch_state,
        "binding_version": int(current.get("binding_version") or 0) + 1,
        "updated_at": int(time.time()),
        "expected": dict(specs[env_id]),
    }
    path = active_binding_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
    return binding


def sanitize_config_for_ui(config: dict[str, Any]) -> dict[str, Any]:
    """Return UI-safe config without secret values."""
    safe = dict(config)
    for key in SECRET_KEYS:
        safe[key] = bool(config.get(key))
    return safe
