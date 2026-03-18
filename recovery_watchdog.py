#!/usr/bin/env python3
"""Recovery watchdog: structured anomaly detection + optional local Ollama hinting."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from state_store import MonitorStateStore


CorrectionType = str


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _tail_lines(path: Path, limit: int = 120) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    return lines[-limit:]


def detect_recurrence_problem_code(candidate: dict[str, Any]) -> str:
    """Map watchdog anomalies to stable self-evolution problem codes."""
    anomaly = str(candidate.get("anomaly_type") or "").strip()
    blocked_reason = str(candidate.get("blocked_reason") or "").strip()
    if blocked_reason == "missing_pipeline_receipt":
        return "missing_pipeline_receipt"
    if anomaly == "no_reply_after_commit":
        return "no_reply_after_commit"
    if anomaly in {"wrong_task_binding", "binding_mismatch"}:
        return "wrong_task_binding"
    if anomaly == "late_result_not_adopted":
        return "late_result_not_adopted"
    if anomaly == "delivery_failed_without_notice":
        return "delivery_failed_without_notice"
    if anomaly == "followup_misbound":
        return "followup_misbound"
    if anomaly == "followup_pending_without_main_recovery":
        return "followup_pending_without_main_recovery"
    if anomaly == "received_only_requires_main_followup":
        return "received_only_requires_main_followup"
    if anomaly.startswith("heartbeat_missing_"):
        return anomaly
    if anomaly == "task_blocked_user_visible":
        return "task_blocked_user_visible"
    return "task_closure_missing"


class RecoveryWatchdog:
    def __init__(
        self,
        *,
        base_dir: Path,
        store: MonitorStateStore,
        config: dict[str, Any],
        dispatcher: Callable[[Path, str, str], dict[str, Any]] | None = None,
        ollama_classifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.base_dir = base_dir
        self.store = store
        self.config = config
        self.dispatcher = dispatcher or self._dispatch_via_openclaw
        self.ollama_classifier = ollama_classifier or self._classify_with_ollama

    def _log_guardian(self, message: str) -> None:
        log_file = self.base_dir / "logs" / "guardian.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] [INFO] [RecoveryWatchdog] {message}\n")

    def run(self, spec: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(self.config.get("ENABLE_RECOVERY_WATCHDOG", True))
        env_id = str(spec.get("id") or "primary")
        now = int(time.time())
        status = {
            "generated_at": now,
            "env_id": env_id,
            "enabled": enabled,
            "model_enabled": bool(self.config.get("RECOVERY_WATCHDOG_USE_OLLAMA", True)),
            "candidate_count": 0,
            "hint_count": 0,
            "dispatched_count": 0,
            "cooldown_skips": 0,
            "items": [],
        }
        if not enabled:
            self._save(status, [])
            return status

        context = self._load_context(spec)
        candidates = self._detect_candidates(context)
        status["candidate_count"] = len(candidates)
        hints: list[dict[str, Any]] = []
        for candidate in candidates:
            decision = self._decide(candidate, context)
            item = {**candidate, **decision}
            if decision.get("should_dispatch"):
                dispatch = self._dispatch_candidate(spec, item)
                item["dispatch"] = dispatch
                if dispatch.get("status") == "sent":
                    status["dispatched_count"] += 1
                if dispatch.get("status") == "cooldown":
                    status["cooldown_skips"] += 1
            hints.append(item)
        status["hint_count"] = len(hints)
        status["items"] = hints[:10]
        self._save(status, hints)
        return status

    def _load_context(self, spec: dict[str, Any]) -> dict[str, Any]:
        data_dir = self.base_dir / "data"
        shared_dir = data_dir / "shared-state"
        current_facts = _read_json(data_dir / "current-task-facts.json", {})
        registry = _read_json(shared_dir / "task-registry-snapshot.json", {})
        home_value = spec.get("home") or str(Path.home() / ".openclaw")
        home_path = Path(str(home_value))
        anomalies = _tail_lines(home_path / "logs" / "gateway.err.log", 120)
        if not anomalies:
            anomalies = _tail_lines(home_path / "logs" / "gateway.log", 120)
        return {
            "current_facts": current_facts if isinstance(current_facts, dict) else {},
            "task_registry": registry if isinstance(registry, dict) else {},
            "anomaly_lines": anomalies,
        }

    def _detect_candidates(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        # Phase 2 简化：watchdog 只负责"完成/阻塞但未送达就追"
        facts = context.get("current_facts") or {}
        current_task = dict(facts.get("current_task") or {})
        current_root = dict(facts.get("current_root_task") or {})
        current_run = dict(facts.get("current_workflow_run") or {})
        current_delivery = dict(facts.get("current_delivery_attempt") or {})
        candidates: list[dict[str, Any]] = []
        task_id = str(current_task.get("task_id") or "").strip()
        root_task_id = str(current_root.get("root_task_id") or current_run.get("root_task_id") or "")
        workflow_run_id = str(current_run.get("workflow_run_id") or current_root.get("current_workflow_run_id") or "")
        session_key = str(current_task.get("session_key") or facts.get("session_resolution", {}).get("session_key") or "")
        target_agent = "main"
        state_reason = str(current_run.get("state_reason") or current_root.get("state_reason") or "")
        control_state = str(current_task.get("control_state") or "")
        next_action = str(current_task.get("next_action") or "")
        delivery_state = str(
            current_delivery.get("delivery_state")
            or current_delivery.get("current_state")
            or current_root.get("delivery_state")
            or ""
        ).strip().lower()
        if not task_id:
            return []

        def add_candidate(anomaly_type: str, severity: str, summary: str, **extra: Any) -> None:
            candidates.append({
                "anomaly_type": anomaly_type,
                "severity": severity,
                "summary": summary,
                "task_id": task_id,
                "root_task_id": root_task_id,
                "workflow_run_id": workflow_run_id,
                "session_key": session_key,
                "target_agent": target_agent,
                "evidence": {
                    "task_id": task_id,
                    "state_reason": state_reason,
                    "control_state": control_state,
                    "next_action": next_action,
                    "delivery_state": delivery_state,
                    **extra,
                },
            })

        # 核心逻辑：完成/阻塞但未送达就追
        is_completed = control_state == "completed_verified"
        is_blocked = control_state in {"blocked_unverified", "blocked_control_followup_failed"}
        is_delivered = delivery_state in {"confirmed", "delivered", "delivery_confirmed"}

        if is_completed and not is_delivered:
            add_candidate(
                "completed_not_delivered",
                "high",
                f"task {task_id} is completed but not delivered; main must deliver the result",
            )
        if is_blocked and not is_delivered:
            add_candidate(
                "blocked_not_delivered",
                "critical",
                f"task {task_id} is blocked but not delivered; main must deliver the block verdict",
            )
        return candidates

    @staticmethod
    def _is_terminal(
        current_task: dict[str, Any],
        current_root: dict[str, Any],
        current_run: dict[str, Any],
        current_delivery: dict[str, Any],
    ) -> bool:
        workflow_state = str(current_run.get("current_state") or current_root.get("workflow_state") or "").strip().lower()
        control_state = str(current_task.get("control_state") or "").strip().lower()
        delivery_state = str(
            current_delivery.get("delivery_state")
            or current_delivery.get("current_state")
            or current_root.get("delivery_state")
            or current_root.get("current_state")
            or ""
        ).strip().lower()
        next_action = str(current_task.get("next_action") or "").strip().lower()
        needs_followup = bool(current_task.get("core_truth", {}).get("needs_followup") or current_root.get("needs_followup"))
        if delivery_state in {"confirmed", "delivered", "delivery_confirmed"}:
            return True
        if workflow_state in {"failed", "cancelled", "dlq"}:
            return True
        if control_state in {"blocked", "failed"}:
            return True
        # 当 control_state == "completed_verified" 但 delivery_state 不是 confirmed/delivered 时，非终态
        if control_state == "completed_verified" and delivery_state not in {"confirmed", "delivered", "delivery_confirmed"}:
            return False
        if workflow_state == "delivered":
            return True
        if workflow_state == "delivery_pending" and (needs_followup or next_action == "await_delivery_confirmation"):
            return False
        return False

    @staticmethod
    def _resolve_age_seconds(*items: dict[str, Any]) -> int:
        now = int(time.time())
        timestamps: list[int] = []
        for item in items:
            for key in ("updated_at", "completed_at", "created_at", "started_at", "terminal_at"):
                value = int(item.get(key) or 0)
                if value > 0:
                    timestamps.append(value)
        if not timestamps:
            return 0
        return max(0, now - max(timestamps))

    def _decide(self, candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if not bool(self.config.get("RECOVERY_WATCHDOG_USE_OLLAMA", True)):
            return self._fallback_decision(candidate)
        try:
            result = self.ollama_classifier(candidate, context)
            if isinstance(result, dict) and result.get("should_dispatch") is not None:
                return result
        except Exception:
            pass
        return self._fallback_decision(candidate)

    @staticmethod
    def _fallback_decision(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "should_dispatch": True,
            "target_agent": candidate.get("target_agent") or "main",
            "severity": candidate.get("severity") or "medium",
            "reason": detect_recurrence_problem_code(candidate),
            "hint_title": f"WATCHDOG_RECOVERY_HINT:{candidate.get('anomaly_type') or 'unknown'}",
            "hint_message": candidate.get("summary") or "detected a recoverable closure anomaly",
        }

    def _classify_with_ollama(self, candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        model = str(self.config.get("RECOVERY_WATCHDOG_OLLAMA_MODEL") or "").strip()
        if not model:
            self._log_guardian(
                f"Ollama classification skipped: missing model for anomaly={candidate.get('anomaly_type') or 'unknown'}"
            )
            return self._fallback_decision(candidate)
        url = str(self.config.get("RECOVERY_WATCHDOG_OLLAMA_URL") or "http://127.0.0.1:11434/api/generate")
        prompt = {
            "instruction": "You classify OpenClaw recovery anomalies. Never decide user-facing replies. Output compact JSON only.",
            "candidate": candidate,
            "current_task": (context.get("current_facts") or {}).get("current_task") or {},
            "current_root_task": (context.get("current_facts") or {}).get("current_root_task") or {},
            "current_workflow_run": (context.get("current_facts") or {}).get("current_workflow_run") or {},
            "rules": {
                "allowed_target_agent": ["main", "pm"],
                "should_dispatch_means": "send an internal hint to main or pm for re-evaluation",
            },
        }
        payload = json.dumps(
            {
                "model": model,
                "prompt": json.dumps(prompt, ensure_ascii=False),
                "stream": False,
                "format": {
                    "type": "object",
                    "properties": {
                        "should_dispatch": {"type": "boolean"},
                        "target_agent": {"type": "string"},
                        "severity": {"type": "string"},
                        "reason": {"type": "string"},
                        "hint_title": {"type": "string"},
                        "hint_message": {"type": "string"},
                    },
                    "required": ["should_dispatch", "target_agent", "severity", "reason", "hint_title", "hint_message"],
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        timeout_seconds = float(self.config.get("RECOVERY_WATCHDOG_OLLAMA_TIMEOUT_SECONDS", 8))
        anomaly_type = candidate.get("anomaly_type") or "unknown"
        self._log_guardian(
            f"Ollama classification request: model={model} anomaly={anomaly_type} target={candidate.get('target_agent') or 'main'} timeout={timeout_seconds}s"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8", errors="ignore"))
            body = str(raw.get("response") or "{}").strip()
            parsed = json.loads(body)
        except Exception as exc:
            self._log_guardian(
                f"Ollama classification failed: model={model} anomaly={anomaly_type} error={type(exc).__name__}: {exc}"
            )
            raise
        target_agent = str(parsed.get("target_agent") or candidate.get("target_agent") or "main")
        if target_agent not in {"main", "pm"}:
            target_agent = candidate.get("target_agent") or "main"
        decision = {
            "should_dispatch": bool(parsed.get("should_dispatch")),
            "target_agent": target_agent,
            "severity": str(parsed.get("severity") or candidate.get("severity") or "medium"),
            "reason": str(parsed.get("reason") or detect_recurrence_problem_code(candidate)),
            "hint_title": str(parsed.get("hint_title") or f"WATCHDOG_RECOVERY_HINT:{candidate.get('anomaly_type') or 'unknown'}"),
            "hint_message": str(parsed.get("hint_message") or candidate.get("summary") or "detected anomaly"),
        }
        self._log_guardian(
            f"Ollama classification decision: model={model} anomaly={anomaly_type} should_dispatch={decision['should_dispatch']} target={decision['target_agent']} severity={decision['severity']}"
        )
        return decision

    def _dispatch_candidate(self, spec: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        target_agent = str(item.get("target_agent") or "main")
        dedupe_key = "|".join(
            [
                str(item.get("task_id") or ""),
                str(item.get("root_task_id") or ""),
                str(item.get("workflow_run_id") or ""),
                str(item.get("anomaly_type") or item.get("reason") or ""),
                target_agent,
            ]
        )
        cooldown_seconds = int(self.config.get("RECOVERY_WATCHDOG_COOLDOWN_SECONDS", 600))
        max_attempts = int(self.config.get("RECOVERY_WATCHDOG_MAX_ATTEMPTS", 3))
        runtime_key = f"watchdog_recovery_dispatch:{dedupe_key}"
        state = self.store.load_runtime_value(runtime_key, {})
        now = int(time.time())
        last_dispatched_at = int((state or {}).get("last_dispatched_at") or 0)
        attempts = int((state or {}).get("attempts") or 0)
        if last_dispatched_at and (now - last_dispatched_at) < cooldown_seconds:
            return {"status": "cooldown", "attempts": attempts, "last_dispatched_at": last_dispatched_at}
        if attempts >= max_attempts:
            self.store.append_runtime_event(
                f"watchdog_recovery_dispatches:{spec.get('id') or 'primary'}",
                {
                    "dedupe_key": dedupe_key,
                    "task_id": item.get("task_id"),
                    "root_task_id": item.get("root_task_id"),
                    "workflow_run_id": item.get("workflow_run_id"),
                    "anomaly_type": item.get("anomaly_type"),
                    "result": {"status": "watchdog_exhausted", "attempts": attempts},
                },
                limit=50,
            )
            return {"status": "watchdog_exhausted", "attempts": attempts}
        if not bool(self.config.get("ENABLE_RECOVERY_WATCHDOG_DISPATCH", True)):
            return {"status": "dry_run", "attempts": attempts}
        target_session_key = f"agent:{target_agent}:main"
        message = self._build_hint_message(item, target_session_key)
        code_root_value = spec.get("code") or self.config.get("OPENCLAW_CODE") or str(self.base_dir)
        result = self.dispatcher(Path(str(code_root_value)), target_session_key, message)
        next_state = {"last_dispatched_at": now, "attempts": attempts + 1, "last_result": result}
        self.store.save_runtime_value(runtime_key, next_state)
        self.store.append_runtime_event(
            f"watchdog_recovery_dispatches:{spec.get('id') or 'primary'}",
            {
                "dedupe_key": dedupe_key,
                "target_session_key": target_session_key,
                "root_task_id": item.get("root_task_id"),
                "workflow_run_id": item.get("workflow_run_id"),
                "anomaly_type": item.get("anomaly_type"),
                "result": result,
            },
            limit=50,
        )
        return {"status": "sent" if result.get("ok") else "error", "attempts": attempts + 1, **result}

    @staticmethod
    def _build_hint_message(item: dict[str, Any], target_session_key: str) -> str:
        payload = {
            "type": "WATCHDOG_RECOVERY_HINT",
            "task_id": item.get("task_id"),
            "target_session_key": target_session_key,
            "anomaly_type": item.get("anomaly_type"),
            "severity": item.get("severity"),
            "root_task_id": item.get("root_task_id"),
            "workflow_run_id": item.get("workflow_run_id"),
            "session_key": item.get("session_key"),
            "reason": item.get("reason"),
            "hint_title": item.get("hint_title"),
            "hint_message": item.get("hint_message"),
            "evidence": item.get("evidence") or {},
        }
        return (
            "Internal watchdog recovery hint. This is a control-plane pushback, not a user request. "
            "You must re-evaluate the active task and choose one of: resume execution, ask the responsible agent for structured evidence, or declare a user-visible blocked state. "
            "Do not leave this hint as pending bookkeeping only, and do not claim closure until a real reply reached the user. "
            "If the task is already truly closed and delivered, reply ONLY with NO_REPLY.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _lookup_session_id_via_openclaw(code_root: Path, target_session_key: str) -> dict[str, Any]:
        cmd = ["node", "openclaw.mjs", "sessions", "--json", "--all-agents", "--active", "10080"]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(code_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "command": cmd}
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0 or not stdout:
            return {
                "ok": False,
                "returncode": result.returncode,
                "stdout": stdout[-400:],
                "stderr": stderr[-400:],
                "command": cmd,
            }
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid sessions json: {exc}", "stdout": stdout[-400:], "command": cmd}
        for item in payload.get("sessions", []):
            if item.get("key") == target_session_key and item.get("sessionId"):
                return {"ok": True, "session_id": str(item["sessionId"]), "command": cmd}
        return {"ok": False, "error": f"session not found: {target_session_key}", "command": cmd}

    @classmethod
    def _dispatch_via_openclaw(cls, code_root: Path, target_session_key: str, message: str) -> dict[str, Any]:
        lookup = cls._lookup_session_id_via_openclaw(code_root, target_session_key)
        if not lookup.get("ok"):
            return {"ok": False, "phase": "lookup_session", **lookup}
        cmd = [
            "node",
            "openclaw.mjs",
            "agent",
            "--session-id",
            str(lookup.get("session_id") or ""),
            "--channel",
            "feishu",
            "--message",
            message,
            "--timeout",
            "0",
            "--thinking",
            "low",
        ]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(code_root),
                capture_output=True,
                text=True,
                timeout=90,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "command": cmd, "phase": "dispatch"}
        return {
            "ok": result.returncode == 0,
            "phase": "dispatch",
            "session_id": lookup.get("session_id"),
            "target_session_key": target_session_key,
            "command": cmd,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip()[-400:],
            "stderr": (result.stderr or "").strip()[-400:],
        }

    def _save(self, status: dict[str, Any], hints: list[dict[str, Any]]) -> None:
        env_id = str(status.get("env_id") or "primary")
        self.store.save_runtime_value(f"watchdog_recovery_status:{env_id}", status)
        self.store.save_runtime_value(f"watchdog_recovery_hints:{env_id}", hints[:20])
