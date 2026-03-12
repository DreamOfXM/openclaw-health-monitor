#!/usr/bin/env python3
"""Bootstrap helpers for OpenClaw initialization and self-evolution baseline."""

from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path
from typing import Any


CONTEXT_LIFECYCLE_BASELINE: dict[str, Any] = {
    "session": {
        "memoryFlush": {"enabled": True, "maxTurns": 120},
        "contextPruning": {"enabled": True, "tokenBudget": 180000},
        "dailyReset": {"enabled": True, "hour": 4},
        "idleReset": {"enabled": True, "seconds": 21600},
        "sessionMaintenance": {"enabled": True, "intervalSeconds": 1800},
    }
}


def load_openclaw_payload(config_path: Path) -> dict[str, Any]:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_openclaw_payload(config_path: Path, payload: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def merge_context_lifecycle_baseline(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    merged = copy.deepcopy(payload or {})
    session_cfg = merged.setdefault("session", {})
    baseline_session = CONTEXT_LIFECYCLE_BASELINE["session"]
    report = {"mode": "merge_missing", "applied": [], "preserved": [], "created_session": "session" not in (payload or {})}

    for section, expected in baseline_session.items():
        current = session_cfg.get(section)
        if isinstance(current, dict):
            report["preserved"].append(section)
            for key, value in expected.items():
                if key not in current:
                    current[key] = value
                    report["applied"].append(f"{section}.{key}")
        else:
            session_cfg[section] = copy.deepcopy(expected)
            report["applied"].append(section)
    return merged, report


def build_context_lifecycle_readiness_from_payload(openclaw_payload: dict[str, Any], *, env_id: str) -> dict[str, Any]:
    session_cfg = openclaw_payload.get("session") or {}
    memory_cfg = openclaw_payload.get("memory") or {}
    memory_flush_cfg = memory_cfg.get("memoryFlush") or session_cfg.get("memoryFlush") or {}
    pruning_cfg = memory_cfg.get("contextPruning") or session_cfg.get("contextPruning") or {}
    daily_reset_cfg = session_cfg.get("dailyReset") or {}
    idle_reset_cfg = session_cfg.get("idleReset") or {}
    maintenance_cfg = session_cfg.get("sessionMaintenance") or session_cfg.get("maintenance") or {}

    def build_threshold_check(
        *,
        name: str,
        actual: Any,
        expected: Any,
        exists: bool,
        ok: bool,
        success_detail: str,
        missing_detail: str,
        degraded_detail: str,
    ) -> dict[str, Any]:
        if ok:
            detail = success_detail
        elif exists:
            detail = degraded_detail
        else:
            detail = missing_detail
        return {
            "name": name,
            "ok": ok,
            "actual": actual,
            "expected": expected,
            "detail": detail,
        }

    checks = [
        build_threshold_check(
            name="memory_flush",
            actual=memory_flush_cfg,
            expected={"enabled": True, "maxTurns": 120},
            exists=bool(memory_flush_cfg),
            ok=bool(memory_flush_cfg.get("enabled")) and int(memory_flush_cfg.get("maxTurns") or 0) >= 120,
            success_detail="memoryFlush 已达到推荐基线",
            missing_detail="缺少 memoryFlush 策略",
            degraded_detail=f"memoryFlush.maxTurns={int(memory_flush_cfg.get('maxTurns') or 0)}，低于基线 120" if memory_flush_cfg else "缺少 memoryFlush 策略",
        ),
        build_threshold_check(
            name="context_pruning",
            actual=pruning_cfg,
            expected={"enabled": True, "tokenBudget": 180000},
            exists=bool(pruning_cfg),
            ok=bool(pruning_cfg.get("enabled")) and int(pruning_cfg.get("tokenBudget") or 0) >= 180000,
            success_detail="contextPruning 已达到推荐基线",
            missing_detail="缺少 contextPruning 策略",
            degraded_detail=f"contextPruning.tokenBudget={int(pruning_cfg.get('tokenBudget') or 0)}，低于基线 180000" if pruning_cfg else "缺少 contextPruning 策略",
        ),
        build_threshold_check(
            name="daily_reset",
            actual=daily_reset_cfg,
            expected={"enabled": True, "hour": 4},
            exists=bool(daily_reset_cfg),
            ok=bool(daily_reset_cfg.get("enabled")) and daily_reset_cfg.get("hour") is not None,
            success_detail="dailyReset 已达到推荐基线",
            missing_detail="缺少 dailyReset 策略",
            degraded_detail=f"dailyReset 配置不完整：{json.dumps(daily_reset_cfg, ensure_ascii=False)}" if daily_reset_cfg else "缺少 dailyReset 策略",
        ),
        build_threshold_check(
            name="idle_reset",
            actual=idle_reset_cfg,
            expected={"enabled": True, "seconds": 21600},
            exists=bool(idle_reset_cfg),
            ok=bool(idle_reset_cfg.get("enabled")) and int(idle_reset_cfg.get("seconds") or 0) >= 21600,
            success_detail="idleReset 已达到推荐基线",
            missing_detail="缺少 idleReset 策略",
            degraded_detail=f"idleReset.seconds={int(idle_reset_cfg.get('seconds') or 0)}，低于基线 21600" if idle_reset_cfg else "缺少 idleReset 策略",
        ),
        build_threshold_check(
            name="session_maintenance",
            actual=maintenance_cfg,
            expected={"enabled": True, "intervalSeconds": 1800},
            exists=bool(maintenance_cfg),
            ok=bool(maintenance_cfg.get("enabled")) and int(maintenance_cfg.get("intervalSeconds") or 0) <= 1800 and int(maintenance_cfg.get("intervalSeconds") or 0) > 0,
            success_detail="sessionMaintenance 已达到推荐基线",
            missing_detail="缺少 sessionMaintenance 策略",
            degraded_detail=f"sessionMaintenance.intervalSeconds={int(maintenance_cfg.get('intervalSeconds') or 0)}，高于基线 1800" if maintenance_cfg else "缺少 sessionMaintenance 策略",
        ),
    ]
    missing_required = any(not item.get("actual") for item in checks)
    degraded = any(item.get("actual") and not item.get("ok") for item in checks)
    status = "ready" if all(bool(item.get("ok")) for item in checks) else ("not_ready" if missing_required else "degraded")
    return {
        "ready": status == "ready",
        "status": status,
        "checks": checks,
        "target_env": env_id,
        "headline": "长期运行基线已达标" if status == "ready" else ("长期运行基线缺少关键配置" if status == "not_ready" else "长期运行基线已配置但未达标"),
        "recommended_baseline": copy.deepcopy(CONTEXT_LIFECYCLE_BASELINE),
        "has_degraded_items": degraded,
    }


def resolve_workspace_dirs(home: Path, payload: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    defaults_workspace = (
        (payload.get("agents") or {})
        .get("defaults", {})
        .get("workspace")
    )
    if defaults_workspace:
        workspace = Path(str(defaults_workspace)).expanduser()
        if not workspace.is_absolute():
            workspace = home / workspace
        candidates.append(workspace)
    candidates.extend(sorted(path for path in home.glob("workspace*") if path.is_dir()))
    if not candidates:
        candidates.append(home / "workspace")
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve() if path.exists() else path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _ensure_file(path: Path, content: str, *, created: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(str(path))


def _touch_file(path: Path, *, created: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    created.append(str(path))


def default_soul_content(env_id: str) -> str:
    return (
        "# SOUL\n\n"
        f"- env: {env_id}\n"
        "- 你必须长期运行、减少重复犯错、保留长期有效经验。\n"
        "- 你不得自动改写 SOUL.md。\n"
        "- 当经验稳定时，应沉淀到 MEMORY.md 或 Skills，而不是让上下文无限膨胀。\n"
    )


def default_agents_content() -> str:
    return (
        "# AGENTS\n\n"
        "## 协作协议\n\n"
        "- `[request]`: @对方 + ack_id + 动作 + 截止时间\n"
        "- `[confirmed]`: @发起方 + 相同 ack_id + 生效结论\n"
        "- `[final]`: 线程收敛的唯一终态\n"
        "- `final` 后默认 `NO_REPLY`\n"
        "- timeout != failed\n"
        "- 关键状态优先落 shared-context，不把聊天当数据库\n"
    )


def default_memory_content(env_id: str) -> str:
    return (
        "# MEMORY\n\n"
        f"- env: {env_id}\n"
        "- 这里只保留长期有效规则、用户偏好、稳定工作流。\n"
        "- 每日细节放到 memory/YYYY-MM-DD.md。\n"
        "- 高频问题从 .learnings promote 进入这里。\n"
    )


def ensure_bootstrap_workspace(
    *,
    home: Path,
    env_id: str,
    write_missing: bool = True,
) -> dict[str, Any]:
    config_path = home / "openclaw.json"
    original = load_openclaw_payload(config_path)
    _, merge_report = merge_context_lifecycle_baseline(original)
    readiness = build_context_lifecycle_readiness_from_payload(original, env_id=env_id)

    created: list[str] = []
    shared_root = home / "shared-context"
    workspace_dirs = resolve_workspace_dirs(home, original)

    if write_missing:
        for rel_dir in (
            ".learnings",
            "memory",
            "shared-context/intel",
            "shared-context/status",
            "shared-context/job-status",
            "shared-context/monitor-tasks",
        ):
            path = home / rel_dir
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                created.append(str(path))
        _ensure_file(home / ".learnings" / "ERRORS.md", "# Errors\n\n- 暂无待处理错误模式\n", created=created)
        _ensure_file(home / ".learnings" / "LEARNINGS.md", "# Learnings\n\n- 暂无学习记录\n", created=created)
        _ensure_file(home / ".learnings" / "FEATURE_REQUESTS.md", "# Feature Requests\n\n- 暂无 feature requests\n", created=created)
        _ensure_file(home / "MEMORY.md", default_memory_content(env_id), created=created)
        _ensure_file(shared_root / "tech-radar.json", "[]\n", created=created)
        _touch_file(shared_root / "monitor-tasks" / "tasks.jsonl", created=created)
        _touch_file(shared_root / "monitor-tasks" / "watcher.log", created=created)
        _touch_file(shared_root / "monitor-tasks" / "audit.log", created=created)
        _touch_file(shared_root / "monitor-tasks" / "dlq.jsonl", created=created)
        for workspace in workspace_dirs:
            if not workspace.exists():
                workspace.mkdir(parents=True, exist_ok=True)
                created.append(str(workspace))
            _ensure_file(workspace / "SOUL.md", default_soul_content(env_id), created=created)
            _ensure_file(workspace / "AGENTS.md", default_agents_content(), created=created)

    items = [
        {"path": str(home / ".learnings"), "exists": (home / ".learnings").exists()},
        {"path": str(home / "memory"), "exists": (home / "memory").exists()},
        {"path": str(home / "MEMORY.md"), "exists": (home / "MEMORY.md").exists()},
        {"path": str(shared_root), "exists": shared_root.exists()},
    ]
    for workspace in workspace_dirs:
        items.append({"path": str(workspace / "SOUL.md"), "exists": (workspace / "SOUL.md").exists()})
        items.append({"path": str(workspace / "AGENTS.md"), "exists": (workspace / "AGENTS.md").exists()})

    return {
        "env_id": env_id,
        "home": str(home),
        "config_path": str(config_path),
        "write_mode": "merge_missing" if write_missing else "check_only",
        "workspace_dirs": [str(path) for path in workspace_dirs],
        "config_merge": merge_report,
        "context_readiness": readiness,
        "created_paths": created,
        "checks": items,
        "ready": all(item["exists"] for item in items) and readiness["status"] in {"ready", "degraded"},
    }


def derive_watcher_task_id(payload: dict[str, Any]) -> str:
    for key in ("task_id", "request_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
