#!/usr/bin/env python3
"""Promotion controller for official -> primary OpenClaw upgrades."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from monitor_config import save_local_config_value
from snapshot_manager import SnapshotManager


CommandRunner = Callable[[list[str], Optional[dict[str, str]], Optional[int]], tuple[int, str, str]]


def default_runner(args: list[str], env: Optional[dict[str, str]] = None, timeout: Optional[int] = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def get_env_specs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "primary": {
            "id": "primary",
            "home": Path(str(config.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")))),
            "code": Path(str(config.get("OPENCLAW_CODE", str(Path.home() / "openclaw-workspace" / "openclaw")))),
            "port": int(config.get("GATEWAY_PORT", 18789)),
        },
        "official": {
            "id": "official",
            "home": Path(str(config.get("OPENCLAW_OFFICIAL_STATE", str(Path.home() / ".openclaw-official")))),
            "code": Path(str(config.get("OPENCLAW_OFFICIAL_CODE", str(Path.home() / "openclaw-workspace" / "openclaw-official")))),
            "port": int(config.get("OPENCLAW_OFFICIAL_PORT", 19001)),
        },
    }


def build_preflight(environments: list[dict[str, Any]], task_registry: dict[str, Any]) -> dict[str, Any]:
    primary = next((item for item in environments if item.get("id") == "primary"), {})
    official = next((item for item in environments if item.get("id") == "official"), {})
    current = task_registry.get("current") or {}
    current_control = current.get("control") or {}
    blocked_tasks = int((task_registry.get("summary") or {}).get("blocked", 0) or 0)

    checks = [
        {
            "name": "official_present",
            "ok": bool(official),
            "detail": "找到官方验证环境" if official else "未找到官方验证环境",
        },
        {
            "name": "official_running",
            "ok": bool(official.get("running")),
            "detail": "官方验证环境正在运行" if official.get("running") else "官方验证环境未运行",
        },
        {
            "name": "official_healthy",
            "ok": bool(official.get("healthy")),
            "detail": "官方验证环境健康" if official.get("healthy") else "官方验证环境未通过健康检查",
        },
        {
            "name": "blocked_tasks",
            "ok": blocked_tasks == 0,
            "detail": "没有阻塞任务" if blocked_tasks == 0 else f"当前存在 {blocked_tasks} 个阻塞任务",
        },
        {
            "name": "current_task_clear",
            "ok": current_control.get("control_state") not in {
                "blocked_unverified",
                "blocked_control_followup_failed",
                "dev_blocked",
                "test_blocked",
                "analysis_blocked",
            },
            "detail": "当前任务不处于阻塞态" if current_control.get("control_state") not in {
                "blocked_unverified",
                "blocked_control_followup_failed",
                "dev_blocked",
                "test_blocked",
                "analysis_blocked",
            } else "当前活动任务仍处于阻塞态",
        },
        {
            "name": "candidate_differs",
            "ok": bool(primary.get("git_head")) and bool(official.get("git_head")) and primary.get("git_head") != official.get("git_head"),
            "detail": (
                f"将从 {primary.get('git_head')} 切到 {official.get('git_head')}"
                if primary.get("git_head") and official.get("git_head") and primary.get("git_head") != official.get("git_head")
                else "官方候选版本与当前主用版相同或未知"
            ),
        },
    ]
    safe = all(item["ok"] for item in checks[:5])
    return {
        "safe_to_promote": safe,
        "checks": checks,
        "primary_git_head": primary.get("git_head", ""),
        "official_git_head": official.get("git_head", ""),
    }


def rewrite_path_string(value: str, source_root: Path, target_root: Path) -> str:
    source = str(source_root)
    target = str(target_root)
    if value == source:
        return target
    prefix = f"{source}{os.sep}"
    if value.startswith(prefix):
        suffix = value[len(prefix):]
        return str(Path(target) / suffix)
    return value


class PromotionController:
    def __init__(
        self,
        base_dir: Path,
        store: Any,
        config: dict[str, Any],
        *,
        runner: CommandRunner = default_runner,
        time_fn: Callable[[], float] = time.time,
    ):
        self.base_dir = base_dir
        self.store = store
        self.config = config
        self.runner = runner
        self.time_fn = time_fn
        self.specs = get_env_specs(config)

    def _save_state(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload.setdefault("updated_at", int(self.time_fn()))
        self.store.save_runtime_value("promotion_last_run", payload)

    def _run(self, args: list[str], env: Optional[dict[str, str]] = None, timeout: Optional[int] = None) -> tuple[int, str, str]:
        return self.runner(args, env, timeout)

    def _rewrite_value(self, value: Any, source_home: Path, target_home: Path, source_code: Path, target_code: Path) -> Any:
        if isinstance(value, str):
            value = rewrite_path_string(value, source_home, target_home)
            value = rewrite_path_string(value, source_code, target_code)
            return value
        if isinstance(value, list):
            return [self._rewrite_value(v, source_home, target_home, source_code, target_code) for v in value]
        if isinstance(value, dict):
            return {k: self._rewrite_value(v, source_home, target_home, source_code, target_code) for k, v in value.items()}
        return value

    def capture_backups(self, label: str) -> dict[str, str]:
        created: dict[str, str] = {}
        keep = int(self.config.get("SNAPSHOT_RETENTION", 10))
        for env_id, spec in self.specs.items():
            manager = SnapshotManager(self.base_dir, Path(spec["home"]))
            snapshot_dir = manager.create_snapshot(f"{label}-{env_id}")
            manager.prune(keep)
            if snapshot_dir is not None:
                created[env_id] = snapshot_dir.name
        return created

    def sync_primary_code_from_official(self) -> dict[str, Any]:
        primary = self.specs["primary"]
        official = self.specs["official"]
        code, stdout, stderr = self._run(["git", "-C", str(official["code"]), "rev-parse", "HEAD"], timeout=60)
        if code != 0:
            raise RuntimeError(stderr or stdout or "failed to read official HEAD")
        official_head = stdout.strip()
        code, stdout, stderr = self._run(["git", "-C", str(primary["code"]), "fetch", "origin"], timeout=120)
        if code != 0:
            raise RuntimeError(stderr or stdout or "failed to fetch primary repo")
        code, stdout, stderr = self._run(["git", "-C", str(primary["code"]), "reset", "--hard", official_head], timeout=120)
        if code != 0:
            raise RuntimeError(stderr or stdout or "failed to reset primary repo")
        return {"official_head": official_head}

    def sync_primary_state_from_official(self) -> dict[str, Any]:
        primary = self.specs["primary"]
        official = self.specs["official"]
        primary_home = Path(primary["home"])
        official_home = Path(official["home"])
        primary_code = Path(primary["code"])
        official_code = Path(official["code"])

        official_cfg = json.loads((official_home / "openclaw.json").read_text(encoding="utf-8"))
        primary_cfg_path = primary_home / "openclaw.json"
        primary_cfg = json.loads(primary_cfg_path.read_text(encoding="utf-8")) if primary_cfg_path.exists() else {}

        next_cfg = self._rewrite_value(official_cfg, official_home, primary_home, official_code, primary_code)
        if "gateway" in primary_cfg:
            next_cfg["gateway"] = primary_cfg["gateway"]
            next_cfg["gateway"]["port"] = int(primary["port"])

        primary_cfg_path.write_text(json.dumps(next_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        primary_main = primary_home / "agents" / "main" / "agent"
        official_main = official_home / "agents" / "main" / "agent"
        primary_main.mkdir(parents=True, exist_ok=True)
        for name in ("auth-profiles.json", "models.json"):
            shutil.copy2(official_main / name, primary_main / name)

        agents_root = primary_home / "agents"
        for agent_dir in agents_root.iterdir() if agents_root.exists() else []:
            if not agent_dir.is_dir():
                continue
            target = agent_dir / "agent"
            if not target.exists():
                continue
            auth_target = target / "auth-profiles.json"
            models_target = target / "models.json"
            if auth_target != primary_main / "auth-profiles.json":
                shutil.copy2(primary_main / "auth-profiles.json", auth_target)
            if models_target != primary_main / "models.json":
                shutil.copy2(primary_main / "models.json", models_target)

        for source in official_home.glob("workspace-*"):
            if not source.is_dir():
                continue
            target = primary_home / source.name
            target.mkdir(parents=True, exist_ok=True)
            for name in ("AGENTS.md", "SOUL.md"):
                source_file = source / name
                if source_file.exists():
                    content = source_file.read_text(encoding="utf-8")
                    content = content.replace(str(official_home), str(primary_home)).replace(str(official_code), str(primary_code))
                    (target / name).write_text(content, encoding="utf-8")

        return {"primary_config": str(primary_cfg_path)}

    def cutover_primary(self) -> dict[str, Any]:
        desktop_runtime = self.base_dir / "desktop_runtime.sh"
        official_manager = self.base_dir / "manage_official_openclaw.sh"
        if not save_local_config_value(self.base_dir, "ACTIVE_OPENCLAW_ENV", "primary"):
            raise RuntimeError("failed to persist ACTIVE_OPENCLAW_ENV=primary")
        self.store.save_runtime_value("active_openclaw_env", {"env_id": "primary", "updated_at": int(self.time_fn())})
        self._run([str(official_manager), "stop"], timeout=120)
        self._run([str(desktop_runtime), "stop", "gateway"], timeout=120)
        code, stdout, stderr = self._run([str(desktop_runtime), "start", "gateway"], timeout=240)
        if code != 0:
            raise RuntimeError(stderr or stdout or "failed to start primary")
        return {"message": stdout or "primary started"}

    def verify_primary(self) -> dict[str, Any]:
        primary = self.specs["primary"]
        env = dict(os.environ)
        env.update(
            {
                "OPENCLAW_STATE_DIR": str(primary["home"]),
                "OPENCLAW_CONFIG_PATH": str(Path(primary["home"]) / "openclaw.json"),
                "OPENCLAW_GATEWAY_PORT": str(primary["port"]),
            }
        )
        checks: list[dict[str, Any]] = []
        for name, args, timeout in [
            ("models_status", ["openclaw", "models", "status"], 120),
            ("main_agent", ["openclaw", "agent", "--agent", "main", "--message", "Reply with OK only.", "--thinking", "low"], 120),
            ("verifier_agent", ["openclaw", "agent", "--agent", "verifier", "--message", "Reply with OK only.", "--thinking", "low"], 120),
        ]:
            code, stdout, stderr = self._run(args, env=env, timeout=timeout)
            ok = code == 0 and (name == "models_status" or stdout.strip().endswith("OK"))
            checks.append({"name": name, "ok": ok, "stdout": stdout, "stderr": stderr})
            if not ok:
                raise RuntimeError(stderr or stdout or f"verification failed: {name}")
        return {"checks": checks}

    def rollback(self, primary_snapshot_name: str, primary_head: str) -> dict[str, Any]:
        primary = self.specs["primary"]
        manager = SnapshotManager(self.base_dir, Path(primary["home"]))
        snapshot_dir = manager.snapshot_root / primary_snapshot_name
        if snapshot_dir.exists():
            manager.restore_snapshot(snapshot_dir)
        if primary_head:
            self._run(["git", "-C", str(primary["code"]), "reset", "--hard", primary_head], timeout=120)
        desktop_runtime = self.base_dir / "desktop_runtime.sh"
        self._run([str(desktop_runtime), "stop", "gateway"], timeout=120)
        self._run([str(desktop_runtime), "start", "gateway"], timeout=240)
        return {"primary_snapshot": primary_snapshot_name, "primary_head": primary_head}

    def run(self, environments: list[dict[str, Any]], task_registry: dict[str, Any]) -> dict[str, Any]:
        preflight = build_preflight(environments, task_registry)
        self._save_state({"status": "preflight", "preflight": preflight})
        proceed_with_warnings = not preflight["safe_to_promote"]

        backups = self.capture_backups("before-promotion")
        self._save_state({
            "status": "backup",
            "preflight": preflight,
            "preflight_warning": proceed_with_warnings,
            "backups": backups,
        })
        primary_head = preflight.get("primary_git_head", "")

        try:
            code_sync = self.sync_primary_code_from_official()
            state_sync = self.sync_primary_state_from_official()
            self._save_state({
                "status": "cutover",
                "preflight": preflight,
                "backups": backups,
                "code_sync": code_sync,
                "state_sync": state_sync,
            })
            cutover = self.cutover_primary()
            verification = self.verify_primary()
            result = {
                "status": "promoted",
                "preflight": preflight,
                "preflight_warning": proceed_with_warnings,
                "backups": backups,
                "code_sync": code_sync,
                "state_sync": state_sync,
                "cutover": cutover,
                "verification": verification,
            }
            self._save_state(result)
            return result
        except Exception as exc:
            rollback = self.rollback(backups.get("primary", ""), primary_head)
            result = {
                "status": "rolled_back",
                "error": str(exc),
                "preflight": preflight,
                "preflight_warning": proceed_with_warnings,
                "backups": backups,
                "rollback": rollback,
            }
            self._save_state(result)
            return result
