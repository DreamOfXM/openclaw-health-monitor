#!/usr/bin/env python3
"""Shared configuration helpers for openclaw-health-monitor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = {
    "DINGTALK_WEBHOOK": "",
    "FEISHU_WEBHOOK": "",
    "ENABLE_MAC_NOTIFY": True,
    "CHECK_INTERVAL": 30,
    "HEALTH_CHECK_RETRIES": 3,
    "HEALTH_CHECK_DELAY": 5,
    "GATEWAY_PORT": 18789,
    "SLOW_RESPONSE_THRESHOLD": 30,
    "STALLED_RESPONSE_THRESHOLD": 90,
    "AUTO_UPDATE": False,
    "AUTO_RESTART": True,
    "ENABLE_DESTRUCTIVE_RECOVERY": False,
    "ENABLE_SNAPSHOT_RECOVERY": True,
    "SNAPSHOT_RETENTION": 10,
    "UPDATE_CHANNEL": "stable",
    "CPU_THRESHOLD": 90,
    "MEMORY_THRESHOLD": 85,
    "ALERT_DEDUP_INTERVAL": 600,
}

SECRET_KEYS = {"DINGTALK_WEBHOOK", "FEISHU_WEBHOOK"}


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


def sanitize_config_for_ui(config: dict[str, Any]) -> dict[str, Any]:
    """Return UI-safe config without secret values."""
    safe = dict(config)
    for key in SECRET_KEYS:
        safe[key] = bool(config.get(key))
    return safe
