#!/usr/bin/env python3
"""Recovery watchdog: structured anomaly detection + optional local Ollama hinting."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from state_store import MonitorStateStore


CorrectionType = str


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if not raw or not raw.strip():
            return default
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if text[-1:] not in {"}", "]"}:
                return default
            return default
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


def _validate_runtime_projection(
    *,
    task_id: str,
    root_task_id: str,
    workflow_run_id: str,
    workflow_state: str,
    delivery_state: str,
    control_state: str,
) -> dict[str, Any] | None:
    """Return a quarantine hint when the projected runtime state is internally inconsistent."""
    terminal_states = {"failed", "cancelled", "dlq", "delivered"}
    if control_state and not task_id:
        return {"reason": "missing_task_id", "detail": "control_state exists without task_id"}
    if task_id and not root_task_id and workflow_state not in terminal_states:
        return {"reason": "missing_root_task_id", "detail": "active task is missing root_task_id"}
    if workflow_state in terminal_states and delivery_state == "undelivered" and control_state not in {"blocked", "failed", "completed_verified"}:
        return {"reason": "terminal_without_delivery_path", "detail": "terminal workflow has no valid delivery/control state"}
    if workflow_run_id and not root_task_id:
        return {"reason": "orphan_workflow_run", "detail": "workflow_run_id exists without root_task_id"}
    return None


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
    if anomaly == "delivery_failed":
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
    if anomaly == "completed_not_delivered":
        return "task_closure_missing"
    if anomaly == "blocked_not_delivered":
        return "task_blocked_user_visible"
    if anomaly == "openclaw_unreachable":
        return "openclaw_unreachable"
    if anomaly == "execution_finished_result_uncommitted":
        return "no_visible_result_timeout"
    if anomaly == "runtime_projection_invalid":
        return "task_closure_missing"
    if anomaly == "no_visible_result_timeout":
        return "no_visible_result_timeout"
    return "task_closure_missing"


class RecoveryWatchdog:
    def __init__(
        self,
        *,
        base_dir: Path,
        store: MonitorStateStore,
        config: dict[str, Any],
        dispatcher: Callable[[Path, str, str], dict[str, Any]] | None = None,
        ollama_classifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
        | None = None,
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
            "model_enabled": bool(
                self.config.get("RECOVERY_WATCHDOG_USE_OLLAMA", True)
            ),
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
        runtime_health = _read_json(shared_dir / "runtime-health.json", {})
        home_value = spec.get("home") or str(Path.home() / ".openclaw")
        home_path = Path(str(home_value))
        anomalies = _tail_lines(home_path / "logs" / "gateway.err.log", 120)
        if not anomalies:
            anomalies = _tail_lines(home_path / "logs" / "gateway.log", 120)
        return {
            "current_facts": current_facts if isinstance(current_facts, dict) else {},
            "task_registry": registry if isinstance(registry, dict) else {},
            "runtime_health": runtime_health
            if isinstance(runtime_health, dict)
            else {},
            "anomaly_lines": anomalies,
        }

    def _detect_candidates(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        # 极小可落地版：watchdog 只负责 5 类关键异常。
        facts = context.get("current_facts") or {}
        runtime_health = context.get("runtime_health") or {}
        current_task = dict(facts.get("current_task") or {})
        current_root = dict(facts.get("current_root_task") or {})
        current_run = dict(facts.get("current_workflow_run") or {})
        current_finalizer = dict(facts.get("current_finalizer") or {})
        current_delivery = dict(facts.get("current_delivery_attempt") or {})
        candidates: list[dict[str, Any]] = []
        task_id = str(current_task.get("task_id") or "").strip()
        root_task_id = str(
            current_root.get("root_task_id") or current_run.get("root_task_id") or ""
        )
        workflow_run_id = str(
            current_run.get("workflow_run_id")
            or current_root.get("current_workflow_run_id")
            or ""
        )
        session_key = str(
            current_task.get("session_key")
            or facts.get("session_resolution", {}).get("session_key")
            or ""
        )
        target_agent = "main"
        state_reason = str(
            current_run.get("state_reason") or current_root.get("state_reason") or ""
        )
        control_state = str(current_task.get("control_state") or "")
        next_action = str(current_task.get("next_action") or "")
        control = dict(current_task.get("control") or {})
        delivery_state = (
            str(
                current_delivery.get("delivery_state")
                or current_delivery.get("current_state")
                or current_root.get("delivery_state")
                or ""
            )
            .strip()
            .lower()
        )
        workflow_state = (
            str(
                current_run.get("current_state")
                or current_root.get("workflow_state")
                or ""
            )
            .strip()
            .lower()
        )
        finalization_state = (
            str(
                current_finalizer.get("decision_state")
                or current_run.get("finalization_state")
                or current_root.get("finalization_state")
                or ""
            )
            .strip()
            .lower()
        )
        recent_logs = list((context.get("anomaly_lines") or [])[-8:])
        heartbeat_age = int(
            current_task.get("heartbeat_age_seconds")
            or control.get("heartbeat_age_seconds")
            or ((control.get("timing") or {}).get("heartbeat_age_seconds") if isinstance(control.get("timing"), dict) else 0)
            or 0
        )
        followup_stage = str(current_task.get("followup_stage") or control.get("followup_stage") or "").strip().lower()
        timing = dict(control.get("timing") or {})
        hard_followup = int(timing.get("hard_followup") or self.config.get("RECOVERY_WATCHDOG_FOLLOWUP_SECONDS", 180) or 180)
        core_supervision = dict(control.get("core_supervision") or {})
        needs_followup = bool(core_supervision.get("needs_followup") or current_root.get("needs_followup"))
        projection_issue = _validate_runtime_projection(
            task_id=task_id,
            root_task_id=root_task_id,
            workflow_run_id=workflow_run_id,
            workflow_state=workflow_state,
            delivery_state=delivery_state,
            control_state=control_state,
        )

        def build_recent_events() -> list[dict[str, Any]]:
            events: list[dict[str, Any]] = []
            if workflow_state:
                events.append({"type": "workflow_state", "value": workflow_state})
            if finalization_state:
                events.append(
                    {"type": "finalization_state", "value": finalization_state}
                )
            if delivery_state:
                events.append({"type": "delivery_state", "value": delivery_state})
            if control_state:
                events.append({"type": "control_state", "value": control_state})
            return events

        def build_evidence(**extra: Any) -> dict[str, Any]:
            return {
                "workflow_state": workflow_state,
                "delivery_state": delivery_state,
                "finalization_state": finalization_state,
                "recent_events": build_recent_events(),
                "related_logs": recent_logs,
                "identifiers": {
                    "task_id": task_id,
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "session_key": session_key,
                },
                **extra,
            }

        def add_candidate(
            incident_type: str,
            severity: str,
            summary: str,
            recommended_action: str,
            **extra: Any,
        ) -> None:
            created_at = (
                datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            incident_id = (
                f"inc:{incident_type}:{int(time.time())}:{len(candidates) + 1}"
            )
            candidates.append(
                {
                    "incident_id": incident_id,
                    "incident_type": incident_type,
                    "anomaly_type": incident_type,
                    "severity": severity,
                    "status": "open",
                    "summary": summary,
                    "task_id": task_id,
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "session_key": session_key,
                    "target_agent": target_agent,
                    "recommended_action": recommended_action,
                    "created_at": created_at,
                    "resolved_at": None,
                    "evidence": build_evidence(
                        state_reason=state_reason,
                        control_state=control_state,
                        next_action=next_action,
                        **extra,
                    ),
                }
            )

        gateway_running = bool(runtime_health.get("gateway_running"))
        gateway_healthy = bool(runtime_health.get("gateway_healthy"))
        runtime_health_present = isinstance(runtime_health, dict) and bool(
            runtime_health
        )
        if runtime_health_present and (not gateway_running or not gateway_healthy):
            add_candidate(
                "openclaw_unreachable",
                "critical",
                "OpenClaw runtime probe reports the gateway is unreachable",
                "restart_openclaw",
                runtime_health_snapshot=runtime_health,
            )

        # 核心逻辑：完成/阻塞但未送达就追
        is_completed = control_state == "completed_verified"
        is_blocked = control_state in {
            "blocked_unverified",
            "blocked_control_followup_failed",
        }
        is_delivered = delivery_state in {
            "confirmed",
            "delivered",
            "delivery_confirmed",
        }
        has_finalizer = finalization_state == "finalized" or workflow_state in {
            "completed",
            "delivery_pending",
            "delivered",
        }
        delivery_terminal_failed = delivery_state in {
            "delivery_failed",
            "delivery_dlq_entered",
        }
        age_seconds = self._resolve_age_seconds(
            current_task, current_root, current_run, current_delivery, current_finalizer
        )
        timeout_seconds = int(
            self.config.get("RECOVERY_WATCHDOG_NO_VISIBLE_RESULT_TIMEOUT_SECONDS", 900)
        )

        if task_id and is_completed and has_finalizer and not is_delivered:
            add_candidate(
                "completed_not_delivered",
                "high",
                f"task {task_id} is completed but not delivered; main must deliver the result",
                "retry_delivery",
            )
        if task_id and is_blocked and not is_delivered:
            add_candidate(
                "blocked_not_delivered",
                "critical",
                f"task {task_id} is blocked but not delivered; main must deliver the block verdict",
                "deliver_blocked_verdict",
            )
        if task_id and delivery_terminal_failed:
            add_candidate(
                "delivery_failed",
                "high",
                f"task {task_id} hit a terminal delivery failure and needs one retry or a visible fallback",
                "retry_delivery_once",
                delivery_failure=True,
            )
        if projection_issue:
            add_candidate(
                "runtime_projection_invalid",
                "high",
                f"runtime projection is inconsistent: {projection_issue['detail']}",
                "quarantine_projection_and_rebuild",
                projection_issue=projection_issue,
            )

        is_execution_finished = workflow_state in {"completed", "delivery_pending"} or finalization_state in {"ready", "completed", "finalized"}
        if (
            task_id
            and age_seconds >= timeout_seconds
            and not is_delivered
            and not is_blocked
            and is_execution_finished
        ):
            add_candidate(
                "execution_finished_result_uncommitted",
                "high",
                f"task {task_id} finished execution but result is not committed/visible yet",
                "commit_result_or_retry_delivery",
                age_seconds=age_seconds,
                timeout_seconds=timeout_seconds,
                finalization_state=finalization_state,
            )

        if (
            task_id
            and age_seconds >= timeout_seconds
            and not is_delivered
            and not is_blocked
            and workflow_state
            not in {
                "completed",
                "delivery_pending",
                "delivered",
                "failed",
                "cancelled",
                "dlq",
            }
            and not is_execution_finished
        ):
            add_candidate(
                "no_visible_result_timeout",
                "medium",
                f"task {task_id} has no visible result within the timeout window",
                "main_recheck_or_block",
                age_seconds=age_seconds,
                timeout_seconds=timeout_seconds,
            )

        orphan_followup = bool(task_id and needs_followup and (not root_task_id or workflow_state in {"failed", "cancelled", "dlq", "delivered"}))
        if orphan_followup and not is_delivered and heartbeat_age >= hard_followup:
            add_candidate(
                "followup_pending_without_main_recovery",
                "critical",
                f"task {task_id} has orphaned followup state without recoverable main/root binding",
                "requeue_orphan_followup",
                heartbeat_age_seconds=heartbeat_age,
                followup_stage=followup_stage,
                root_task_id=root_task_id or None,
                workflow_state=workflow_state,
            )

        if task_id and not is_delivered and control_state in {"received_only", "progress_only"} and needs_followup and heartbeat_age >= hard_followup:
            add_candidate(
                "followup_pending_without_main_recovery",
                "high",
                f"task {task_id} has stalled in {control_state} for {heartbeat_age}s without main recovery",
                "main_recheck_or_block",
                heartbeat_age_seconds=heartbeat_age,
                followup_stage=followup_stage,
                hard_followup_seconds=hard_followup,
            )

        if task_id and not is_delivered and followup_stage == "blocked" and next_action in {"manual_or_session_recovery", "require_receipt_or_block"}:
            add_candidate(
                "received_only_requires_main_followup",
                "critical",
                f"task {task_id} is stuck in blocked followup stage without a main-visible closure decision",
                "main_recheck_or_block",
                heartbeat_age_seconds=heartbeat_age,
                followup_stage=followup_stage,
            )
        return candidates

    @staticmethod
    def _is_terminal(
        current_task: dict[str, Any],
        current_root: dict[str, Any],
        current_run: dict[str, Any],
        current_delivery: dict[str, Any],
    ) -> bool:
        workflow_state = (
            str(
                current_run.get("current_state")
                or current_root.get("workflow_state")
                or ""
            )
            .strip()
            .lower()
        )
        control_state = str(current_task.get("control_state") or "").strip().lower()
        delivery_state = (
            str(
                current_delivery.get("delivery_state")
                or current_delivery.get("current_state")
                or current_root.get("delivery_state")
                or current_root.get("current_state")
                or ""
            )
            .strip()
            .lower()
        )
        next_action = str(current_task.get("next_action") or "").strip().lower()
        needs_followup = bool(
            current_task.get("core_truth", {}).get("needs_followup")
            or current_root.get("needs_followup")
        )
        if delivery_state in {"confirmed", "delivered", "delivery_confirmed"}:
            return True
        if workflow_state in {"failed", "cancelled", "dlq"}:
            return True
        if control_state in {"blocked", "failed"}:
            return True
        # 当 control_state == "completed_verified" 但 delivery_state 不是 confirmed/delivered 时，非终态
        if control_state == "completed_verified" and delivery_state not in {
            "confirmed",
            "delivered",
            "delivery_confirmed",
        }:
            return False
        if workflow_state == "delivered":
            return True
        if workflow_state == "delivery_pending" and (
            needs_followup or next_action == "await_delivery_confirmation"
        ):
            return False
        return False

    @staticmethod
    def _resolve_age_seconds(*items: dict[str, Any]) -> int:
        now = int(time.time())
        timestamps: list[int] = []
        for item in items:
            for key in (
                "updated_at",
                "completed_at",
                "created_at",
                "started_at",
                "terminal_at",
            ):
                value = int(item.get(key) or 0)
                if value > 0:
                    timestamps.append(value)
        if not timestamps:
            return 0
        return max(0, now - max(timestamps))

    def _decide(
        self, candidate: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
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
            "hint_title": f"WATCHDOG_RECOVERY_HINT:{candidate.get('incident_type') or candidate.get('anomaly_type') or 'unknown'}",
            "hint_message": candidate.get("summary")
            or "detected a recoverable closure anomaly",
        }

    def _classify_with_ollama(
        self, candidate: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        model = str(self.config.get("RECOVERY_WATCHDOG_OLLAMA_MODEL") or "").strip()
        if not model:
            self._log_guardian(
                f"Ollama classification skipped: missing model for anomaly={candidate.get('anomaly_type') or 'unknown'}"
            )
            return self._fallback_decision(candidate)
        url = str(
            self.config.get("RECOVERY_WATCHDOG_OLLAMA_URL")
            or "http://127.0.0.1:11434/api/generate"
        )
        prompt = {
            "instruction": "You classify OpenClaw recovery anomalies. Never decide user-facing replies. Output compact JSON only.",
            "candidate": candidate,
            "current_task": (context.get("current_facts") or {}).get("current_task")
            or {},
            "current_root_task": (context.get("current_facts") or {}).get(
                "current_root_task"
            )
            or {},
            "current_workflow_run": (context.get("current_facts") or {}).get(
                "current_workflow_run"
            )
            or {},
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
                    "required": [
                        "should_dispatch",
                        "target_agent",
                        "severity",
                        "reason",
                        "hint_title",
                        "hint_message",
                    ],
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout_seconds = float(
            self.config.get("RECOVERY_WATCHDOG_OLLAMA_TIMEOUT_SECONDS", 8)
        )
        anomaly_type = (
            candidate.get("incident_type") or candidate.get("anomaly_type") or "unknown"
        )
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
        target_agent = str(
            parsed.get("target_agent") or candidate.get("target_agent") or "main"
        )
        if target_agent not in {"main", "pm"}:
            target_agent = candidate.get("target_agent") or "main"
        decision = {
            "should_dispatch": bool(parsed.get("should_dispatch")),
            "target_agent": target_agent,
            "severity": str(
                parsed.get("severity") or candidate.get("severity") or "medium"
            ),
            "reason": str(
                parsed.get("reason") or detect_recurrence_problem_code(candidate)
            ),
            "hint_title": str(
                parsed.get("hint_title")
                or f"WATCHDOG_RECOVERY_HINT:{candidate.get('incident_type') or candidate.get('anomaly_type') or 'unknown'}"
            ),
            "hint_message": str(
                parsed.get("hint_message")
                or candidate.get("summary")
                or "detected anomaly"
            ),
        }
        self._log_guardian(
            f"Ollama classification decision: model={model} anomaly={anomaly_type} should_dispatch={decision['should_dispatch']} target={decision['target_agent']} severity={decision['severity']}"
        )
        return decision

    def _resolve_target_session_key(self, item: dict[str, Any], target_agent: str) -> str:
        session_key = str(item.get("session_key") or "").strip()
        if target_agent == "main" and session_key:
            return session_key
        if session_key:
            parts = session_key.split(":")
            if len(parts) >= 2:
                parts[1] = target_agent
                return ":".join(parts)
        return f"agent:{target_agent}:main"

    def _dispatch_candidate(
        self, spec: dict[str, Any], item: dict[str, Any]
    ) -> dict[str, Any]:
        target_agent = str(item.get("target_agent") or "main")
        dedupe_key = "|".join(
            [
                str(item.get("task_id") or ""),
                str(item.get("root_task_id") or ""),
                str(item.get("workflow_run_id") or ""),
                str(
                    item.get("incident_type")
                    or item.get("anomaly_type")
                    or item.get("reason")
                    or ""
                ),
                target_agent,
            ]
        )
        cooldown_seconds = int(
            self.config.get("RECOVERY_WATCHDOG_COOLDOWN_SECONDS", 600)
        )
        max_attempts = int(self.config.get("RECOVERY_WATCHDOG_MAX_ATTEMPTS", 3))
        runtime_key = f"watchdog_recovery_dispatch:{dedupe_key}"
        state = self.store.load_runtime_value(runtime_key, {})
        now = int(time.time())
        last_dispatched_at = int((state or {}).get("last_dispatched_at") or 0)
        attempts = int((state or {}).get("attempts") or 0)
        if last_dispatched_at and (now - last_dispatched_at) < cooldown_seconds:
            return {
                "status": "cooldown",
                "attempts": attempts,
                "last_dispatched_at": last_dispatched_at,
            }
        if attempts >= max_attempts:
            self.store.append_runtime_event(
                f"watchdog_recovery_dispatches:{spec.get('id') or 'primary'}",
                {
                    "dedupe_key": dedupe_key,
                    "task_id": item.get("task_id"),
                    "root_task_id": item.get("root_task_id"),
                    "workflow_run_id": item.get("workflow_run_id"),
                    "incident_type": item.get("incident_type"),
                    "result": {"status": "watchdog_exhausted", "attempts": attempts},
                },
                limit=50,
            )
            return {"status": "watchdog_exhausted", "attempts": attempts}
        if not bool(self.config.get("ENABLE_RECOVERY_WATCHDOG_DISPATCH", True)):
            return {"status": "dry_run", "attempts": attempts}
        target_session_key = self._resolve_target_session_key(item, target_agent)
        message = self._build_hint_message(item, target_session_key)
        code_root_value = (
            spec.get("code") or self.config.get("OPENCLAW_CODE") or str(self.base_dir)
        )
        result = self.dispatcher(
            Path(str(code_root_value)), target_session_key, message
        )
        next_state = {
            "last_dispatched_at": now,
            "attempts": attempts + 1,
            "last_result": result,
        }
        self.store.save_runtime_value(runtime_key, next_state)
        self.store.append_runtime_event(
            f"watchdog_recovery_dispatches:{spec.get('id') or 'primary'}",
            {
                "dedupe_key": dedupe_key,
                "target_session_key": target_session_key,
                "root_task_id": item.get("root_task_id"),
                "workflow_run_id": item.get("workflow_run_id"),
                "incident_type": item.get("incident_type"),
                "result": result,
            },
            limit=50,
        )
        return {
            "status": "sent" if result.get("ok") else "error",
            "attempts": attempts + 1,
            **result,
        }

    @staticmethod
    def _build_hint_message(item: dict[str, Any], target_session_key: str) -> str:
        payload = {
            "type": "WATCHDOG_RECOVERY_HINT",
            "incident_id": item.get("incident_id"),
            "incident_type": item.get("incident_type") or item.get("anomaly_type"),
            "status": item.get("status") or "open",
            "task_id": item.get("task_id"),
            "target_session_key": target_session_key,
            "anomaly_type": item.get("anomaly_type"),
            "severity": item.get("severity"),
            "root_task_id": item.get("root_task_id"),
            "workflow_run_id": item.get("workflow_run_id"),
            "session_key": item.get("session_key"),
            "reason": item.get("reason"),
            "recommended_action": item.get("recommended_action"),
            "created_at": item.get("created_at"),
            "hint_title": item.get("hint_title"),
            "hint_message": item.get("hint_message"),
            "evidence": item.get("evidence") or {},
        }
        incident_type = str(item.get("incident_type") or item.get("anomaly_type") or "unknown")
        recommended_action = str(item.get("recommended_action") or "")
        action_clause = ""
        if recommended_action == "requeue_orphan_followup":
            action_clause = (
                "This followup is orphaned from its main/root binding. Rebind or explicitly requeue the followup instead of waiting for main recovery. "
            )
        elif recommended_action == "commit_result_or_retry_delivery":
            action_clause = (
                "Treat execution as finished but result not committed. Prefer commit/reindex/retry-delivery work before declaring timeout. "
            )
        elif recommended_action == "quarantine_projection_and_rebuild":
            action_clause = (
                "The projected runtime state is internally inconsistent. Quarantine the broken projection, rebuild facts from authoritative state, and do not let it flow into replay/reporting unchanged. "
            )
        blocking_clause = (
            "If incident_type == blocked_not_delivered, you must produce a user-visible blocked explanation in this same recovery path unless delivery is already confirmed. "
            "Do not stop at internal acknowledgement, and do not leave the task in blocked-but-silent state. "
        )
        return (
            "Internal watchdog recovery hint. This is a control-plane pushback, not a user request. "
            "You must re-evaluate the active task and make exactly one adjudication: accept_repair, observe_only, dismiss, or need_more_evidence. "
            "If you accept_repair, do the smallest safe action: retry delivery, resume execution, or send a user-visible blocked explanation. "
            + action_clause
            + blocking_clause
            + "Do not leave this hint as pending bookkeeping only, and do not claim closure until a real reply reached the user. If the task is already truly closed and delivered, reply ONLY with NO_REPLY.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _infer_channel_from_session_key(session_key: str) -> str:
        parts = str(session_key or "").split(":")
        if len(parts) >= 4 and parts[2]:
            return parts[2]
        return "feishu"

    @classmethod
    def _dispatch_via_openclaw(
        cls, code_root: Path, target_session_key: str, message: str
    ) -> dict[str, Any]:
        channel = cls._infer_channel_from_session_key(target_session_key)
        cmd = [
            "node",
            "openclaw.mjs",
            "agent",
            "--session-key",
            target_session_key,
            "--channel",
            channel,
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
        self.store.save_runtime_value(
            f"watchdog_recovery_incidents:{env_id}", hints[:20]
        )
