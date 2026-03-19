#!/usr/bin/env python3
"""Version and recovery metadata helpers for the health monitor."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


def _run_git(code_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(code_root), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def collect_version_record(*, code_root: Path, env_id: str, reason: str, status: str = "observed") -> dict[str, Any]:
    branch = _run_git(code_root, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _run_git(code_root, "rev-parse", "HEAD")
    short_commit = _run_git(code_root, "rev-parse", "--short", "HEAD")
    describe = _run_git(code_root, "describe", "--tags", "--always")
    dirty = bool(_run_git(code_root, "status", "--porcelain"))
    origin_url = _run_git(code_root, "remote", "get-url", "origin")
    upstream_url = _run_git(code_root, "remote", "get-url", "upstream")
    ahead_behind = _run_git(code_root, "rev-list", "--left-right", "--count", "HEAD...upstream/main")
    ahead = 0
    behind = 0
    if ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except Exception:
                ahead = 0
                behind = 0
    timestamp = int(time.time())
    return {
        "env_id": env_id,
        "reason": reason,
        "status": status,
        "captured_at": timestamp,
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp)),
        "code_root": str(code_root),
        "branch": branch or "unknown",
        "commit": commit or "unknown",
        "short_commit": short_commit or "unknown",
        "describe": describe or short_commit or commit or "unknown",
        "dirty": dirty,
        "origin_url": origin_url,
        "upstream_url": upstream_url,
        "upstream_ahead": ahead,
        "upstream_behind": behind,
    }


def load_versions_file(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"current": None, "history": [], "known_good": None}


def update_versions_file(path: Path, record: dict[str, Any], *, keep: int = 30, mark_known_good: bool = False) -> dict[str, Any]:
    payload = load_versions_file(path)
    history = [item for item in list(payload.get("history") or []) if isinstance(item, dict)]
    history.append(record)
    payload["current"] = record
    payload["history"] = history[-keep:]
    if mark_known_good:
        payload["known_good"] = record
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_recovery_profile(versions: dict[str, Any]) -> dict[str, Any]:
    current = dict(versions.get("current") or {})
    known_good = dict(versions.get("known_good") or {})
    return {
        "current": current,
        "known_good": known_good,
        "has_known_good": bool(known_good),
        "rollback_hint": {
            "config_snapshot_first": True,
            "code_rollback_manual": True,
            "target_commit": str(known_good.get("commit") or ""),
            "target_describe": str(known_good.get("describe") or ""),
        },
    }
