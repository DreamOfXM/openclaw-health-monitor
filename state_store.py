#!/usr/bin/env python3
"""SQLite-backed state store for openclaw-health-monitor."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

UNKNOWN_REASON_CODE = "unknown_reason"

REASON_CODE_ALIASES = {
    "dispatch_started": "legacy.dispatch_started",
    "contract_assigned": "legacy.contract_assigned",
    "stage_progress": "legacy.stage_progress",
    "visible_completion": "legacy.visible_completion",
    "legacy_projection": "legacy.projection",
    "workflow_not_found": "state.workflow_not_found",
    "workflow_routed": "workflow.routed",
    "workflow_failed": "workflow.failed",
    "workflow_cancelled": "workflow.cancelled",
    "workflow_accepted": "workflow.accepted",
    "workflow_queued": "workflow.queued",
    "workflow_resumed": "workflow.resumed",
    "manual_retry_requested": "workflow.manual_retry_requested",
    "executor_started": "step.executor_started",
    "executor_completed": "step.executor_completed",
    "pipeline_receipt:started": "receipt.pipeline_started",
    "pipeline_receipt:completed": "receipt.pipeline_completed",
    "pipeline_receipt:blocked": "receipt.pipeline_blocked",
    "receipt_adopted_started": "receipt.adopted_started",
    "receipt_adopted_completed": "receipt.adopted_completed",
    "receipt_adopted_blocked": "receipt.adopted_blocked",
    "finalizer_done": "finalizer.completed",
    "ready": "finalizer.ready",
    "finalizer_finalized": "finalizer.finalized",
    "channel_ack": "delivery.confirmed",
    "delivery_failed": "delivery.failed",
    "delivery_dlq_entered": "delivery.dlq_entered",
    "protocol_violation": "protocol.violation",
    "background_result": "result.late",
    "blocked_unverified": "followup.blocked_unverified",
    "missing_pipeline_receipt": "followup.missing_pipeline_receipt",
    "manual_followup": "followup.manual_followup",
    "delivery_retry": "followup.delivery_retry",
    "await_delivery_confirmation": "followup.await_delivery_confirmation",
}

REASON_CODE_TAXONOMY = {
    UNKNOWN_REASON_CODE,
    "legacy.dispatch_started",
    "legacy.contract_assigned",
    "legacy.stage_progress",
    "legacy.visible_completion",
    "legacy.projection",
    "state.workflow_not_found",
    "workflow.accepted",
    "workflow.routed",
    "workflow.queued",
    "workflow.resumed",
    "workflow.manual_retry_requested",
    "workflow.failed",
    "workflow.cancelled",
    "workflow.delivery_pending",
    "workflow.delivery_failed",
    "workflow.dlq",
    "workflow.ambiguous_success",
    "step.executor_started",
    "step.executor_completed",
    "step.started",
    "step.completed",
    "step.blocked",
    "receipt.pipeline_started",
    "receipt.pipeline_completed",
    "receipt.pipeline_blocked",
    "receipt.adopted_started",
    "receipt.adopted_completed",
    "receipt.adopted_blocked",
    "finalizer.ready",
    "finalizer.completed",
    "finalizer.finalized",
    "delivery.sent",
    "delivery.observed",
    "delivery.confirmed",
    "delivery.failed",
    "delivery.dlq_entered",
    "followup.blocked_unverified",
    "followup.missing_pipeline_receipt",
    "followup.manual_followup",
    "followup.delivery_retry",
    "followup.await_delivery_confirmation",
    "followup.pipeline_recovery",
    "followup.protocol_violation",
    "binding.switched",
    "binding.run_pointer_switched",
    "binding.retarget_existing_root",
    "binding.retarget_new_root",
    "correction.applied",
    "protocol.violation",
    "result.late",
}

CORE_EVENT_TYPES = {
    "request_accepted",
    "workflow_accepted",
    "workflow_routed",
    "workflow_queued",
    "manual_retry_requested",
    "workflow_resumed",
    "step_started",
    "receipt_adopted_started",
    "receipt_adopted_completed",
    "receipt_adopted_blocked",
    "workflow_failed",
    "workflow_cancelled",
    "ambiguous_success_detected",
    "finalizer_finalized",
    "delivery_sent",
    "delivery_observed",
    "delivery_confirmed",
    "delivery_failed",
    "delivery_dlq_entered",
    "followup_requested",
    "followup_resolved",
    "followup_closed",
    "late_result_recorded",
    "foreground_binding_switched",
    "workflow_run_pointer_switched",
    "retarget_to_existing_root",
    "retarget_to_new_root",
    "correction_applied",
}

SELF_EVOLUTION_STATES = {
    "recorded",
    "candidate_rule",
    "adopted",
    "verified",
    "closed",
    "reopened",
}

SELF_EVOLUTION_EVENT_TYPES = {
    "recorded",
    "candidate_rule",
    "adopted",
    "verified",
    "closed",
    "reopened",
    "recurrence",
}

SELF_EVOLUTION_PROBLEM_CODES = {
    "missing_pipeline_receipt",
    "wrong_task_binding",
    "no_reply_after_commit",
    "task_execution_stalled",
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
    "guardian_crash",
    "gateway_unhealthy",
    "openclaw_unreachable",
    "model_timeout",
    "failover_exhausted",
    "channel_inflight_stuck",
    "purity_gate_failed",
    "run_tracking_warning",
    "tool_interrupted_no_reply",
    "no_visible_result_timeout",
    "unknown_problem",
}

SELF_EVOLUTION_PROBLEM_CODE_ALIASES = {
    "watchdog_signal": "task_closure_missing",
    "binding_mismatch": "wrong_task_binding",
    "heartbeat_missing": "heartbeat_missing_soft",
    "dispatch_stuck": "task_execution_stalled",
    "stage_stuck": "task_execution_stalled",
    "stalled_reply": "task_execution_stalled",
    "gateway_ws_closed": "gateway_unhealthy",
    "main_closure_purity_gate_failed": "purity_gate_failed",
    "llm_request_timed_out": "model_timeout",
    "inflight_skipped": "channel_inflight_stuck",
}

DEFAULT_DURATION_PROFILES = {
    "short": {
        "first_ack_sla": 30,
        "heartbeat_interval": 45,
        "hard_timeout": 180,
        "soft_followup": 30,
        "hard_followup": 75,
        "auto_blocked_unverified": 180,
        "blocked_user_visible": True,
    },
    "medium": {
        "first_ack_sla": 60,
        "heartbeat_interval": 120,
        "hard_timeout": 900,
        "soft_followup": 60,
        "hard_followup": 180,
        "auto_blocked_unverified": 900,
        "blocked_user_visible": True,
    },
    "long": {
        "first_ack_sla": 120,
        "heartbeat_interval": 300,
        "hard_timeout": 2700,
        "soft_followup": 120,
        "hard_followup": 420,
        "auto_blocked_unverified": 2700,
        "blocked_user_visible": True,
    },
}

DEFAULT_PHASE_POLICIES = {
    "planning": "short",
    "implementation": "long",
    "testing": "medium",
    "calculation": "short",
    "verification": "short",
    "risk_assessment": "short",
}

TERMINAL_MSG_STATES = {"completed", "failed", "blocked"}
TERMINAL_DELIVERY_STATES = {"delivered", "owner_escalated"}


def normalize_msg_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state in {"", "accepted", "routed", "queued", "started", "open"}:
        return "open"
    if state in {"completed", "delivered", "delivery_pending"}:
        return "completed"
    if state == "blocked":
        return "blocked"
    if state in {"failed", "cancelled", "dlq", "delivery_failed", "ambiguous_success"}:
        return "failed"
    if state == "background":
        return "background"
    return state or "open"


def normalize_delivery_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state in {"delivered", "delivery_confirmed", "confirmed", "channel_ack"}:
        return "delivered"
    if state in {"owner_escalated", "watchdog_exhausted", "escalated"}:
        return "owner_escalated"
    return "undelivered"


def is_resolved_msg_state(value: Any) -> bool:
    return normalize_msg_state(value) in TERMINAL_MSG_STATES


def is_closed_delivery_state(value: Any) -> bool:
    return normalize_delivery_state(value) in TERMINAL_DELIVERY_STATES
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class RetryingSQLiteConnection(sqlite3.Connection):
    """SQLite connection with lightweight retry for transient lock contention."""

    retry_attempts = 5
    retry_sleep_seconds = 0.05

    @staticmethod
    def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
        return "locked" in str(exc).lower()

    def _retry_sqlite_call(self, fn, *args, **kwargs):
        for attempt in range(self.retry_attempts):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not self._is_locked_error(exc) or attempt >= self.retry_attempts - 1:
                    raise
                time.sleep(self.retry_sleep_seconds * (attempt + 1))
        raise AssertionError("unreachable")

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        return self._retry_sqlite_call(super().execute, sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        return self._retry_sqlite_call(super().executemany, sql, seq_of_parameters)

    def executescript(self, sql_script, /):  # type: ignore[override]
        return self._retry_sqlite_call(super().executescript, sql_script)

    def commit(self):  # type: ignore[override]
        return self._retry_sqlite_call(super().commit)


class MonitorStateStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.db_path = base_dir / "data" / "monitor.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._init_db()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, factory=RetryingSQLiteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.OperationalError:
            pass
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_state (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(namespace, key)
                );

                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_date TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS health_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at INTEGER NOT NULL,
                    process_running INTEGER NOT NULL,
                    gateway_healthy INTEGER NOT NULL,
                    cpu REAL,
                    mem_used REAL,
                    mem_total REAL
                );

                CREATE TABLE IF NOT EXISTS managed_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    env_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    question TEXT NOT NULL,
                    last_user_message TEXT NOT NULL,
                    blocked_reason TEXT NOT NULL,
                    latest_receipt_json TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    last_progress_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL DEFAULT 0,
                    backgrounded_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_managed_tasks_session_updated
                ON managed_tasks(session_key, updated_at DESC);

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_key TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_contracts (
                    task_id TEXT PRIMARY KEY,
                    contract_type TEXT NOT NULL,
                    contract_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_control_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    env_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    control_state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    required_receipts_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_followup_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    resolved_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_task_control_actions_task_status
                ON task_control_actions(task_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    learning_key TEXT NOT NULL UNIQUE,
                    env_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    promoted_target TEXT NOT NULL DEFAULT '',
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_learnings_status_updated
                ON learnings(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS reflection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS self_evolution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    learning_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    problem_code TEXT NOT NULL,
                    root_task_id TEXT NOT NULL DEFAULT '',
                    workflow_run_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_self_evolution_events_learning_created
                ON self_evolution_events(learning_key, created_at ASC, id ASC);

                CREATE INDEX IF NOT EXISTS idx_self_evolution_events_problem_created
                ON self_evolution_events(problem_code, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS self_evolution_projection (
                    learning_key TEXT PRIMARY KEY,
                    problem_code TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    candidate_rule_json TEXT NOT NULL DEFAULT '{}',
                    adopted_rule_target TEXT NOT NULL DEFAULT '',
                    verified_at INTEGER NOT NULL DEFAULT 0,
                    verified_in TEXT NOT NULL DEFAULT '',
                    recurrence_count INTEGER NOT NULL DEFAULT 0,
                    last_root_task_id TEXT NOT NULL DEFAULT '',
                    last_workflow_run_id TEXT NOT NULL DEFAULT '',
                    last_evidence_json TEXT NOT NULL DEFAULT '{}',
                    last_actor TEXT NOT NULL DEFAULT '',
                    last_event_type TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    closed_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_self_evolution_projection_state_updated
                ON self_evolution_projection(current_state, updated_at DESC);

                CREATE TABLE IF NOT EXISTS watcher_tasks (
                    watcher_task_id TEXT PRIMARY KEY,
                    env_id TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    completed_at INTEGER NOT NULL DEFAULT 0,
                    delivered_at INTEGER NOT NULL DEFAULT 0,
                    last_checked_at INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    in_dlq INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_watcher_tasks_env_state_updated
                ON watcher_tasks(env_id, current_state, updated_at DESC);

                CREATE TABLE IF NOT EXISTS root_tasks (
                    root_task_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    origin_request_id TEXT NOT NULL DEFAULT '',
                    origin_message_id TEXT NOT NULL DEFAULT '',
                    reply_to_message_id TEXT NOT NULL DEFAULT '',
                    user_goal_summary TEXT NOT NULL,
                    intent_type TEXT NOT NULL DEFAULT '',
                    contract_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    state_reason TEXT NOT NULL DEFAULT '',
                    current_workflow_run_id TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    foreground_priority INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    terminal_at INTEGER NOT NULL DEFAULT 0,
                    finalized_at INTEGER NOT NULL DEFAULT 0,
                    superseded_by_root_task_id TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_root_tasks_session_updated
                ON root_tasks(session_key, updated_at DESC);

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    workflow_run_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    parent_workflow_run_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    workflow_type TEXT NOT NULL,
                    intent_type TEXT NOT NULL DEFAULT '',
                    contract_type TEXT NOT NULL DEFAULT '',
                    current_state TEXT NOT NULL,
                    state_reason TEXT NOT NULL DEFAULT '',
                    current_step_run_id TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER NOT NULL DEFAULT 0,
                    terminal_at INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_runs_root_updated
                ON workflow_runs(root_task_id, updated_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_idempotency
                ON workflow_runs(idempotency_key)
                WHERE idempotency_key != '';

                CREATE TABLE IF NOT EXISTS step_runs (
                    step_run_id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL,
                    root_task_id TEXT NOT NULL,
                    stable_step_key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    state_reason TEXT NOT NULL DEFAULT '',
                    latest_receipt_id TEXT NOT NULL DEFAULT '',
                    latest_heartbeat_seq INTEGER NOT NULL DEFAULT 0,
                    last_heartbeat_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER NOT NULL DEFAULT 0,
                    terminal_at INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_step_runs_workflow_updated
                ON step_runs(workflow_run_id, updated_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_step_runs_stable_key
                ON step_runs(workflow_run_id, stable_step_key);

                CREATE TABLE IF NOT EXISTS finalizer_records (
                    finalization_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    decision_state TEXT NOT NULL,
                    final_status TEXT NOT NULL DEFAULT '',
                    trigger_reason TEXT NOT NULL DEFAULT '',
                    delivery_state TEXT NOT NULL DEFAULT '',
                    delivery_attempt_no INTEGER NOT NULL DEFAULT 0,
                    delivery_channel TEXT NOT NULL DEFAULT '',
                    last_delivery_error TEXT NOT NULL DEFAULT '',
                    user_visible_summary TEXT NOT NULL DEFAULT '',
                    finalized_by TEXT NOT NULL DEFAULT '',
                    finalized_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_finalizer_records_root_updated
                ON finalizer_records(root_task_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    delivery_attempt_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    finalization_id TEXT NOT NULL DEFAULT '',
                    attempt_no INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    confirmation_level TEXT NOT NULL DEFAULT '',
                    current_state TEXT NOT NULL,
                    state_reason TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    terminal_at INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_delivery_attempts_root_updated
                ON delivery_attempts(root_task_id, updated_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_attempts_idempotency
                ON delivery_attempts(idempotency_key)
                WHERE idempotency_key != '';

                CREATE TABLE IF NOT EXISTS followups (
                    followup_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL DEFAULT '',
                    step_run_id TEXT NOT NULL DEFAULT '',
                    followup_type TEXT NOT NULL,
                    trigger_reason TEXT NOT NULL DEFAULT '',
                    current_state TEXT NOT NULL,
                    suggested_action TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    resolved_at INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_followups_root_updated
                ON followups(root_task_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS foreground_bindings (
                    session_key TEXT PRIMARY KEY,
                    foreground_root_task_id TEXT NOT NULL,
                    binding_version INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS core_events (
                    event_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL DEFAULT '',
                    step_run_id TEXT NOT NULL DEFAULT '',
                    delivery_attempt_id TEXT NOT NULL DEFAULT '',
                    followup_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    event_seq INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_core_events_root_order
                ON core_events(root_task_id, event_ts ASC, event_seq ASC, event_id ASC);

                CREATE INDEX IF NOT EXISTS idx_core_events_workflow_order
                ON core_events(workflow_run_id, event_ts ASC, event_seq ASC, event_id ASC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_core_events_idempotency
                ON core_events(idempotency_key)
                WHERE idempotency_key != '';
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(task_events)").fetchall()
            }
            if "event_key" not in columns:
                conn.execute("ALTER TABLE task_events ADD COLUMN event_key TEXT NOT NULL DEFAULT ''")
            index_rows = conn.execute("PRAGMA index_list(task_events)").fetchall()
            dedupe_index = next(
                (row for row in index_rows if row["name"] == "idx_task_events_dedupe"),
                None,
            )
            needs_rebuild = bool(dedupe_index is None or not dedupe_index["unique"])

            rows = conn.execute(
                "SELECT id, task_id, event_type, payload_json FROM task_events WHERE event_key = '' OR event_key IS NULL"
            ).fetchall()
            dedupe_map: dict[tuple[str, str, str], int] = {}
            duplicate_ids: list[int] = []
            pending_updates: list[tuple[str, int]] = []
            existing_rows = conn.execute(
                "SELECT id, task_id, event_type, event_key FROM task_events WHERE event_key != '' AND event_key IS NOT NULL"
            ).fetchall()
            for row in existing_rows:
                marker = (str(row["task_id"] or ""), str(row["event_type"] or ""), str(row["event_key"] or ""))
                if marker in dedupe_map:
                    duplicate_ids.append(int(row["id"]))
                    needs_rebuild = True
                    continue
                dedupe_map[marker] = int(row["id"])
            if rows or duplicate_ids:
                needs_rebuild = True
            for row in rows:
                event_key = hashlib.sha1(
                    f"{row['event_type']}|{row['payload_json'] or '{}'}".encode("utf-8", errors="ignore")
                ).hexdigest()
                marker = (str(row["task_id"] or ""), str(row["event_type"] or ""), event_key)
                if marker in dedupe_map:
                    duplicate_ids.append(int(row["id"]))
                    needs_rebuild = True
                    continue
                dedupe_map[marker] = int(row["id"])
                pending_updates.append((event_key, int(row["id"])))

            # 先删除唯一索引，避免更新时冲突
            conn.execute("DROP INDEX IF EXISTS idx_task_events_dedupe")
            
            if duplicate_ids:
                placeholders = ",".join("?" for _ in duplicate_ids)
                conn.execute(
                    f"DELETE FROM task_events WHERE id IN ({placeholders})",
                    duplicate_ids,
                )
            
            # 更新 event_key（此时索引已删除，不会冲突）
            for event_key, row_id in pending_updates:
                conn.execute(
                    "UPDATE task_events SET event_key = ? WHERE id = ?",
                    (event_key, row_id),
                )
            
            if needs_rebuild:
                conn.execute(
                    """
                    DELETE FROM task_events
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM task_events
                        GROUP BY task_id, event_type, event_key
                    )
                    """
                )
            
            # 最后重新创建唯一索引
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_events_dedupe
                ON task_events(task_id, event_type, event_key)
                """
            )

    def _load_kv(self, namespace: str, key: str) -> Any | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM kv_state WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["value_json"])

    def _save_kv(self, namespace: str, key: str, value: Any) -> None:
        now = int(time.time())
        payload = json.dumps(value, ensure_ascii=False)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO kv_state(namespace, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key)
                DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (namespace, key, payload, now),
            )

    def load_alerts(self, legacy_file: Path) -> dict[str, Any]:
        state = self._load_kv("runtime", "alerts")
        if state is not None:
            return state
        if legacy_file.exists():
            with open(legacy_file) as handle:
                state = json.load(handle)
            self.save_alerts(state)
            return state
        return {}

    def save_alerts(self, alerts: dict[str, Any]) -> None:
        self._save_kv("runtime", "alerts", alerts)

    def load_versions(self, legacy_file: Path) -> dict[str, Any]:
        state = self._load_kv("runtime", "versions")
        if state is not None:
            return state
        if legacy_file.exists():
            with open(legacy_file) as handle:
                state = json.load(handle)
            self.save_versions(state)
            return state
        return {"current": None, "history": []}

    def save_versions(self, versions: dict[str, Any]) -> None:
        self._save_kv("runtime", "versions", versions)

    def load_runtime_value(self, key: str, default: Any = None) -> Any:
        state = self._load_kv("runtime", key)
        return default if state is None else state

    def save_runtime_value(self, key: str, value: Any) -> None:
        self._save_kv("runtime", key, value)

    def append_runtime_event(self, key: str, event: dict[str, Any], *, limit: int = 100) -> list[dict[str, Any]]:
        events = self.load_runtime_value(key, [])
        if not isinstance(events, list):
            events = []
        normalized = dict(event or {})
        normalized.setdefault("timestamp", int(time.time()))
        events.append(normalized)
        trimmed = events[-limit:]
        self.save_runtime_value(key, trimmed)
        return trimmed

    def record_change(self, change_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        details = details or {}
        now = time.localtime()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO change_events(event_date, event_time, change_type, message, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    time.strftime("%Y-%m-%d", now),
                    time.strftime("%H:%M:%S", now),
                    change_type,
                    message,
                    json.dumps(details, ensure_ascii=False),
                ),
            )

    def list_recent_changes(self, days: int = 7, limit: int = 100) -> list[dict[str, Any]]:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT event_date, event_time, change_type, message, details_json
                FROM change_events
                WHERE event_date >= ?
                ORDER BY event_date DESC, event_time DESC, id DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [
            {
                "date": row["event_date"],
                "time": row["event_time"],
                "type": row["change_type"],
                "message": row["message"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def record_health_sample(
        self,
        *,
        process_running: bool,
        gateway_healthy: bool,
        cpu: float | int | None,
        mem_used: float | int | None,
        mem_total: float | int | None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO health_samples(recorded_at, process_running, gateway_healthy, cpu, mem_used, mem_total)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    int(process_running),
                    int(gateway_healthy),
                    cpu,
                    mem_used,
                    mem_total,
                ),
            )

    def prune_retention(self, config: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        if not bool(config.get("DB_RETENTION_ENABLED", True)):
            return {"enabled": False, "deleted": {}}

        table_specs = [
            ("heartbeats", "timestamp_ms", now - int(config.get("DB_RETENTION_HEARTBEATS_DAYS", 1)) * 86400, True),
            ("health_samples", "recorded_at", now - int(config.get("DB_RETENTION_HEALTH_SAMPLES_DAYS", 3)) * 86400, False),
            ("change_events", "event_date", time.strftime("%Y-%m-%d", time.localtime(now - int(config.get("DB_RETENTION_CHANGE_EVENTS_DAYS", 7)) * 86400)), False),
            ("task_events", "created_at", now - int(config.get("DB_RETENTION_TASK_EVENTS_DAYS", 7)) * 86400, False),
            ("watcher_tasks", "updated_at", now - int(config.get("DB_RETENTION_WATCHER_TASKS_DAYS", 7)) * 86400, False),
            ("task_control_actions", "updated_at", now - int(config.get("DB_RETENTION_TASK_CONTROL_ACTIONS_DAYS", 7)) * 86400, False),
            ("followups", "updated_at", now - int(config.get("DB_RETENTION_FOLLOWUPS_DAYS", 7)) * 86400, False),
            ("core_events", "event_ts", now - int(config.get("DB_RETENTION_CORE_EVENTS_DAYS", 14)) * 86400, False),
            ("managed_tasks", "updated_at", now - int(config.get("DB_RETENTION_MANAGED_TASKS_DAYS", 14)) * 86400, False),
            ("root_tasks", "updated_at", now - int(config.get("DB_RETENTION_ROOT_TASKS_DAYS", 14)) * 86400, False),
            ("workflow_runs", "updated_at", now - int(config.get("DB_RETENTION_WORKFLOW_RUNS_DAYS", 14)) * 86400, False),
            ("step_runs", "updated_at", now - int(config.get("DB_RETENTION_STEP_RUNS_DAYS", 14)) * 86400, False),
            ("reflection_runs", "created_at", now - int(config.get("DB_RETENTION_REFLECTION_RUNS_DAYS", 30)) * 86400, False),
        ]

        deleted: dict[str, int] = {}
        with self._connection() as conn:
            for table, column, cutoff, is_millis in table_specs:
                value = int(cutoff * 1000) if is_millis else cutoff
                cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (value,))
                deleted[table] = max(int(cursor.rowcount or 0), 0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        return {"enabled": True, "deleted": deleted, "ran_at": now}

    @staticmethod
    def _normalize_reason_code(reason: Any, *, fallback: str = UNKNOWN_REASON_CODE) -> str:
        raw = str(reason or "").strip()
        if not raw:
            return fallback
        alias = REASON_CODE_ALIASES.get(raw)
        if alias:
            return alias
        if raw in REASON_CODE_TAXONOMY:
            return raw
        return fallback

    def _normalized_metadata_with_reason(
        self,
        metadata: dict[str, Any] | None,
        *,
        original_reason: Any,
        normalized_reason: str,
    ) -> dict[str, Any]:
        payload = dict(metadata or {})
        original = str(original_reason or "").strip()
        if original and original != normalized_reason:
            payload.setdefault("original_reason_code", original)
        return payload

    def upsert_root_task(self, task: dict[str, Any]) -> None:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(task.get("state_reason"))
        metadata = self._normalized_metadata_with_reason(
            task.get("metadata"),
            original_reason=task.get("state_reason"),
            normalized_reason=normalized_reason,
        )
        payload = {
            "root_task_id": task["root_task_id"],
            "session_key": task["session_key"],
            "origin_request_id": task.get("origin_request_id", ""),
            "origin_message_id": task.get("origin_message_id", ""),
            "reply_to_message_id": task.get("reply_to_message_id", ""),
            "user_goal_summary": task.get("user_goal_summary", ""),
            "intent_type": task.get("intent_type", ""),
            "contract_type": task.get("contract_type", ""),
            "status": task.get("status", "open"),
            "state_reason": normalized_reason,
            "current_workflow_run_id": task.get("current_workflow_run_id", ""),
            "active": int(bool(task.get("active", True))),
            "foreground_priority": int(task.get("foreground_priority", 0)),
            "created_at": int(task.get("created_at", now)),
            "updated_at": int(task.get("updated_at", now)),
            "terminal_at": int(task.get("terminal_at", 0)),
            "finalized_at": int(task.get("finalized_at", 0)),
            "superseded_by_root_task_id": task.get("superseded_by_root_task_id", ""),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO root_tasks(
                    root_task_id, session_key, origin_request_id, origin_message_id, reply_to_message_id,
                    user_goal_summary, intent_type, contract_type, status, state_reason,
                    current_workflow_run_id, active, foreground_priority, created_at, updated_at,
                    terminal_at, finalized_at, superseded_by_root_task_id, metadata_json
                )
                VALUES(
                    :root_task_id, :session_key, :origin_request_id, :origin_message_id, :reply_to_message_id,
                    :user_goal_summary, :intent_type, :contract_type, :status, :state_reason,
                    :current_workflow_run_id, :active, :foreground_priority, :created_at, :updated_at,
                    :terminal_at, :finalized_at, :superseded_by_root_task_id, :metadata_json
                )
                ON CONFLICT(root_task_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    origin_request_id = excluded.origin_request_id,
                    origin_message_id = excluded.origin_message_id,
                    reply_to_message_id = excluded.reply_to_message_id,
                    user_goal_summary = excluded.user_goal_summary,
                    intent_type = excluded.intent_type,
                    contract_type = excluded.contract_type,
                    status = excluded.status,
                    state_reason = excluded.state_reason,
                    current_workflow_run_id = excluded.current_workflow_run_id,
                    active = excluded.active,
                    foreground_priority = excluded.foreground_priority,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    terminal_at = excluded.terminal_at,
                    finalized_at = excluded.finalized_at,
                    superseded_by_root_task_id = excluded.superseded_by_root_task_id,
                    metadata_json = excluded.metadata_json
                """,
                payload,
            )

    def get_root_task(self, root_task_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM root_tasks WHERE root_task_id = ?",
                (root_task_id,),
            ).fetchone()
        return self._row_to_root_task(row)

    def list_root_tasks(self, *, session_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM root_tasks"
        params: list[Any] = []
        if session_key:
            query += " WHERE session_key = ?"
            params.append(session_key)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [task for row in rows if (task := self._row_to_root_task(row))]

    def upsert_workflow_run(self, workflow: dict[str, Any]) -> None:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(workflow.get("state_reason"))
        metadata = self._normalized_metadata_with_reason(
            workflow.get("metadata"),
            original_reason=workflow.get("state_reason"),
            normalized_reason=normalized_reason,
        )
        payload = {
            "workflow_run_id": workflow["workflow_run_id"],
            "root_task_id": workflow["root_task_id"],
            "parent_workflow_run_id": workflow.get("parent_workflow_run_id", ""),
            "idempotency_key": workflow.get("idempotency_key", ""),
            "workflow_type": workflow.get("workflow_type", "direct_main"),
            "intent_type": workflow.get("intent_type", ""),
            "contract_type": workflow.get("contract_type", ""),
            "current_state": normalize_msg_state(workflow.get("current_state", "open")),
            "state_reason": normalized_reason,
            "current_step_run_id": workflow.get("current_step_run_id", ""),
            "created_at": int(workflow.get("created_at", now)),
            "updated_at": int(workflow.get("updated_at", now)),
            "started_at": int(workflow.get("started_at", 0)),
            "terminal_at": int(workflow.get("terminal_at", 0)),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs(
                    workflow_run_id, root_task_id, parent_workflow_run_id, idempotency_key,
                    workflow_type, intent_type, contract_type, current_state, state_reason,
                    current_step_run_id, created_at, updated_at, started_at, terminal_at, metadata_json
                )
                VALUES(
                    :workflow_run_id, :root_task_id, :parent_workflow_run_id, :idempotency_key,
                    :workflow_type, :intent_type, :contract_type, :current_state, :state_reason,
                    :current_step_run_id, :created_at, :updated_at, :started_at, :terminal_at, :metadata_json
                )
                ON CONFLICT(workflow_run_id) DO UPDATE SET
                    root_task_id = excluded.root_task_id,
                    parent_workflow_run_id = excluded.parent_workflow_run_id,
                    idempotency_key = excluded.idempotency_key,
                    workflow_type = excluded.workflow_type,
                    intent_type = excluded.intent_type,
                    contract_type = excluded.contract_type,
                    current_state = excluded.current_state,
                    state_reason = excluded.state_reason,
                    current_step_run_id = excluded.current_step_run_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    started_at = excluded.started_at,
                    terminal_at = excluded.terminal_at,
                    metadata_json = excluded.metadata_json
                """,
                payload,
            )

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchone()
        return self._row_to_workflow_run(row)

    def upsert_step_run(self, step: dict[str, Any]) -> None:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(step.get("state_reason"))
        metadata = self._normalized_metadata_with_reason(
            step.get("metadata"),
            original_reason=step.get("state_reason"),
            normalized_reason=normalized_reason,
        )
        payload = {
            "step_run_id": step["step_run_id"],
            "workflow_run_id": step["workflow_run_id"],
            "root_task_id": step["root_task_id"],
            "stable_step_key": step.get("stable_step_key", ""),
            "agent_id": step.get("agent_id", ""),
            "phase": step.get("phase", ""),
            "current_state": step.get("current_state", "started"),
            "state_reason": normalized_reason,
            "latest_receipt_id": step.get("latest_receipt_id", ""),
            "latest_heartbeat_seq": int(step.get("latest_heartbeat_seq", 0)),
            "last_heartbeat_at": int(step.get("last_heartbeat_at", 0)),
            "created_at": int(step.get("created_at", now)),
            "updated_at": int(step.get("updated_at", now)),
            "started_at": int(step.get("started_at", 0)),
            "terminal_at": int(step.get("terminal_at", 0)),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO step_runs(
                    step_run_id, workflow_run_id, root_task_id, stable_step_key, agent_id, phase,
                    current_state, state_reason, latest_receipt_id, latest_heartbeat_seq,
                    last_heartbeat_at, created_at, updated_at, started_at, terminal_at, metadata_json
                )
                VALUES(
                    :step_run_id, :workflow_run_id, :root_task_id, :stable_step_key, :agent_id, :phase,
                    :current_state, :state_reason, :latest_receipt_id, :latest_heartbeat_seq,
                    :last_heartbeat_at, :created_at, :updated_at, :started_at, :terminal_at, :metadata_json
                )
                ON CONFLICT(step_run_id) DO UPDATE SET
                    workflow_run_id = excluded.workflow_run_id,
                    root_task_id = excluded.root_task_id,
                    stable_step_key = excluded.stable_step_key,
                    agent_id = excluded.agent_id,
                    phase = excluded.phase,
                    current_state = excluded.current_state,
                    state_reason = excluded.state_reason,
                    latest_receipt_id = excluded.latest_receipt_id,
                    latest_heartbeat_seq = excluded.latest_heartbeat_seq,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    started_at = excluded.started_at,
                    terminal_at = excluded.terminal_at,
                    metadata_json = excluded.metadata_json
                """,
                payload,
            )

    def list_step_runs(self, workflow_run_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM step_runs
                WHERE workflow_run_id = ?
                ORDER BY created_at ASC, step_run_id ASC
                """,
                (workflow_run_id,),
            ).fetchall()
        return [step for row in rows if (step := self._row_to_step_run(row))]

    def upsert_finalizer_record(self, record: dict[str, Any]) -> None:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(record.get("trigger_reason"))
        metadata = self._normalized_metadata_with_reason(
            record.get("metadata"),
            original_reason=record.get("trigger_reason"),
            normalized_reason=normalized_reason,
        )
        payload = {
            "finalization_id": record["finalization_id"],
            "root_task_id": record["root_task_id"],
            "workflow_run_id": record["workflow_run_id"],
            "decision_state": record.get("decision_state", "pending_decision"),
            "final_status": record.get("final_status", ""),
            "trigger_reason": normalized_reason,
            "delivery_state": normalize_delivery_state(record.get("delivery_state", "undelivered")),
            "delivery_attempt_no": int(record.get("delivery_attempt_no", 0)),
            "delivery_channel": record.get("delivery_channel", ""),
            "last_delivery_error": record.get("last_delivery_error", ""),
            "user_visible_summary": record.get("user_visible_summary", ""),
            "finalized_by": record.get("finalized_by", ""),
            "finalized_at": int(record.get("finalized_at", 0)),
            "created_at": int(record.get("created_at", now)),
            "updated_at": int(record.get("updated_at", now)),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO finalizer_records(
                    finalization_id, root_task_id, workflow_run_id, decision_state, final_status,
                    trigger_reason, delivery_state, delivery_attempt_no, delivery_channel,
                    last_delivery_error, user_visible_summary, finalized_by, finalized_at,
                    created_at, updated_at, metadata_json
                )
                VALUES(
                    :finalization_id, :root_task_id, :workflow_run_id, :decision_state, :final_status,
                    :trigger_reason, :delivery_state, :delivery_attempt_no, :delivery_channel,
                    :last_delivery_error, :user_visible_summary, :finalized_by, :finalized_at,
                    :created_at, :updated_at, :metadata_json
                )
                ON CONFLICT(finalization_id) DO UPDATE SET
                    root_task_id = excluded.root_task_id,
                    workflow_run_id = excluded.workflow_run_id,
                    decision_state = excluded.decision_state,
                    final_status = excluded.final_status,
                    trigger_reason = excluded.trigger_reason,
                    delivery_state = excluded.delivery_state,
                    delivery_attempt_no = excluded.delivery_attempt_no,
                    delivery_channel = excluded.delivery_channel,
                    last_delivery_error = excluded.last_delivery_error,
                    user_visible_summary = excluded.user_visible_summary,
                    finalized_by = excluded.finalized_by,
                    finalized_at = excluded.finalized_at,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                payload,
            )

    def upsert_delivery_attempt(self, attempt: dict[str, Any]) -> None:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(attempt.get("state_reason"))
        metadata = self._normalized_metadata_with_reason(
            attempt.get("metadata"),
            original_reason=attempt.get("state_reason"),
            normalized_reason=normalized_reason,
        )
        payload = {
            "delivery_attempt_id": attempt["delivery_attempt_id"],
            "root_task_id": attempt["root_task_id"],
            "workflow_run_id": attempt["workflow_run_id"],
            "finalization_id": attempt.get("finalization_id", ""),
            "attempt_no": int(attempt.get("attempt_no", 1)),
            "channel": attempt.get("channel", ""),
            "target": attempt.get("target", ""),
            "confirmation_level": attempt.get("confirmation_level", ""),
            "current_state": normalize_delivery_state(
                attempt.get("current_state", attempt.get("delivery_state", "undelivered"))
            ),
            "state_reason": normalized_reason,
            "idempotency_key": attempt.get("idempotency_key", ""),
            "created_at": int(attempt.get("created_at", now)),
            "updated_at": int(attempt.get("updated_at", now)),
            "terminal_at": int(attempt.get("terminal_at", 0)),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO delivery_attempts(
                    delivery_attempt_id, root_task_id, workflow_run_id, finalization_id, attempt_no,
                    channel, target, confirmation_level, current_state, state_reason, idempotency_key,
                    created_at, updated_at, terminal_at, metadata_json
                )
                VALUES(
                    :delivery_attempt_id, :root_task_id, :workflow_run_id, :finalization_id, :attempt_no,
                    :channel, :target, :confirmation_level, :current_state, :state_reason, :idempotency_key,
                    :created_at, :updated_at, :terminal_at, :metadata_json
                )
                ON CONFLICT(delivery_attempt_id) DO UPDATE SET
                    root_task_id = excluded.root_task_id,
                    workflow_run_id = excluded.workflow_run_id,
                    finalization_id = excluded.finalization_id,
                    attempt_no = excluded.attempt_no,
                    channel = excluded.channel,
                    target = excluded.target,
                    confirmation_level = excluded.confirmation_level,
                    current_state = excluded.current_state,
                    state_reason = excluded.state_reason,
                    idempotency_key = excluded.idempotency_key,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    terminal_at = excluded.terminal_at,
                    metadata_json = excluded.metadata_json
                """,
                payload,
            )

    def upsert_followup(self, followup: dict[str, Any]) -> None:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(followup.get("trigger_reason"))
        metadata = self._normalized_metadata_with_reason(
            followup.get("metadata"),
            original_reason=followup.get("trigger_reason"),
            normalized_reason=normalized_reason,
        )
        payload = {
            "followup_id": followup["followup_id"],
            "root_task_id": followup["root_task_id"],
            "workflow_run_id": followup.get("workflow_run_id", ""),
            "step_run_id": followup.get("step_run_id", ""),
            "followup_type": followup.get("followup_type", ""),
            "trigger_reason": normalized_reason,
            "current_state": followup.get("current_state", "open"),
            "suggested_action": followup.get("suggested_action", ""),
            "created_by": followup.get("created_by", ""),
            "created_at": int(followup.get("created_at", now)),
            "updated_at": int(followup.get("updated_at", now)),
            "resolved_at": int(followup.get("resolved_at", 0)),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO followups(
                    followup_id, root_task_id, workflow_run_id, step_run_id, followup_type,
                    trigger_reason, current_state, suggested_action, created_by,
                    created_at, updated_at, resolved_at, metadata_json
                )
                VALUES(
                    :followup_id, :root_task_id, :workflow_run_id, :step_run_id, :followup_type,
                    :trigger_reason, :current_state, :suggested_action, :created_by,
                    :created_at, :updated_at, :resolved_at, :metadata_json
                )
                ON CONFLICT(followup_id) DO UPDATE SET
                    root_task_id = excluded.root_task_id,
                    workflow_run_id = excluded.workflow_run_id,
                    step_run_id = excluded.step_run_id,
                    followup_type = excluded.followup_type,
                    trigger_reason = excluded.trigger_reason,
                    current_state = excluded.current_state,
                    suggested_action = excluded.suggested_action,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    resolved_at = excluded.resolved_at,
                    metadata_json = excluded.metadata_json
                """,
                payload,
            )

    def get_foreground_binding(self, session_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM foreground_bindings WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return self._row_to_foreground_binding(row)

    def switch_foreground_root_task(
        self,
        *,
        session_key: str,
        next_root_task_id: str,
        reason: str,
        expected_foreground_root_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = int(time.time())
        with self._connection() as conn:
            row = conn.execute(
                "SELECT foreground_root_task_id, binding_version FROM foreground_bindings WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            current = str((row["foreground_root_task_id"] if row else "") or "")
            version = int((row["binding_version"] if row else 0) or 0)
            if expected_foreground_root_task_id is not None and current != expected_foreground_root_task_id:
                return False
            next_version = version + 1 if row else 1
            conn.execute(
                """
                INSERT INTO foreground_bindings(session_key, foreground_root_task_id, binding_version, reason, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    foreground_root_task_id = excluded.foreground_root_task_id,
                    binding_version = excluded.binding_version,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    session_key,
                    next_root_task_id,
                    next_version,
                    reason,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
        self.record_core_event(
            {
                "event_id": f"foreground-binding:{session_key}:{next_version}:{next_root_task_id}",
                "root_task_id": next_root_task_id,
                "event_type": "foreground_binding_switched",
                "event_ts": now,
                "event_seq": next_version,
                "idempotency_key": f"foreground-binding:{session_key}:{next_version}",
                "payload": {
                    "reason": "binding.switched",
                    "session_key": session_key,
                    "previous_root_task_id": current,
                    "next_root_task_id": next_root_task_id,
                    "binding_version": next_version,
                    "binding_reason": reason,
                    "metadata": metadata or {},
                },
            }
        )
        return True

    def switch_current_workflow_run(
        self,
        *,
        root_task_id: str,
        next_workflow_run_id: str,
        reason: str,
        expected_workflow_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(reason, fallback="binding.run_pointer_switched")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT current_workflow_run_id, metadata_json FROM root_tasks WHERE root_task_id = ?",
                (root_task_id,),
            ).fetchone()
            if not row:
                return False
            current_workflow_run_id = str(row["current_workflow_run_id"] or "")
            if expected_workflow_run_id is not None and current_workflow_run_id != expected_workflow_run_id:
                return False
            root_metadata = self._load_json_field(row["metadata_json"], {})
            updated_metadata = {
                **root_metadata,
                "run_pointer": {
                    "previous_workflow_run_id": current_workflow_run_id,
                    "next_workflow_run_id": next_workflow_run_id,
                    "reason_code": normalized_reason,
                    "switched_at": now,
                    "metadata": metadata or {},
                },
            }
            conn.execute(
                """
                UPDATE root_tasks
                SET current_workflow_run_id = ?, updated_at = ?, metadata_json = ?
                WHERE root_task_id = ?
                """,
                (next_workflow_run_id, now, json.dumps(updated_metadata, ensure_ascii=False), root_task_id),
            )
        self.record_core_event(
            {
                "event_id": f"run-pointer:{root_task_id}:{now}:{next_workflow_run_id}",
                "root_task_id": root_task_id,
                "workflow_run_id": next_workflow_run_id,
                "event_type": "workflow_run_pointer_switched",
                "event_ts": now,
                "event_seq": 1,
                "idempotency_key": f"run-pointer:{root_task_id}:{current_workflow_run_id}:{next_workflow_run_id}:{now}",
                "payload": {
                    "reason": normalized_reason,
                    "previous_workflow_run_id": current_workflow_run_id,
                    "next_workflow_run_id": next_workflow_run_id,
                    "metadata": metadata or {},
                },
            }
        )
        return True

    def record_retarget_event(
        self,
        *,
        source_root_task_id: str,
        workflow_run_id: str,
        target_root_task_id: str,
        reason: str,
        create_new_root: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = int(time.time())
        event_type = "retarget_to_new_root" if create_new_root else "retarget_to_existing_root"
        normalized_reason = self._normalize_reason_code(
            reason,
            fallback="binding.retarget_new_root" if create_new_root else "binding.retarget_existing_root",
        )
        return self.record_core_event(
            {
                "event_id": f"retarget:{source_root_task_id}:{target_root_task_id}:{now}:{event_type}",
                "root_task_id": source_root_task_id,
                "workflow_run_id": workflow_run_id,
                "event_type": event_type,
                "event_ts": now,
                "event_seq": 1,
                "idempotency_key": f"retarget:{source_root_task_id}:{workflow_run_id}:{target_root_task_id}:{event_type}:{now}",
                "payload": {
                    "reason": normalized_reason,
                    "source_root_task_id": source_root_task_id,
                    "target_root_task_id": target_root_task_id,
                    "metadata": metadata or {},
                },
            }
        )

    def record_correction_event(
        self,
        *,
        root_task_id: str,
        workflow_run_id: str,
        correction_type: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = int(time.time())
        normalized_reason = self._normalize_reason_code(reason, fallback="correction.applied")
        return self.record_core_event(
            {
                "event_id": f"correction:{root_task_id}:{workflow_run_id}:{correction_type}:{now}",
                "root_task_id": root_task_id,
                "workflow_run_id": workflow_run_id,
                "event_type": "correction_applied",
                "event_ts": now,
                "event_seq": 1,
                "idempotency_key": f"correction:{root_task_id}:{workflow_run_id}:{correction_type}:{now}",
                "payload": {
                    "reason": normalized_reason,
                    "correction_type": correction_type,
                    "metadata": metadata or {},
                },
            }
        )

    def record_core_event(self, event: dict[str, Any]) -> bool:
        now = int(time.time())
        event_type = str(event.get("event_type") or "")
        if event_type not in CORE_EVENT_TYPES:
            raise ValueError(f"unsupported_core_event_type:{event_type}")
        payload = dict(event.get("payload") or {})
        payload["reason"] = self._normalize_reason_code(payload.get("reason"), fallback=UNKNOWN_REASON_CODE)
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        event_id = str(event["event_id"])
        idempotency_key = str(event.get("idempotency_key") or "")
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO core_events(
                    event_id, root_task_id, workflow_run_id, step_run_id, delivery_attempt_id,
                    followup_id, event_type, event_ts, event_seq, idempotency_key, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(event.get("root_task_id") or ""),
                    str(event.get("workflow_run_id") or ""),
                    str(event.get("step_run_id") or ""),
                    str(event.get("delivery_attempt_id") or ""),
                    str(event.get("followup_id") or ""),
                    event_type,
                    int(event.get("event_ts") or now),
                    int(event.get("event_seq") or 0),
                    idempotency_key,
                    payload_json,
                    now,
                ),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def list_core_events(
        self,
        *,
        root_task_id: str | None = None,
        workflow_run_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT event_id, root_task_id, workflow_run_id, step_run_id, delivery_attempt_id,
                   followup_id, event_type, event_ts, event_seq, idempotency_key, payload_json, created_at
            FROM core_events
            WHERE 1 = 1
        """
        params: list[Any] = []
        if root_task_id:
            query += " AND root_task_id = ?"
            params.append(root_task_id)
        if workflow_run_id:
            query += " AND workflow_run_id = ?"
            params.append(workflow_run_id)
        query += " ORDER BY event_ts ASC, event_seq ASC, event_id ASC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "root_task_id": row["root_task_id"],
                "workflow_run_id": row["workflow_run_id"],
                "step_run_id": row["step_run_id"] or "",
                "delivery_attempt_id": row["delivery_attempt_id"] or "",
                "followup_id": row["followup_id"] or "",
                "event_type": row["event_type"],
                "event_ts": int(row["event_ts"] or 0),
                "event_seq": int(row["event_seq"] or 0),
                "idempotency_key": row["idempotency_key"] or "",
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": int(row["created_at"] or 0),
            }
            for row in rows
        ]

    def list_finalizer_records(self, *, root_task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM finalizer_records
            WHERE 1 = 1
        """
        params: list[Any] = []
        if root_task_id:
            query += " AND root_task_id = ?"
            params.append(root_task_id)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "finalization_id": row["finalization_id"],
                "root_task_id": row["root_task_id"],
                "workflow_run_id": row["workflow_run_id"],
                "decision_state": row["decision_state"] or "",
                "final_status": row["final_status"] or "",
                "trigger_reason": row["trigger_reason"] or "",
                "delivery_state": normalize_delivery_state(row["delivery_state"] or ""),
                "delivery_attempt_no": int(row["delivery_attempt_no"] or 0),
                "delivery_channel": row["delivery_channel"] or "",
                "last_delivery_error": row["last_delivery_error"] or "",
                "user_visible_summary": row["user_visible_summary"] or "",
                "finalized_by": row["finalized_by"] or "",
                "finalized_at": int(row["finalized_at"] or 0),
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "metadata": self._load_json_field(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def get_finalizer_record(self, finalization_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM finalizer_records WHERE finalization_id = ?",
                (finalization_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "finalization_id": row["finalization_id"],
            "root_task_id": row["root_task_id"],
            "workflow_run_id": row["workflow_run_id"],
            "decision_state": row["decision_state"] or "",
            "final_status": row["final_status"] or "",
            "trigger_reason": row["trigger_reason"] or "",
            "delivery_state": normalize_delivery_state(row["delivery_state"] or ""),
            "delivery_attempt_no": int(row["delivery_attempt_no"] or 0),
            "delivery_channel": row["delivery_channel"] or "",
            "last_delivery_error": row["last_delivery_error"] or "",
            "user_visible_summary": row["user_visible_summary"] or "",
            "finalized_by": row["finalized_by"] or "",
            "finalized_at": int(row["finalized_at"] or 0),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def list_delivery_attempts(self, *, root_task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM delivery_attempts
            WHERE 1 = 1
        """
        params: list[Any] = []
        if root_task_id:
            query += " AND root_task_id = ?"
            params.append(root_task_id)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "delivery_attempt_id": row["delivery_attempt_id"],
                "root_task_id": row["root_task_id"],
                "workflow_run_id": row["workflow_run_id"],
                "finalization_id": row["finalization_id"] or "",
                "attempt_no": int(row["attempt_no"] or 0),
                "channel": row["channel"] or "",
                "target": row["target"] or "",
                "confirmation_level": row["confirmation_level"] or "",
                "current_state": normalize_delivery_state(row["current_state"] or ""),
                "state_reason": row["state_reason"] or "",
                "idempotency_key": row["idempotency_key"] or "",
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "terminal_at": int(row["terminal_at"] or 0),
                "metadata": self._load_json_field(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def get_delivery_attempt(self, delivery_attempt_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_attempts WHERE delivery_attempt_id = ?",
                (delivery_attempt_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "delivery_attempt_id": row["delivery_attempt_id"],
            "root_task_id": row["root_task_id"],
            "workflow_run_id": row["workflow_run_id"],
            "finalization_id": row["finalization_id"] or "",
            "attempt_no": int(row["attempt_no"] or 0),
            "channel": row["channel"] or "",
            "target": row["target"] or "",
            "confirmation_level": row["confirmation_level"] or "",
            "current_state": normalize_delivery_state(row["current_state"] or ""),
            "state_reason": row["state_reason"] or "",
            "idempotency_key": row["idempotency_key"] or "",
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "terminal_at": int(row["terminal_at"] or 0),
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def list_followups(self, *, root_task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM followups
            WHERE 1 = 1
        """
        params: list[Any] = []
        if root_task_id:
            query += " AND root_task_id = ?"
            params.append(root_task_id)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "followup_id": row["followup_id"],
                "root_task_id": row["root_task_id"],
                "workflow_run_id": row["workflow_run_id"] or "",
                "step_run_id": row["step_run_id"] or "",
                "followup_type": row["followup_type"] or "",
                "trigger_reason": row["trigger_reason"] or "",
                "current_state": row["current_state"] or "",
                "suggested_action": row["suggested_action"] or "",
                "created_by": row["created_by"] or "",
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "resolved_at": int(row["resolved_at"] or 0),
                "metadata": self._load_json_field(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def get_followup(self, followup_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM followups WHERE followup_id = ?",
                (followup_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "followup_id": row["followup_id"],
            "root_task_id": row["root_task_id"],
            "workflow_run_id": row["workflow_run_id"] or "",
            "step_run_id": row["step_run_id"] or "",
            "followup_type": row["followup_type"] or "",
            "trigger_reason": row["trigger_reason"] or "",
            "current_state": normalize_msg_state(row["current_state"] or ""),
            "suggested_action": row["suggested_action"] or "",
            "created_by": row["created_by"] or "",
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "resolved_at": int(row["resolved_at"] or 0),
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def update_followup(
        self,
        followup_id: str,
        *,
        current_state: str | None = None,
        suggested_action: str | None = None,
        resolved_at: int | None = None,
        metadata_updates: dict[str, Any] | None = None,
        updated_at: int | None = None,
    ) -> None:
        existing = self.get_followup(followup_id)
        if not existing:
            return
        metadata = dict(existing.get("metadata") or {})
        if metadata_updates:
            metadata.update(metadata_updates)
        payload = {
            **existing,
            "current_state": current_state if current_state is not None else existing.get("current_state", "open"),
            "suggested_action": suggested_action if suggested_action is not None else existing.get("suggested_action", ""),
            "resolved_at": int(resolved_at if resolved_at is not None else existing.get("resolved_at", 0) or 0),
            "updated_at": int(updated_at or time.time()),
            "metadata": metadata,
        }
        self.upsert_followup(payload)

    def get_latest_foreground_binding(self) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM foreground_bindings
                ORDER BY updated_at DESC, binding_version DESC
                LIMIT 1
                """
            ).fetchone()
        return self._row_to_foreground_binding(row)

    def summarize_main_closure(self, *, limit_roots: int = 20, limit_events: int = 50) -> dict[str, Any]:
        roots = self.list_root_tasks(limit=limit_roots)
        binding = self.get_latest_foreground_binding()
        finalizers = self.list_finalizer_records(limit=200)
        delivery_attempts = self.list_delivery_attempts(limit=200)
        followups = self.list_followups(limit=200)
        finalizer_by_root = {
            str(item.get("root_task_id") or ""): item
            for item in finalizers
            if item.get("root_task_id")
        }
        delivery_by_root = {
            str(item.get("root_task_id") or ""): item
            for item in delivery_attempts
            if item.get("root_task_id")
        }
        open_followups_by_root: dict[str, list[dict[str, Any]]] = {}
        for item in followups:
            if str(item.get("current_state") or "") in {"resolved", "closed"}:
                continue
            root_id = str(item.get("root_task_id") or "")
            if not root_id:
                continue
            open_followups_by_root.setdefault(root_id, []).append(item)
        root_items: list[dict[str, Any]] = []
        active_root_count = 0
        background_root_count = 0
        finalization_pending_count = 0
        delivery_failed_count = 0
        for root in roots:
            workflow = self.get_workflow_run(str(root.get("current_workflow_run_id") or ""))
            current_state = str((workflow or {}).get("current_state") or "")
            root_id = root["root_task_id"]
            latest_finalizer = finalizer_by_root.get(root_id) or {}
            latest_delivery = delivery_by_root.get(root_id) or {}
            root_open_followups = open_followups_by_root.get(root_id) or []
            if root.get("active"):
                active_root_count += 1
            if binding and root["root_task_id"] != binding.get("foreground_root_task_id"):
                background_root_count += 1
            if current_state in {"completed", "blocked", "ambiguous_success", "delivery_pending"}:
                finalization_pending_count += 1
            if current_state in {"delivery_failed", "dlq"}:
                delivery_failed_count += 1
            root_items.append(
                {
                    "root_task_id": root["root_task_id"],
                    "session_key": root["session_key"],
                    "user_goal_summary": root["user_goal_summary"],
                    "status": root["status"],
                    "state_reason": root["state_reason"],
                    "current_workflow_run_id": root["current_workflow_run_id"],
                    "workflow_state": current_state,
                    "foreground": bool(binding and binding.get("foreground_root_task_id") == root["root_task_id"]),
                    "finalization_state": str(latest_finalizer.get("decision_state") or ""),
                    "final_status": str(latest_finalizer.get("final_status") or ""),
                    "delivery_state": str(latest_finalizer.get("delivery_state") or latest_delivery.get("current_state") or ""),
                    "delivery_confirmation_level": str(latest_delivery.get("confirmation_level") or ""),
                    "open_followup_count": len(root_open_followups),
                    "followup_types": [str(item.get("followup_type") or "") for item in root_open_followups[:5]],
                    "updated_at": root["updated_at"],
                }
            )
        open_followups = [item for item in followups if str(item.get("current_state") or "") not in {"resolved", "closed"}]
        all_events = self.list_core_events(limit=limit_events)
        late_result_count = sum(1 for item in all_events if item.get("event_type") == "late_result_recorded")
        binding_source_counts: dict[str, int] = {}
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT reason, COUNT(*) AS cnt
                FROM foreground_bindings
                GROUP BY reason
                """
            ).fetchall()
            workflow_total = int(
                conn.execute("SELECT COUNT(*) AS cnt FROM workflow_runs").fetchone()["cnt"] or 0
            )
            state_without_events_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM workflow_runs wr
                    LEFT JOIN core_events ce ON ce.workflow_run_id = wr.workflow_run_id
                    WHERE wr.current_state NOT IN ('', 'accepted')
                      AND ce.event_id IS NULL
                    """
                ).fetchone()["cnt"]
                or 0
            )
            adopted_receipt_without_step_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM core_events ce
                    LEFT JOIN step_runs sr ON sr.step_run_id = ce.step_run_id
                    WHERE ce.event_type IN ('receipt_adopted_started', 'receipt_adopted_completed', 'receipt_adopted_blocked')
                      AND (ce.step_run_id = '' OR sr.step_run_id IS NULL)
                    """
                ).fetchone()["cnt"]
                or 0
            )
            legacy_projection_root_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM root_tasks
                    WHERE intent_type = 'legacy_projection'
                       OR root_task_id LIKE 'legacy-root:%'
                    """
                ).fetchone()["cnt"]
                or 0
            )
            core_root_ids = {
                str(row["root_task_id"] or "")
                for row in conn.execute("SELECT root_task_id FROM root_tasks").fetchall()
            }
        for row in rows:
            binding_source_counts[str(row["reason"] or "unknown")] = int(row["cnt"] or 0)
        shadow_state_hit_count = 0
        unknown_reason_code_count = 0
        for task in self.list_tasks(limit=200):
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            legacy_root = self.get_root_task(self._legacy_root_task_id(task_id))
            legacy_workflow = self.get_workflow_run(self._legacy_workflow_run_id(task_id))
            control = self.derive_task_control_state(task_id)
            control_state = str(control.get("control_state") or "")
            workflow_state = str((legacy_workflow or {}).get("current_state") or "")
            if task.get("status") == "completed" and self._legacy_root_task_id(task_id) not in core_root_ids:
                shadow_state_hit_count += 1
                continue
            if control_state == "completed_verified" and workflow_state not in {"completed", "delivery_pending", "delivered"}:
                shadow_state_hit_count += 1
            elif (control_state.startswith("blocked") or control_state.endswith("_blocked")) and workflow_state not in {"blocked", "delivery_failed", "dlq", "failed"}:
                shadow_state_hit_count += 1
            elif legacy_root and str(legacy_root.get("status") or "") == "closed" and workflow_state not in {"delivered", "failed", "dlq", "cancelled"}:
                shadow_state_hit_count += 1
        with self._connection() as conn:
            unknown_reason_code_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM root_tasks WHERE state_reason = ?",
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
            unknown_reason_code_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM workflow_runs WHERE state_reason = ?",
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
            unknown_reason_code_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM step_runs WHERE state_reason = ?",
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
            unknown_reason_code_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM delivery_attempts WHERE state_reason = ?",
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
            unknown_reason_code_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM finalizer_records WHERE trigger_reason = ?",
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
            unknown_reason_code_count += int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM followups WHERE trigger_reason = ?",
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
            unknown_reason_code_count += int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM core_events
                    WHERE json_extract(payload_json, '$.reason') = ?
                    """,
                    (UNKNOWN_REASON_CODE,),
                ).fetchone()["cnt"]
                or 0
            )
        purity_gate_reasons: list[str] = []
        if state_without_events_count > 0:
            purity_gate_reasons.append("workflow_state_missing_causal_chain")
        if adopted_receipt_without_step_count > 0:
            purity_gate_reasons.append("adopted_receipt_without_step_run")
        if shadow_state_hit_count > 0:
            purity_gate_reasons.append("shadow_state_detected")
        if unknown_reason_code_count > 0:
            purity_gate_reasons.append("unknown_reason_code_detected")
        if legacy_projection_root_count > 0:
            purity_gate_reasons.append("legacy_projection_detected")
        purity_metrics = {
            "workflow_total": workflow_total,
            "state_without_causal_chain_count": state_without_events_count,
            "state_without_causal_chain_ratio": (state_without_events_count / workflow_total) if workflow_total else 0.0,
            "adopted_receipt_without_step_count": adopted_receipt_without_step_count,
            "adopted_receipt_without_step_ratio": (adopted_receipt_without_step_count / workflow_total) if workflow_total else 0.0,
            "shadow_state_hit_count": shadow_state_hit_count,
            "shadow_state_hit_ratio": (shadow_state_hit_count / workflow_total) if workflow_total else 0.0,
            "unknown_reason_code_count": unknown_reason_code_count,
            "legacy_projection_root_count": legacy_projection_root_count,
            "purity_gate_ok": not purity_gate_reasons,
            "purity_gate_reasons": purity_gate_reasons,
        }
        return {
            "foreground_root_task_id": str((binding or {}).get("foreground_root_task_id") or ""),
            "active_root_count": active_root_count,
            "background_root_count": background_root_count,
            "adoption_pending_count": len(open_followups),
            "finalization_pending_count": finalization_pending_count,
            "delivery_failed_count": delivery_failed_count,
            "late_result_count": late_result_count,
            "binding_source_counts": binding_source_counts,
            "roots": root_items,
            "finalizers": finalizers[:50],
            "delivery_attempts": delivery_attempts[:50],
            "followups": open_followups[:50],
            "events": all_events[:limit_events],
            "purity_metrics": purity_metrics,
        }

    def get_core_closure_snapshot_for_task(
        self,
        task_id: str,
        *,
        allow_legacy_projection: bool = True,
    ) -> dict[str, Any]:
        root_task_id = self._legacy_root_task_id(task_id)
        root = None
        legacy_projection_used = False
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM root_tasks
                WHERE origin_request_id = ?
                ORDER BY
                    CASE WHEN root_task_id LIKE 'legacy-root:%' THEN 1 ELSE 0 END ASC,
                    updated_at DESC,
                    created_at DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        root = self._row_to_root_task(row)
        if root:
            root_task_id = str(root.get("root_task_id") or root_task_id)
        if not root:
            root = self.get_root_task(root_task_id)
        if root and self._is_native_root_task_id(root_task_id):
            self.purge_legacy_task_projection(task_id)
        if not root and allow_legacy_projection and self.get_task(task_id):
            self.sync_legacy_task_projection(task_id)
            root = self.get_root_task(root_task_id)
            legacy_projection_used = bool(root)
        if not root:
            return {
                "root_task_id": root_task_id,
                "root_task": None,
                "current_workflow_run": None,
                "current_finalizer": None,
                "current_delivery_attempt": None,
                "current_followups": [],
                "has_core_projection": False,
                "workflow_state": "open",
                "msg_state": "open",
                "delivery_state": "undelivered",
                "delivery_confirmation_level": "",
                "finalization_state": "",
                "final_status": "",
                "is_terminal": False,
                "is_blocked": False,
                "is_delivery_pending": False,
                "needs_followup": False,
                "legacy_projection_used": False,
            }

        current_workflow_run_id = str(root.get("current_workflow_run_id") or "")
        workflow = self.get_workflow_run(current_workflow_run_id) if current_workflow_run_id else None
        finalizers = self.list_finalizer_records(root_task_id=root_task_id, limit=5)
        deliveries = self.list_delivery_attempts(root_task_id=root_task_id, limit=10)
        followups = [
            item
            for item in self.list_followups(root_task_id=root_task_id, limit=20)
            if str(item.get("current_state") or "") not in {"resolved", "closed"}
        ]
        current_finalizer = finalizers[0] if finalizers else None
        current_delivery_attempt = deliveries[0] if deliveries else None
        msg_state = normalize_msg_state((workflow or {}).get("current_state") or "open")
        delivery_state = normalize_delivery_state(
            (current_delivery_attempt or {}).get("current_state")
            or (current_delivery_attempt or {}).get("delivery_state")
            or (current_finalizer or {}).get("delivery_state")
            or "undelivered"
        )
        visible_completion_seen = self.has_task_event(task_id, "visible_completion")
        delivery_confirmation_level = str(
            (current_delivery_attempt or {}).get("confirmation_level")
            or ""
        )
        finalization_state = str((current_finalizer or {}).get("decision_state") or "")
        final_status = str((current_finalizer or {}).get("final_status") or "")
        
        # 检查 core_events 中是否有 delivery_confirmed 事件
        core_delivery_confirmed = False
        if current_workflow_run_id:
            core_events = self.list_core_events(workflow_run_id=current_workflow_run_id, limit=100)
            for ev in core_events:
                if ev.get("event_type") == "delivery_confirmed":
                    core_delivery_confirmed = True
                    delivery_state = "delivered"
                    delivery_confirmation_level = "delivery_confirmed"
                    break
        
        delivery_confirmed = delivery_state == "delivered" or core_delivery_confirmed
        is_terminal = msg_state in TERMINAL_MSG_STATES and delivery_state in TERMINAL_DELIVERY_STATES
        is_blocked = msg_state == "blocked"
        is_delivery_pending = msg_state in TERMINAL_MSG_STATES and delivery_state == "undelivered"
        return {
            "root_task_id": root_task_id,
            "root_task": root,
            "current_workflow_run": workflow,
            "current_finalizer": current_finalizer,
            "current_delivery_attempt": current_delivery_attempt,
            "current_followups": followups,
            "has_core_projection": True,
            "workflow_state": msg_state,
            "msg_state": msg_state,
            "delivery_state": delivery_state,
            "delivery_confirmation_level": delivery_confirmation_level,
            "finalization_state": finalization_state,
            "final_status": final_status,
            "is_terminal": is_terminal,
            "is_blocked": is_blocked,
            "is_delivery_pending": is_delivery_pending,
            "needs_followup": bool(followups) or (msg_state in TERMINAL_MSG_STATES and delivery_state == "undelivered"),
            "visible_completion_seen": visible_completion_seen,
            "delivery_confirmed": delivery_confirmed,
            "legacy_projection_used": legacy_projection_used,
        }

    @staticmethod
    def _core_followup_next_actor(followup: dict[str, Any]) -> str:
        metadata = dict(followup.get("metadata") or {})
        details = dict(metadata.get("details") or {})
        pipeline_recovery = dict(details.get("pipeline_recovery") or {})
        candidates = (
            metadata.get("next_actor"),
            details.get("next_actor"),
            pipeline_recovery.get("rebind_target"),
            metadata.get("rebind_target"),
            followup.get("created_by"),
        )
        for item in candidates:
            value = str(item or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _core_followup_summary(followup: dict[str, Any], workflow_state: str) -> str:
        metadata = dict(followup.get("metadata") or {})
        details = dict(metadata.get("details") or {})
        summary = str(metadata.get("summary") or "").strip()
        if summary:
            return summary
        reason = str(followup.get("trigger_reason") or workflow_state or "followup").strip()
        suggested_action = str(followup.get("suggested_action") or details.get("next_action") or "").strip()
        if suggested_action:
            return f"{reason}，建议动作={suggested_action}"
        return reason or "需要进一步跟进"

    @staticmethod
    def _is_native_root_task_id(root_task_id: str | None) -> bool:
        value = str(root_task_id or "").strip()
        return bool(value) and not value.startswith("legacy-root:")

    def _build_core_control_action(
        self,
        snapshot: dict[str, Any],
        core_supervision: dict[str, Any],
    ) -> dict[str, Any] | None:
        followups = list(snapshot.get("current_followups") or [])
        if not followups:
            return None
        lead = dict(followups[0] or {})
        metadata = dict(lead.get("metadata") or {})
        details = dict(metadata.get("details") or {})
        summary = (
            str(metadata.get("summary") or "").strip()
            or str(core_supervision.get("followup_summary") or "").strip()
            or self._core_followup_summary(lead, str(snapshot.get("workflow_state") or ""))
        )
        return {
            "id": None,
            "source": "core_followup",
            "action_type": str(lead.get("followup_type") or "followup"),
            "control_state": str(core_supervision.get("control_state") or ""),
            "status": str(lead.get("current_state") or "open"),
            "required_receipts": [],
            "summary": summary,
            "attempts": int(details.get("recovery_attempt") or metadata.get("attempts") or 0),
            "last_followup_at": int(lead.get("updated_at") or 0),
            "last_error": str(details.get("recovery_error") or metadata.get("last_error") or ""),
            "details": {
                **details,
                "source": "core_followup",
                "followup_id": str(lead.get("followup_id") or ""),
                "followup_type": str(lead.get("followup_type") or ""),
                "next_action": str(lead.get("suggested_action") or ""),
                "next_actor": self._core_followup_next_actor(lead),
                "status_template": summary,
            },
            "created_at": int(lead.get("created_at") or 0),
            "updated_at": int(lead.get("updated_at") or 0),
            "resolved_at": int(lead.get("resolved_at") or 0),
        }

    def derive_core_task_supervision(self, task_id: str) -> dict[str, Any]:
        snapshot = self.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
        if not snapshot.get("has_core_projection"):
            return {
                "truth_level": "derived",
                "workflow_state": "open",
                "msg_state": "open",
                "delivery_state": "undelivered",
                "delivery_confirmation_level": "",
                "finalization_state": "",
                "final_status": "",
                "is_terminal": False,
                "is_blocked": False,
                "is_delivery_pending": False,
                "needs_followup": False,
                "recovery_candidate": False,
                "next_action": "",
                "next_actor": "",
                "followup_summary": "",
                "followup_types": [],
                "control_state": "",
                "blocked_reason": "",
            }

        msg_state = normalize_msg_state(snapshot.get("msg_state") or snapshot.get("workflow_state") or "open")
        workflow_updated_at = int((snapshot.get("current_workflow_run") or {}).get("updated_at") or 0)
        followups: list[dict[str, Any]] = []
        for item in list(snapshot.get("current_followups") or []):
            metadata = dict(item.get("metadata") or {})
            source = str(metadata.get("source") or "")
            updated_at = int(item.get("updated_at") or 0)
            if (
                source == "legacy_control_action"
                and workflow_updated_at
                and updated_at <= workflow_updated_at
                and msg_state in {"open", "completed"}
            ):
                continue
            followups.append(item)
        lead_followup = followups[0] if followups else {}
        followup_type = str(lead_followup.get("followup_type") or "")
        next_action = str(lead_followup.get("suggested_action") or "").strip()
        next_actor = self._core_followup_next_actor(lead_followup) if lead_followup else ""
        followup_summary = self._core_followup_summary(lead_followup, msg_state) if lead_followup else ""
        blocked_reason = (
            str((snapshot.get("current_finalizer") or {}).get("last_delivery_error") or "").strip()
            or str((snapshot.get("current_workflow_run") or {}).get("state_reason") or "").strip()
            or str((lead_followup or {}).get("trigger_reason") or "").strip()
        )
        delivery_state = normalize_delivery_state(snapshot.get("delivery_state") or "undelivered")
        delivery_confirmed = delivery_state == "delivered"
        if delivery_confirmed:
            next_action = ""
            next_actor = ""
            followup_summary = ""
        recovery_candidate = (not delivery_confirmed) and bool(lead_followup) and (
            followup_type in {"pipeline_recovery", "delivery_retry", "manual_followup", "control_followup"}
            or next_action in {"manual_or_session_recovery", "delivery_retry", "manual_followup", "await_delivery_confirmation"}
            or "recovery" in next_action
            or "retry" in next_action
        )
        control_state = ""
        if msg_state == "completed" and delivery_state in TERMINAL_DELIVERY_STATES:
            control_state = "completed_verified"
        elif msg_state in TERMINAL_MSG_STATES and delivery_state == "undelivered":
            control_state = "delivery_pending" if msg_state == "completed" else "blocked_unverified"
        elif msg_state in {"blocked", "failed"}:
            control_state = (
                "blocked_control_followup_failed"
                if blocked_reason in {"control_followup_failed", "followup.control_followup_failed"}
                else "blocked_unverified"
            )
        elif msg_state == "background":
            control_state = "progress_only"
        elif followups:
            control_state = "progress_only"
        return {
            "truth_level": "core_projection",
            "workflow_state": msg_state,
            "msg_state": msg_state,
            "delivery_state": delivery_state,
            "delivery_confirmation_level": str(snapshot.get("delivery_confirmation_level") or ""),
            "finalization_state": str(snapshot.get("finalization_state") or ""),
            "final_status": str(snapshot.get("final_status") or ""),
            "is_terminal": bool(snapshot.get("is_terminal")),
            "is_blocked": bool(snapshot.get("is_blocked")),
            "is_delivery_pending": bool(snapshot.get("is_delivery_pending")),
            "needs_followup": bool(snapshot.get("needs_followup")),
            "delivery_confirmed": delivery_confirmed,
            "visible_completion_seen": bool(snapshot.get("visible_completion_seen")),
            "recovery_candidate": recovery_candidate,
            "next_action": next_action,
            "next_actor": next_actor,
            "followup_summary": followup_summary,
            "followup_types": [str(item.get("followup_type") or "") for item in followups],
            "control_state": control_state,
            "blocked_reason": blocked_reason,
        }

    def rebuild_workflow_projection(self, workflow_run_id: str) -> dict[str, Any]:
        workflow = self.get_workflow_run(workflow_run_id)
        if not workflow:
            return {
                "workflow_run_id": workflow_run_id,
                "current_state": "open",
                "state_reason": "workflow_not_found",
                "finalized": False,
                "delivered": False,
                "events_applied": 0,
            }
        events = self.list_core_events(workflow_run_id=workflow_run_id, limit=1000)
        state = normalize_msg_state(workflow.get("current_state") or "open")
        state_reason = str(workflow.get("state_reason") or "")
        finalized = False
        delivered = False
        finalization_id = ""
        delivery_attempt_id = ""
        current_step_run_id = str(workflow.get("current_step_run_id") or "")
        current_step_event_ts = 0
        finalized_at = 0
        terminal_at = 0
        delivery_state = "undelivered"
        delivery_confirmation_level = ""
        followup_ids_to_resolve: set[str] = set()
        for event in events:
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") or {}
            event_ts = int(event.get("event_ts") or 0)
            step_run_id = str(event.get("step_run_id") or "")
            if step_run_id and (
                event_ts > current_step_event_ts
                or (event_ts == current_step_event_ts and step_run_id != current_step_run_id)
            ):
                current_step_run_id = step_run_id
                current_step_event_ts = event_ts
            if event_type in {"request_accepted", "workflow_accepted"}:
                state = "open"
                state_reason = str(payload.get("reason") or event_type)
            elif event_type == "workflow_routed":
                state = "open"
                state_reason = str(payload.get("reason") or "workflow_routed")
            elif event_type in {"workflow_queued", "manual_retry_requested", "workflow_resumed"}:
                state = "open"
                state_reason = str(payload.get("reason") or event_type)
            elif event_type in {"step_started", "receipt_adopted_started"}:
                state = "open"
                state_reason = str(payload.get("reason") or event_type)
            elif event_type == "receipt_adopted_completed":
                state = "completed"
                state_reason = str(payload.get("reason") or "receipt_adopted_completed")
            elif event_type == "receipt_adopted_blocked":
                state = "blocked"
                state_reason = str(payload.get("reason") or "receipt_adopted_blocked")
            elif event_type == "workflow_failed":
                state = "failed"
                state_reason = str(payload.get("reason") or "workflow_failed")
            elif event_type == "workflow_cancelled":
                state = "failed"
                state_reason = str(payload.get("reason") or "workflow_cancelled")
            elif event_type == "ambiguous_success_detected":
                state = "failed"
                state_reason = str(payload.get("reason") or "ambiguous_success")
            elif event_type == "finalizer_finalized":
                finalized = True
                finalization_id = str(payload.get("finalization_id") or finalization_id)
                finalized_at = max(finalized_at, event_ts)
                state_reason = str(payload.get("reason") or state_reason or "finalizer_finalized")
            elif event_type in {"delivery_sent", "delivery_observed"}:
                delivery_attempt_id = str(payload.get("delivery_attempt_id") or delivery_attempt_id)
                delivery_state = "undelivered"
                delivery_confirmation_level = event_type
            elif event_type == "delivery_confirmed":
                delivered = True
                delivery_attempt_id = str(payload.get("delivery_attempt_id") or delivery_attempt_id)
                delivery_state = "delivered"
                delivery_confirmation_level = "delivery_confirmed"
                state_reason = str(payload.get("reason") or state_reason or "delivery_confirmed")
                terminal_at = max(terminal_at, event_ts)
            elif event_type == "delivery_failed":
                delivery_attempt_id = str(payload.get("delivery_attempt_id") or delivery_attempt_id)
                delivery_state = "undelivered"
                delivery_confirmation_level = "delivery_failed"
                state_reason = str(payload.get("reason") or state_reason or "delivery_failed")
            elif event_type == "delivery_dlq_entered":
                delivery_attempt_id = str(payload.get("delivery_attempt_id") or delivery_attempt_id)
                delivery_state = "owner_escalated"
                delivery_confirmation_level = "delivery_failed"
                state_reason = str(payload.get("reason") or state_reason or "delivery_dlq_entered")
                terminal_at = max(terminal_at, event_ts)
            elif event_type in {"followup_resolved", "followup_closed"}:
                followup_id = str(payload.get("followup_id") or event.get("followup_id") or "")
                if followup_id:
                    followup_ids_to_resolve.add(followup_id)
        state = normalize_msg_state(state)
        delivery_state = normalize_delivery_state(delivery_state)
        projection = {
            "workflow_run_id": workflow_run_id,
            "root_task_id": workflow.get("root_task_id") or "",
            "current_state": state,
            "state_reason": state_reason,
            "finalized": finalized,
            "delivered": delivery_state == "delivered",
            "finalization_id": finalization_id,
            "delivery_attempt_id": delivery_attempt_id,
            "current_step_run_id": current_step_run_id,
            "events_applied": len(events),
        }
        if delivery_attempt_id:
            existing_attempt = self.get_delivery_attempt(delivery_attempt_id)
            if existing_attempt:
                self.upsert_delivery_attempt(
                    {
                        **existing_attempt,
                        "confirmation_level": delivery_confirmation_level or existing_attempt.get("confirmation_level", ""),
                        "current_state": delivery_state or existing_attempt.get("current_state", ""),
                        "state_reason": state_reason or existing_attempt.get("state_reason", ""),
                        "updated_at": int(time.time()),
                        "terminal_at": terminal_at or int(existing_attempt.get("terminal_at") or 0),
                    }
                )
        if finalization_id:
            existing_finalizer = self.get_finalizer_record(finalization_id)
            if existing_finalizer:
                self.upsert_finalizer_record(
                    {
                        **existing_finalizer,
                        "decision_state": "finalized" if finalized else existing_finalizer.get("decision_state", ""),
                        "delivery_state": delivery_state or existing_finalizer.get("delivery_state", "undelivered"),
                        "last_delivery_error": state_reason
                        if delivery_state == "undelivered"
                        else existing_finalizer.get("last_delivery_error", ""),
                        "finalized_at": finalized_at or int(existing_finalizer.get("finalized_at") or 0),
                        "updated_at": int(time.time()),
                    }
                )
        if state in TERMINAL_MSG_STATES and delivery_state in TERMINAL_DELIVERY_STATES:
            for item in self.list_followups(root_task_id=str(workflow.get("root_task_id") or ""), limit=100):
                if str(item.get("workflow_run_id") or "") != workflow_run_id:
                    continue
                if str(item.get("current_state") or "") in {"resolved", "closed"}:
                    continue
                followup_ids_to_resolve.add(str(item.get("followup_id") or ""))
        for followup_id in followup_ids_to_resolve:
            existing_followup = self.get_followup(followup_id)
            if not existing_followup or str(existing_followup.get("current_state") or "") in {"resolved", "closed"}:
                continue
            self.upsert_followup(
                {
                    **existing_followup,
                    "current_state": "resolved",
                    "resolved_at": terminal_at or int(time.time()),
                    "updated_at": int(time.time()),
                    "metadata": {
                        **(existing_followup.get("metadata") or {}),
                        "resolved_by_reducer": True,
                        "resolved_workflow_state": state,
                    },
                }
            )
        self.upsert_workflow_run(
            {
                **workflow,
                "current_state": state,
                "state_reason": state_reason,
                "current_step_run_id": current_step_run_id,
                "updated_at": int(time.time()),
                "terminal_at": terminal_at if state in TERMINAL_MSG_STATES and delivery_state in TERMINAL_DELIVERY_STATES and terminal_at else int(workflow.get("terminal_at") or 0),
                "metadata": {
                    **(workflow.get("metadata") or {}),
                    "projection": {
                        "finalized": finalized,
                        "delivered": delivery_state == "delivered",
                        "delivery_state": delivery_state,
                        "delivery_confirmation_level": delivery_confirmation_level,
                        "current_step_run_id": current_step_run_id,
                        "events_applied": len(events),
                    },
                },
            }
        )
        root = self.get_root_task(str(workflow.get("root_task_id") or ""))
        if root:
            root_status = str(root.get("status") or "open")
            root_active = bool(root.get("active", True))
            root_terminal_at = int(root.get("terminal_at") or 0)
            if str(root.get("superseded_by_root_task_id") or ""):
                root_status = "superseded"
                root_active = False
            elif state in TERMINAL_MSG_STATES and delivery_state in TERMINAL_DELIVERY_STATES:
                root_status = "closed"
                root_active = False
                root_terminal_at = max(root_terminal_at, terminal_at)
            else:
                root_status = "open"
                root_active = True
            self.upsert_root_task(
                {
                    **root,
                    "status": root_status,
                    "state_reason": state_reason,
                    "current_workflow_run_id": workflow_run_id,
                    "active": root_active,
                    "updated_at": int(time.time()),
                    "terminal_at": root_terminal_at,
                    "finalized_at": max(int(root.get("finalized_at") or 0), finalized_at),
                    "metadata": {
                        **(root.get("metadata") or {}),
                        "projection": {
                            "workflow_state": state,
                            "delivery_state": delivery_state,
                            "delivery_confirmation_level": delivery_confirmation_level,
                            "current_step_run_id": current_step_run_id,
                        },
                    },
                }
            )
        return projection

    @staticmethod
    def _legacy_root_task_id(task_id: str) -> str:
        return f"legacy-root:{task_id}"

    @staticmethod
    def _legacy_workflow_run_id(task_id: str) -> str:
        return f"legacy-run:{task_id}"

    @staticmethod
    def _legacy_finalization_id(task_id: str) -> str:
        return f"legacy-finalizer:{task_id}"

    @staticmethod
    def _legacy_delivery_attempt_id(task_id: str) -> str:
        return f"legacy-delivery:{task_id}:1"

    @staticmethod
    def _legacy_step_run_id(task_id: str, agent: str, phase: str) -> str:
        return f"legacy-step:{task_id}:{agent}:{phase}"

    @staticmethod
    def _legacy_followup_id(task_id: str, source: str, suffix: str) -> str:
        return f"legacy-followup:{task_id}:{source}:{suffix}"

    def purge_legacy_task_projection(self, task_id: str) -> None:
        root_task_id = self._legacy_root_task_id(task_id)
        workflow_run_id = self._legacy_workflow_run_id(task_id)
        finalizer_id = self._legacy_finalization_id(task_id)
        delivery_attempt_id = self._legacy_delivery_attempt_id(task_id)
        followup_like = f"legacy-followup:{task_id}:%"
        step_like = f"legacy-step:{task_id}:%"
        with self._connection() as conn:
            conn.execute("DELETE FROM root_tasks WHERE root_task_id = ?", (root_task_id,))
            conn.execute("DELETE FROM workflow_runs WHERE workflow_run_id = ?", (workflow_run_id,))
            conn.execute("DELETE FROM finalizer_records WHERE finalization_id = ?", (finalizer_id,))
            conn.execute("DELETE FROM delivery_attempts WHERE delivery_attempt_id = ?", (delivery_attempt_id,))
            conn.execute("DELETE FROM step_runs WHERE step_run_id LIKE ?", (step_like,))
            conn.execute("DELETE FROM followups WHERE followup_id LIKE ?", (followup_like,))
            conn.execute(
                """
                DELETE FROM core_events
                WHERE root_task_id = ?
                   OR workflow_run_id = ?
                   OR step_run_id LIKE ?
                   OR followup_id LIKE ?
                """,
                (root_task_id, workflow_run_id, step_like, followup_like),
            )

    @staticmethod
    def _legacy_step_state(action: str) -> str:
        if action == "completed":
            return "completed"
        if action == "blocked":
            return "blocked"
        return "started"

    @staticmethod
    def _legacy_root_status(task: dict[str, Any]) -> str:
        status = str(task.get("status") or "")
        if status == "completed":
            return "closed"
        if status == "blocked":
            return "open"
        if status == "background":
            return "open"
        if status == "no_reply":
            return "open"
        return "open"

    @staticmethod
    def _legacy_workflow_state(task: dict[str, Any]) -> str:
        status = str(task.get("status") or "")
        if status == "blocked":
            return "blocked"
        if status in {"completed", "no_reply"}:
            return "completed"
        return "accepted"

    def sync_legacy_task_projection(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if not task:
            return
        root_task_id = self._legacy_root_task_id(task_id)
        workflow_run_id = self._legacy_workflow_run_id(task_id)
        root_task = {
            "root_task_id": root_task_id,
            "session_key": str(task.get("session_key") or ""),
            "origin_request_id": task_id,
            "origin_message_id": task_id,
            "user_goal_summary": str(task.get("question") or task.get("last_user_message") or ""),
            "intent_type": "legacy_projection",
            "contract_type": str((self.get_task_contract(task_id) or {}).get("id") or ""),
            "status": self._legacy_root_status(task),
            "state_reason": str(task.get("blocked_reason") or ""),
            "current_workflow_run_id": workflow_run_id,
            "active": str(task.get("status") or "") in {"running", "blocked", "background"},
            "foreground_priority": 0 if str(task.get("status") or "") != "background" else 1,
            "created_at": int(task.get("created_at") or int(time.time())),
            "updated_at": int(task.get("updated_at") or int(time.time())),
            "terminal_at": int(task.get("completed_at") or 0),
            "finalized_at": int(task.get("completed_at") or 0),
            "metadata": {"source": "legacy_task_registry", "legacy_task_id": task_id},
        }
        self.upsert_root_task(root_task)
        self.upsert_workflow_run(
            {
                "workflow_run_id": workflow_run_id,
                "root_task_id": root_task_id,
                "idempotency_key": workflow_run_id,
                "workflow_type": str((self.get_task_contract(task_id) or {}).get("id") or "legacy_projection"),
                "intent_type": "legacy_projection",
                "contract_type": str((self.get_task_contract(task_id) or {}).get("id") or ""),
                "current_state": self._legacy_workflow_state(task),
                "state_reason": str(task.get("blocked_reason") or ""),
                "created_at": int(task.get("created_at") or int(time.time())),
                "updated_at": int(task.get("updated_at") or int(time.time())),
                "started_at": int(task.get("started_at") or 0),
                "terminal_at": int(task.get("completed_at") or 0),
                "metadata": {"source": "legacy_task_registry", "legacy_task_id": task_id},
            }
        )
        if str(task.get("status") or "") != "background":
            self.switch_foreground_root_task(
                session_key=str(task.get("session_key") or ""),
                next_root_task_id=root_task_id,
                reason="legacy_task_projection",
                metadata={"legacy_task_id": task_id},
            )

        events = self.list_task_events(task_id, limit=200)
        for item in sorted(events, key=lambda event: (int(event.get("created_at") or 0), str(event.get("event_key") or ""))):
            event_type = str(item.get("event_type") or "")
            payload = item.get("payload") or {}
            created_at = int(item.get("created_at") or int(time.time()))
            event_key = str(item.get("event_key") or hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
            core_event: dict[str, Any] | None = None
            if event_type == "dispatch_started":
                core_event = {
                    "event_id": f"legacy:{task_id}:dispatch_started:{event_key}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "event_type": "workflow_accepted",
                    "event_ts": created_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:workflow_accepted:{event_key}",
                    "payload": {"reason": "dispatch_started", **payload},
                }
            elif event_type == "contract_assigned":
                core_event = {
                    "event_id": f"legacy:{task_id}:contract_assigned:{event_key}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "event_type": "workflow_routed",
                    "event_ts": created_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:workflow_routed:{event_key}",
                    "payload": {"reason": "contract_assigned", **payload},
                }
            elif event_type == "stage_progress":
                core_event = {
                    "event_id": f"legacy:{task_id}:stage_progress:{event_key}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "event_type": "workflow_queued",
                    "event_ts": created_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:workflow_queued:{event_key}",
                    "payload": {"reason": "stage_progress", **payload},
                }
            elif event_type == "pipeline_receipt":
                receipt = dict(payload.get("receipt") or {})
                agent = str(receipt.get("agent") or "unknown")
                phase = str(receipt.get("phase") or "execution")
                action = str(receipt.get("action") or "started")
                step_run_id = self._legacy_step_run_id(task_id, agent, phase)
                self.upsert_step_run(
                    {
                        "step_run_id": step_run_id,
                        "workflow_run_id": workflow_run_id,
                        "root_task_id": root_task_id,
                        "stable_step_key": f"{agent}:{phase}",
                        "agent_id": agent,
                        "phase": phase,
                        "current_state": self._legacy_step_state(action),
                        "state_reason": str(receipt.get("evidence") or ""),
                        "latest_receipt_id": f"legacy-receipt:{task_id}:{event_key}",
                        "created_at": created_at,
                        "updated_at": created_at,
                        "started_at": created_at,
                        "terminal_at": created_at if action in {"completed", "blocked"} else 0,
                        "metadata": {"source": "legacy_task_registry", "receipt": receipt},
                    }
                )
                mapped_event = {
                    "started": "receipt_adopted_started",
                    "completed": "receipt_adopted_completed",
                    "blocked": "receipt_adopted_blocked",
                }.get(action, "receipt_adopted_started")
                core_event = {
                    "event_id": f"legacy:{task_id}:pipeline_receipt:{event_key}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "step_run_id": step_run_id,
                    "event_type": mapped_event,
                    "event_ts": created_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:pipeline_receipt:{event_key}",
                    "payload": {"reason": f"pipeline_receipt:{action}", "receipt": receipt},
                }
            elif event_type == "visible_completion":
                # 减法重构：legacy visible_completion 不再自动投影为 finalizer/delivery_confirmed。
                # 文本可见性仅保留为审计线索，真正送达必须来自结构化 delivery 记录。
                core_event = None
            elif event_type == "protocol_violation":
                followup_id = self._legacy_followup_id(task_id, "protocol", event_key)
                self.upsert_followup(
                    {
                        "followup_id": followup_id,
                        "root_task_id": root_task_id,
                        "workflow_run_id": workflow_run_id,
                        "followup_type": "protocol_followup",
                        "trigger_reason": "protocol_violation",
                        "current_state": "open",
                        "suggested_action": "manual_followup",
                        "created_by": "legacy_projection",
                        "created_at": created_at,
                        "updated_at": created_at,
                        "metadata": {"source": "legacy_task_registry", **payload},
                    }
                )
                core_event = {
                    "event_id": f"legacy:{task_id}:protocol_violation:{event_key}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "event_type": "followup_requested",
                    "event_ts": created_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:followup_requested:{event_key}",
                    "payload": {
                        "reason": "protocol_violation",
                        "followup_id": followup_id,
                        **payload,
                    },
                }
            elif event_type == "background_result":
                core_event = {
                    "event_id": f"legacy:{task_id}:background_result:{event_key}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "event_type": "late_result_recorded",
                    "event_ts": created_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:late_result_recorded:{event_key}",
                    "payload": {"reason": "background_result", **payload},
                }
            if core_event:
                self.record_core_event(core_event)

        control_actions = self.list_task_control_actions(task_id=task_id, statuses=["pending", "sent", "blocked"], limit=20)
        for item in control_actions:
            followup_id = self._legacy_followup_id(task_id, "control", str(item["id"]))
            updated_at = int(item.get("updated_at") or int(time.time()))
            current_state = "open" if str(item.get("status") or "") in {"pending", "sent", "blocked"} else "resolved"
            self.upsert_followup(
                {
                    "followup_id": followup_id,
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "followup_type": str(item.get("action_type") or "control_action"),
                    "trigger_reason": str(item.get("control_state") or "control_action"),
                    "current_state": current_state,
                    "suggested_action": str((item.get("details") or {}).get("next_action") or ""),
                    "created_by": "guardian",
                    "created_at": int(item.get("created_at") or updated_at),
                    "updated_at": updated_at,
                    "resolved_at": int(item.get("resolved_at") or 0),
                    "metadata": {
                        "source": "legacy_control_action",
                        "control_action_id": item["id"],
                        "summary": item.get("summary") or "",
                        "required_receipts": item.get("required_receipts") or [],
                        "details": item.get("details") or {},
                    },
                }
            )
            event_type = "followup_requested" if current_state == "open" else "followup_resolved"
            self.record_core_event(
                {
                    "event_id": f"legacy:{task_id}:control_followup:{item['id']}:{current_state}",
                    "root_task_id": root_task_id,
                    "workflow_run_id": workflow_run_id,
                    "followup_id": followup_id,
                    "event_type": event_type,
                    "event_ts": updated_at,
                    "event_seq": 1,
                    "idempotency_key": f"legacy:{task_id}:control_followup:{item['id']}:{current_state}",
                    "payload": {
                        "reason": str(item.get("control_state") or "control_action"),
                        "followup_id": followup_id,
                        "control_action_id": item["id"],
                        "summary": item.get("summary") or "",
                        "suggested_action": str((item.get("details") or {}).get("next_action") or ""),
                    },
                }
            )
        self.rebuild_workflow_projection(workflow_run_id)

    def upsert_task(self, task: dict[str, Any]) -> None:
        now = int(time.time())
        payload = {
            "task_id": task["task_id"],
            "session_key": task["session_key"],
            "env_id": task.get("env_id", "primary"),
            "channel": task.get("channel", "unknown"),
            "status": task.get("status", "running"),
            "current_stage": task.get("current_stage", "处理中"),
            "question": task.get("question", ""),
            "last_user_message": task.get("last_user_message", task.get("question", "")),
            "blocked_reason": task.get("blocked_reason", ""),
            "latest_receipt_json": json.dumps(task.get("latest_receipt", {}), ensure_ascii=False),
            "started_at": int(task.get("started_at", now)),
            "last_progress_at": int(task.get("last_progress_at", now)),
            "created_at": int(task.get("created_at", now)),
            "updated_at": int(task.get("updated_at", now)),
            "completed_at": int(task.get("completed_at", 0)),
            "backgrounded_at": int(task.get("backgrounded_at", 0)),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO managed_tasks(
                    task_id, session_key, env_id, channel, status, current_stage, question,
                    last_user_message, blocked_reason, latest_receipt_json, started_at,
                    last_progress_at, created_at, updated_at, completed_at, backgrounded_at
                )
                VALUES(
                    :task_id, :session_key, :env_id, :channel, :status, :current_stage, :question,
                    :last_user_message, :blocked_reason, :latest_receipt_json, :started_at,
                    :last_progress_at, :created_at, :updated_at, :completed_at, :backgrounded_at
                )
                ON CONFLICT(task_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    env_id = excluded.env_id,
                    channel = excluded.channel,
                    status = excluded.status,
                    current_stage = excluded.current_stage,
                    question = excluded.question,
                    last_user_message = excluded.last_user_message,
                    blocked_reason = excluded.blocked_reason,
                    latest_receipt_json = excluded.latest_receipt_json,
                    started_at = excluded.started_at,
                    last_progress_at = excluded.last_progress_at,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    backgrounded_at = excluded.backgrounded_at
                """,
                payload,
            )

    def update_task_fields(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments: list[str] = []
        params: dict[str, Any] = {"task_id": task_id}
        for key, value in fields.items():
            if key == "latest_receipt":
                assignments.append("latest_receipt_json = :latest_receipt_json")
                params["latest_receipt_json"] = json.dumps(value or {}, ensure_ascii=False)
            else:
                assignments.append(f"{key} = :{key}")
                params[key] = value
        with self._connection() as conn:
            conn.execute(
                f"UPDATE managed_tasks SET {', '.join(assignments)} WHERE task_id = :task_id",
                params,
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM managed_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row)

    def get_latest_task_for_session(self, session_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM managed_tasks
                WHERE session_key = ?
                ORDER BY
                    CASE
                        WHEN status = 'running' THEN 0
                        WHEN status = 'background' THEN 1
                        WHEN status = 'blocked' THEN 2
                        ELSE 3
                    END,
                    created_at DESC,
                    updated_at DESC
                LIMIT 1
                """,
                (session_key,),
            ).fetchone()
        return self._row_to_task(row)

    def list_tasks(self, *, limit: int = 20, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM managed_tasks"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [task for row in rows if (task := self._row_to_task(row))]

    def list_active_tasks(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return self.list_tasks(limit=limit, statuses=["running", "blocked", "background"])

    def list_task_events(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT event_type, event_key, payload_json, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "event_key": row["event_key"] or "",
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": int(row["created_at"] or 0),
            }
            for row in rows
        ]

    def has_task_event(self, task_id: str, event_type: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM task_events
                WHERE task_id = ? AND event_type = ?
                LIMIT 1
                """,
                (task_id, event_type),
            ).fetchone()
        return bool(row)

    def count_task_events(self, event_type: str, *, env_id: str | None = None) -> int:
        query = """
            SELECT COUNT(*)
            FROM task_events te
        """
        params: list[Any] = []
        if env_id:
            query += """
                INNER JOIN managed_tasks mt
                    ON mt.task_id = te.task_id
                WHERE te.event_type = ? AND mt.env_id = ?
            """
            params.extend([event_type, env_id])
        else:
            query += " WHERE te.event_type = ?"
            params.append(event_type)
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return int((row[0] if row else 0) or 0)

    def upsert_task_contract(self, task_id: str, contract: dict[str, Any]) -> None:
        payload = json.dumps(contract or {}, ensure_ascii=False)
        now = int(time.time())
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_contracts(task_id, contract_type, contract_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    contract_type = excluded.contract_type,
                    contract_json = excluded.contract_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    str((contract or {}).get("id") or "single_agent"),
                    payload,
                    now,
                ),
            )

    def get_task_contract(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT contract_json FROM task_contracts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["contract_json"] or "{}")
        except Exception:
            return None

    def list_task_control_actions(
        self,
        *,
        task_id: str | None = None,
        env_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, task_id, env_id, action_type, control_state, status,
                   required_receipts_json, summary, attempts, last_followup_at,
                   last_error, details_json, created_at, updated_at, resolved_at
            FROM task_control_actions
            WHERE 1 = 1
        """
        params: list[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if env_id:
            query += " AND env_id = ?"
            params.append(env_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": int(row["id"] or 0),
                "task_id": row["task_id"],
                "env_id": row["env_id"],
                "action_type": row["action_type"],
                "control_state": row["control_state"],
                "status": row["status"],
                "required_receipts": json.loads(row["required_receipts_json"] or "[]"),
                "summary": row["summary"] or "",
                "attempts": int(row["attempts"] or 0),
                "last_followup_at": int(row["last_followup_at"] or 0),
                "last_error": row["last_error"] or "",
                "details": json.loads(row["details_json"] or "{}"),
                "reason": (json.loads(row["details_json"] or "{}").get("reason") if (row["details_json"] or "") else ""),
                "ack_id": (json.loads(row["details_json"] or "{}").get("ack_id") if (row["details_json"] or "") else ""),
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
                "resolved_at": int(row["resolved_at"] or 0),
            }
            for row in rows
        ]

    def get_open_control_action(self, task_id: str) -> dict[str, Any] | None:
        actions = self.list_task_control_actions(
            task_id=task_id,
            statuses=["pending", "sent", "blocked"],
            limit=1,
        )
        return actions[0] if actions else None

    @staticmethod
    def _build_contract_phase_statuses(
        contract_id: str,
        flags: dict[str, bool],
        seen_receipts: set[str],
    ) -> list[dict[str, Any]]:
        delivery_contract_ids = {"delivery_pipeline", "a_share_delivery_pipeline"}
        if contract_id in delivery_contract_ids:
            steps = [
                ("pm", "产品", "planning"),
                ("dev", "开发", "implementation"),
                ("test", "测试", "testing"),
            ]
        elif contract_id == "quant_guarded":
            steps = [
                ("calculator", "精算", "analysis"),
                ("verifier", "复核", "review"),
                ("risk", "风险", "risk"),
            ]
        else:
            steps = [
                ("main", "主任务", "execution"),
            ]

        phase_statuses: list[dict[str, Any]] = []
        for agent, label, phase in steps:
            started = f"{agent}:started" in seen_receipts
            completed = f"{agent}:completed" in seen_receipts
            blocked = f"{agent}:blocked" in seen_receipts or flags.get(f"{agent}_blocked", False)
            if blocked:
                state = "blocked"
            elif completed:
                state = "completed"
            elif started:
                state = "running"
            else:
                state = "pending"
            phase_statuses.append(
                {
                    "agent": agent,
                    "label": label,
                    "phase": phase,
                    "state": state,
                    "started": started,
                    "completed": completed,
                    "blocked": blocked,
                }
            )
        return phase_statuses

    @staticmethod
    def _render_user_visible_progress(
        contract: dict[str, Any],
        control_state: str,
        *,
        approved_summary: str,
        next_action: str,
        missing_receipts: list[str],
    ) -> str:
        custom = (contract.get("user_progress_rules") or {}).get(control_state)
        if custom:
            return str(custom)
        if control_state == "received_only":
            return "这轮任务已接收并执行过，但目前没有结构化流水线证据证明产品/研发链路继续推进。"
        if control_state == "planning_only":
            return "方案已完成，但开发尚未启动。" if next_action == "require_dev_receipt" else "产品阶段已启动，等待方案回执。"
        if control_state == "dev_running":
            return "开发阶段已启动，存在结构化执行证据。"
        if control_state == "awaiting_test":
            return "开发回执已完成，但测试尚未启动。"
        if control_state == "test_running":
            return "测试阶段已启动，等待最终测试回执。"
        if control_state in {"blocked_unverified", "blocked_control_followup_failed"}:
            missing = "、".join(missing_receipts) if missing_receipts else "结构化回执"
            return f"任务缺少{missing}，守护系统已判定为阻塞。"
        return approved_summary

    @staticmethod
    def _infer_pipeline_recovery(
        contract_id: str,
        flags: dict[str, bool],
        missing_receipts: list[str],
        *,
        latest_receipt: dict[str, Any],
        current_stage: str,
        task_status: str,
    ) -> dict[str, Any]:
        delivery_contract_ids = {"delivery_pipeline", "a_share_delivery_pipeline"}
        if contract_id not in delivery_contract_ids:
            return {}
        current_lower = (current_stage or "").lower()
        last_agent = str(latest_receipt.get("agent") or "")
        if not last_agent:
            if "planning" in current_lower:
                last_agent = "pm"
            elif "implementation" in current_lower or "dev" in current_lower:
                last_agent = "dev"
            elif "test" in current_lower:
                last_agent = "test"
        recovery_kind = ""
        recovery_hint = ""
        stale_subagent = ""
        rebind_target = ""
        downstream_started = flags["dev_started"] or flags["dev_completed"] or flags["test_started"] or flags["test_completed"]
        test_phase_started = flags["test_started"] or flags["test_completed"]
        if "pm:started" in missing_receipts and not downstream_started:
            recovery_kind = "not_started"
            recovery_hint = "主任务已接收，但产品阶段尚未启动，应先确认 pm 是否收到调度。"
            rebind_target = "pm"
        elif flags["pm_started"] and "pm:completed" in missing_receipts and not downstream_started:
            recovery_kind = "started_no_receipt"
            recovery_hint = "pm 已启动但没有结构化回执，建议做 session recovery 并确认规划结果是否已丢失。"
            stale_subagent = "pm"
            rebind_target = "pm"
        elif (flags["dev_started"] or "implementation" in current_lower) and "dev:completed" in missing_receipts:
            recovery_kind = "pipeline_detached"
            recovery_hint = "开发阶段看起来已启动，但主链路没有拿到 dev 结构化回执，应优先做 stale subagent detection 和 active task rebind。"
            stale_subagent = last_agent or "dev"
            rebind_target = "dev"
        elif flags["pm_completed"] and "dev:started" in missing_receipts and not downstream_started:
            recovery_kind = "handoff_lost"
            recovery_hint = "pm 已完成，但 dev 未回执，需检查 dev 是否接到派发或主链路是否丢失 handoff。"
            stale_subagent = last_agent or "dev"
            rebind_target = "dev"
        elif flags["dev_completed"] and "test:started" in missing_receipts and not test_phase_started:
            recovery_kind = "handoff_lost"
            recovery_hint = "dev 已完成，但 test 未启动，需恢复 dev -> test 的流水线接力。"
            stale_subagent = last_agent or "test"
            rebind_target = "test"
        elif (flags["test_started"] or "test" in current_lower) and "test:completed" in missing_receipts:
            recovery_kind = "started_no_receipt"
            recovery_hint = "测试阶段已启动但最终回执缺失，应检查 test 子代理是否失联。"
            stale_subagent = last_agent or "test"
            rebind_target = "test"
        elif task_status == "no_reply":
            recovery_kind = "completed_not_returned"
            recovery_hint = "任务可能已完成但结果未回传，应确认 final 输出是否丢失并执行 active task rebind。"
            stale_subagent = last_agent or "main"
            rebind_target = last_agent or "main"
        if not recovery_kind:
            return {}
        return {
            "kind": recovery_kind,
            "last_dispatched_agent": last_agent or "unknown",
            "missing_receipts": missing_receipts,
            "stale_subagent": stale_subagent or "unknown",
            "rebind_target": rebind_target or "guardian",
            "manual_recovery_hint": recovery_hint,
            "session_recovery": True,
            "stale_subagent_detection": bool(stale_subagent),
            "active_task_rebind": bool(rebind_target),
        }

    def reconcile_task_control_action(
        self,
        task: dict[str, Any],
        control: dict[str, Any],
    ) -> dict[str, Any] | None:
        now = int(time.time())
        task_id = task["task_id"]
        env_id = str(task.get("env_id") or "primary")
        core_snapshot = self.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
        root_task_id = str((core_snapshot.get("root_task") or {}).get("root_task_id") or "")
        next_action = str(control.get("next_action") or "none")
        summary = str(control.get("approved_summary") or "")
        missing = list(control.get("missing_receipts") or [])
        control_state = str(control.get("control_state") or "unknown")
        details_payload = {
            "contract_id": ((control.get("contract") or {}).get("id") or "single_agent"),
            "protocol_version": ((control.get("contract") or {}).get("protocol_version") or "hm.v1"),
            "next_action": next_action,
            "next_actor": control.get("next_actor") or "",
            # Phase 4 简化：删除 claim_level
            "phase_statuses": control.get("phase_statuses") or [],
            "reason": control.get("action_reason") or control.get("approved_summary") or "",
            "ack_id": ((control.get("protocol") or {}).get("ack_id") or ""),
            "pipeline_recovery": control.get("pipeline_recovery") or {},
        }
        existing = self.list_task_control_actions(
            task_id=task_id,
            statuses=["pending", "sent", "blocked"],
            limit=20,
        )

        if self._is_native_root_task_id(root_task_id):
            with self._connection() as conn:
                conn.execute(
                    """
                    UPDATE task_control_actions
                    SET status = 'resolved', summary = ?, control_state = ?, updated_at = ?, resolved_at = ?
                    WHERE task_id = ? AND status IN ('pending', 'sent', 'blocked')
                    """,
                    (summary, control_state, now, now, task_id),
                )
            return control.get("control_action")

        if next_action == "none":
            with self._connection() as conn:
                conn.execute(
                    """
                    UPDATE task_control_actions
                    SET status = ?, summary = ?, control_state = ?, updated_at = ?, resolved_at = ?
                    WHERE task_id = ? AND status IN ('pending', 'sent', 'blocked')
                    """,
                    ("resolved", summary, control_state, now, now, task_id),
                )
            return None

        current = next((item for item in existing if item["action_type"] == next_action), None)
        stale_ids = [item["id"] for item in existing if item["action_type"] != next_action]
        with self._connection() as conn:
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"""
                    UPDATE task_control_actions
                    SET status = 'resolved', updated_at = ?, resolved_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now, now, *stale_ids],
                )
            if current:
                conn.execute(
                    """
                    UPDATE task_control_actions
                    SET env_id = ?, control_state = ?, required_receipts_json = ?, summary = ?, details_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        env_id,
                        control_state,
                        json.dumps(missing, ensure_ascii=False),
                        summary,
                        json.dumps(details_payload, ensure_ascii=False),
                        now,
                        current["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO task_control_actions(
                        task_id, env_id, action_type, control_state, status,
                        required_receipts_json, summary, attempts, last_followup_at,
                        last_error, details_json, created_at, updated_at, resolved_at
                    )
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, 0, 0, '', ?, ?, ?, 0)
                    """,
                    (
                        task_id,
                        env_id,
                        next_action,
                        control_state,
                        json.dumps(missing, ensure_ascii=False),
                        summary,
                        json.dumps(details_payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        return self.get_open_control_action(task_id)

    def update_control_action(
        self,
        action_id: int,
        *,
        status: str | None = None,
        attempts: int | None = None,
        last_followup_at: int | None = None,
        last_error: str | None = None,
        summary: str | None = None,
        control_state: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        fields: list[str] = ["updated_at = :updated_at"]
        params: dict[str, Any] = {"action_id": action_id, "updated_at": int(time.time())}
        if status is not None:
            fields.append("status = :status")
            params["status"] = status
            if status in {"resolved", "blocked"}:
                fields.append("resolved_at = :resolved_at")
                params["resolved_at"] = int(time.time())
        if attempts is not None:
            fields.append("attempts = :attempts")
            params["attempts"] = attempts
        if last_followup_at is not None:
            fields.append("last_followup_at = :last_followup_at")
            params["last_followup_at"] = last_followup_at
        if last_error is not None:
            fields.append("last_error = :last_error")
            params["last_error"] = last_error
        if summary is not None:
            fields.append("summary = :summary")
            params["summary"] = summary
        if control_state is not None:
            fields.append("control_state = :control_state")
            params["control_state"] = control_state
        if details is not None:
            fields.append("details_json = :details_json")
            params["details_json"] = json.dumps(details, ensure_ascii=False)
        with self._connection() as conn:
            conn.execute(
                f"UPDATE task_control_actions SET {', '.join(fields)} WHERE id = :action_id",
                params,
            )

    def create_control_action(
        self,
        task_id: str,
        env_id: str,
        action_type: str,
        *,
        control_state: str = "unknown",
        status: str = "pending",
        summary: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        details_json = json.dumps(details or {}, ensure_ascii=False)
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_control_actions(
                    task_id, env_id, action_type, control_state, status,
                    required_receipts_json, summary, attempts, last_followup_at,
                    last_error, details_json, created_at, updated_at, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, '[]', ?, 0, 0, '', ?, ?, ?, 0)
                """,
                (
                    task_id,
                    env_id,
                    action_type,
                    control_state,
                    status,
                    summary,
                    details_json,
                    now,
                    now,
                ),
            )
            action_id = cursor.lastrowid
        return self.get_open_control_action(task_id) or {"id": action_id, "task_id": task_id}

    @staticmethod
    def _task_label_invalid(text: str | None) -> bool:
        raw = (text or "").strip()
        if not raw or raw == "未知任务":
            return True
        lower = raw.lower()
        invalid_markers = (
            "dispatching to agent",
            "dispatch complete",
            "received message from ",
        )
        return any(marker in lower for marker in invalid_markers)

    def get_task_question_candidate(self, task_id: str) -> str | None:
        events = self.list_task_events(task_id, limit=20)
        for event in events:
            if event.get("event_type") != "dispatch_started":
                continue
            payload = event.get("payload") or {}
            candidate = str(payload.get("question") or "").strip()
            if not self._task_label_invalid(candidate):
                return candidate
        return None

    def repair_task_identity(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        candidate = self.get_task_question_candidate(task_id)
        if not candidate:
            return False

        fields: dict[str, Any] = {}
        if self._task_label_invalid(task.get("question")):
            fields["question"] = candidate
        if self._task_label_invalid(task.get("last_user_message")):
            fields["last_user_message"] = candidate
        if not fields:
            return False
        fields["updated_at"] = max(int(task.get("updated_at") or 0), int(time.time()))
        self.update_task_fields(task_id, **fields)
        return True

    @staticmethod
    def _infer_active_phase(task: dict[str, Any], latest_receipt: dict[str, Any], control_state: str) -> str:
        receipt_phase = str(latest_receipt.get("phase") or "").strip().lower()
        if receipt_phase in DEFAULT_PHASE_POLICIES:
            return receipt_phase
        stage = str(task.get("current_stage") or "").strip().lower()
        for phase in DEFAULT_PHASE_POLICIES:
            if phase in stage:
                return phase
        mapping = {
            "planning_only": "planning",
            "dev_running": "implementation",
            "awaiting_test": "testing",
            "test_running": "testing",
            "calculator_running": "calculation",
            "awaiting_verifier": "verification",
            "received_only": "planning",
            "progress_only": "implementation",
        }
        return mapping.get(control_state, "planning")

    @staticmethod
    def _resolve_timing_metadata(contract: dict[str, Any], phase: str) -> dict[str, Any]:
        policies = dict(DEFAULT_PHASE_POLICIES)
        policies.update(contract.get("phase_policies") or {})
        profile = str(policies.get(phase) or "medium")
        profiles = dict(DEFAULT_DURATION_PROFILES)
        profiles.update(contract.get("duration_profiles") or {})
        timing = dict(DEFAULT_DURATION_PROFILES.get(profile, DEFAULT_DURATION_PROFILES["medium"]))
        timing.update((profiles.get(profile) or {}))
        first_ack_sla = int(timing.get("first_ack_sla") or 0)
        heartbeat_interval = int(timing.get("heartbeat_interval") or 0)
        hard_timeout = int(timing.get("hard_timeout") or 0)
        timing["soft_followup"] = int(timing.get("soft_followup") or first_ack_sla)
        timing["hard_followup"] = int(timing.get("hard_followup") or min(hard_timeout, first_ack_sla + heartbeat_interval))
        timing["auto_blocked_unverified"] = int(timing.get("auto_blocked_unverified") or hard_timeout)
        timing["blocked_user_visible"] = bool(timing.get("blocked_user_visible", True))
        timing["profile"] = profile
        timing["phase"] = phase
        return timing

    @staticmethod
    def _derive_followup_stage(task: dict[str, Any], control_action: dict[str, Any] | None, timing: dict[str, Any], *, now: int) -> str | None:
        attempts = int((control_action or {}).get("attempts") or 0)
        action_status = str((control_action or {}).get("status") or "")
        if attempts >= 2 or str(task.get("blocked_reason") or "") in {"missing_pipeline_receipt", "control_followup_failed"}:
            return "blocked"
        started_at = int(task.get("started_at") or now)
        last_progress_at = int(task.get("last_progress_at") or started_at)
        first_ack_sla = int(timing.get("first_ack_sla") or 0)
        heartbeat_interval = int(timing.get("heartbeat_interval") or 0)
        hard_timeout = int(timing.get("hard_timeout") or 0)
        since_start = max(0, now - started_at)
        since_progress = max(0, now - last_progress_at)
        if hard_timeout and (since_start >= hard_timeout or since_progress >= hard_timeout):
            if action_status in {"sent", "blocked"} or attempts > 0:
                return "blocked"
            return "soft"
        if attempts >= 1 or (action_status in {"sent", "blocked"} and first_ack_sla and since_progress >= first_ack_sla + heartbeat_interval):
            return "hard"
        if first_ack_sla and (since_start >= first_ack_sla or since_progress >= heartbeat_interval):
            return "soft"
        return None

    def _derive_v2_truth_snapshot(
        self,
        *,
        contract: dict[str, Any],
        flags: dict[str, bool],
        seen_receipts: set[str],
        task_status: str,
        blocked_reason: str,
    ) -> dict[str, Any]:
        terminal_receipts = list(contract.get("terminal_receipts") or [])
        terminal_seen = [item for item in terminal_receipts if item in seen_receipts]
        has_structured_start = bool(flags.get("pipeline_receipt"))
        has_structured_completion = any(
            item.endswith(":completed") and item in seen_receipts for item in terminal_receipts
        )
        delivered = bool(terminal_seen) and task_status == "completed"
        if blocked_reason == "missing_pipeline_receipt":
            v2_state = "blocked"
        elif delivered:
            v2_state = "delivered"
        elif has_structured_completion:
            v2_state = "awaiting_delivery"
        elif has_structured_start:
            v2_state = "confirmed"
        elif flags.get("dispatch_started") or flags.get("dispatch_completed"):
            v2_state = "received"
        else:
            v2_state = "unknown"
        return {
            "state": v2_state,
            "request_seen": bool(flags.get("dispatch_started") or flags.get("dispatch_completed")),
            "confirmed": has_structured_start,
            "completed": has_structured_completion,
            "delivered": delivered,
            "terminal_receipts_expected": terminal_receipts,
            "terminal_receipts_seen": terminal_seen,
        }

    def derive_task_control_state(self, task_id: str) -> dict[str, Any]:
        """
        控制面核心判定函数：基于证据推导任务控制状态。

        边界原则：
        - OpenClaw 负责发 receipt / progress / final（执行面主张）
        - helper 负责判断这些主张是否足以升级为控制面事实
        - OpenClaw 不能自证 verified
        - helper 不能替 OpenClaw 做业务编排

        字段归属：
        - control_state: helper 最终裁定
        - claim_level: helper 计算
        - missing_receipts: helper 计算
        - next_action: helper 决定
        - next_actor: helper 决定
        - approved_summary: helper 生成
        """
        task = self.get_task(task_id)
        if not task:
            return {
                "evidence_level": "none",
                "control_state": "unknown",
                "approved_summary": "任务不存在",
                "next_action": "none",
                "next_actor": "",
                "contract": {"id": "single_agent", "required_receipts": []},
                "missing_receipts": [],
                "control_action": None,
                "phase_statuses": [],
                "flags": {},
            }

        events = self.list_task_events(task_id, limit=50)
        core_snapshot = self.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
        core_supervision = self.derive_core_task_supervision(task_id)
        if not core_snapshot.get("has_core_projection"):
            self.sync_legacy_task_projection(task_id)
            core_snapshot = self.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
            core_supervision = self.derive_core_task_supervision(task_id)
        contract = self.get_task_contract(task_id) or {
            "id": "single_agent",
            "required_receipts": [],
        }
        contract_view = dict(contract)
        contract_view.setdefault("mode", "observation_template")
        root_task = dict(core_snapshot.get("root_task") or {})
        native_root_task_id = str(root_task.get("root_task_id") or "")
        if native_root_task_id and not native_root_task_id.startswith("legacy-root:"):
            workflow = dict(core_snapshot.get("current_workflow_run") or {})
            finalizer = dict(core_snapshot.get("current_finalizer") or {})
            delivery_attempt = dict(core_snapshot.get("current_delivery_attempt") or {})
            msg_state = normalize_msg_state(core_supervision.get("msg_state") or core_supervision.get("workflow_state") or "open")
            delivery_state = normalize_delivery_state(core_supervision.get("delivery_state") or "undelivered")
            active_phase = str(workflow.get("phase") or msg_state or task.get("current_stage") or "")
            timing = self._resolve_timing_metadata(contract_view, active_phase)
            step_updated_at = int(workflow.get("updated_at") or task.get("last_progress_at") or task.get("started_at") or 0)
            now = int(time.time())
            heartbeat_age = max(0, now - step_updated_at) if step_updated_at else 0
            heartbeat_ok = heartbeat_age <= int(timing.get("heartbeat_interval") or 0)
            followup_stage = "healthy"
            if core_supervision.get("is_blocked"):
                followup_stage = "blocked"
            elif core_supervision.get("needs_followup"):
                followup_stage = "soft"
            control_state = str(core_supervision.get("control_state") or "")
            if not control_state:
                if msg_state == "open":
                    control_state = "progress_only"
                elif msg_state == "completed" and delivery_state in TERMINAL_DELIVERY_STATES:
                    control_state = "completed_verified"
                elif msg_state == "completed":
                    control_state = "delivery_pending"
                elif msg_state in {"blocked", "failed"}:
                    control_state = (
                        "blocked_control_followup_failed"
                        if str(core_supervision.get("blocked_reason") or "") in {"control_followup_failed", "followup.control_followup_failed"}
                        else "blocked_unverified"
                    )
                else:
                    control_state = "received_only"
            # 如果有 visible_completion 事件，至少应该是 delivery_pending
            visible_completion_seen = bool(core_supervision.get("visible_completion_seen"))
            if visible_completion_seen and control_state == "received_only":
                if delivery_state in TERMINAL_DELIVERY_STATES:
                    control_state = "completed_verified"
                else:
                    control_state = "delivery_pending"
            delivery_confirmed = bool(core_supervision.get("delivery_confirmed"))
            approved_summary = (
                str(core_supervision.get("followup_summary") or "").strip()
                or str(finalizer.get("user_visible_summary") or "").strip()
                or (
                    "主闭环已完成并确认送达。"
                    if msg_state == "completed" and delivery_state in TERMINAL_DELIVERY_STATES
                    else "主闭环最终结论已形成，当前等待送达确认。"
                    if msg_state in TERMINAL_MSG_STATES and delivery_state == "undelivered"
                    else "主闭环当前处于阻塞状态。"
                    if core_supervision.get("is_blocked")
                    else "主闭环已进入原生工作流。"
                )
            )
            next_action = str(core_supervision.get("next_action") or "")
            next_actor = str(core_supervision.get("next_actor") or "")
            if msg_state in TERMINAL_MSG_STATES and delivery_state in TERMINAL_DELIVERY_STATES:
                next_action = "none"
                next_actor = ""
            elif msg_state in TERMINAL_MSG_STATES and delivery_state == "undelivered" and not next_action:
                next_action = "await_delivery_confirmation"
                next_actor = "watchdog"
            elif core_supervision.get("is_blocked") and not next_action:
                next_action = "manual_or_session_recovery"
            # Phase 4 简化：删除 claim_level
            protocol_status = {
                "request": "seen",
                "confirmed": "seen",
                "final": "seen" if msg_state in TERMINAL_MSG_STATES else "missing",
                "blocked": "seen" if core_supervision.get("is_blocked") else "missing",
                "ack_id": str((workflow.get("current_step_run_id") or workflow.get("workflow_run_id") or task_id) or ""),
            }
            native_state = {
                "source": "core_events",
                "dispatch_started": True,
                "dispatch_completed": msg_state != "open",
                "pipeline_progress_seen": msg_state in {"open", "completed", "blocked", "failed"},
                "pipeline_receipt_seen": msg_state in TERMINAL_MSG_STATES,
                "latest_receipt": {},
                "status": str(task.get("status") or ""),
                "stage": active_phase,
            }
            derived_state = {
                "control_state": control_state,
                "approved_summary": approved_summary,
                "next_action": next_action,
                "next_actor": next_actor,
                "missing_receipts": [],
                "contract_id": str(contract_view.get("id") or "single_agent"),
                "v2_state": msg_state or "unknown",
            }
            # Phase 4 简化：删除 claim_level 和 public_control_state
            evidence_summary = (
                f"workflow_state={msg_state or 'unknown'}; finalization_state={core_snapshot.get('finalization_state') or '-'}; "
                f"delivery_state={core_snapshot.get('delivery_state') or '-'}; delivery_confirmation={core_snapshot.get('delivery_confirmation_level') or '-'}; "
                f"next_actor={next_actor or '-'}; action={next_action or '-'}; phase={active_phase or '-'}; "
                f"heartbeat_age={heartbeat_age}; followup_stage={followup_stage}"
            )
            return {
                "evidence_level": "strong",
                "evidence_summary": evidence_summary,
                "control_state": control_state,
                "approved_summary": approved_summary,
                "next_action": next_action,
                "next_actor": next_actor,
                "user_visible_progress": approved_summary,
                "protocol": protocol_status,
                "contract": contract_view,
                "missing_receipts": [],
                "control_action": self._build_core_control_action(core_snapshot, core_supervision),
                "phase_statuses": [],
                "flags": {},
                "latest_receipt": {},
                "pipeline_recovery": {},
                "latest_recovery": {},
                "latest_protocol_violation": {},
                "native_state": native_state,
                "derived_state": derived_state,
                "v2_truth": {
                    "state": msg_state or "unknown",
                    "reason": str(workflow.get("state_reason") or ""),
                },
                "heuristic_state": {},
                "core_supervision": core_supervision,
                "timing": timing,
                "active_phase": active_phase,
                "followup_stage": followup_stage,
                "heartbeat_age_seconds": heartbeat_age,
                "heartbeat_ok": heartbeat_ok,
                "terminal_state_seen": bool(core_supervision.get("is_terminal") or core_supervision.get("is_blocked") or delivery_confirmed),
            }
        flags = {
            "dispatch_started": False,
            "dispatch_completed": False,
            "visible_completion": False,
            "pipeline_progress": False,
            "pipeline_receipt": False,
            "pm_started": False,
            "pm_completed": False,
            "dev_started": False,
            "dev_completed": False,
            "dev_blocked": False,
            "test_started": False,
            "test_completed": False,
            "test_blocked": False,
            "calculator_started": False,
            "calculator_completed": False,
            "calculator_blocked": False,
            "verifier_started": False,
            "verifier_completed": False,
            "verifier_blocked": False,
            "risk_started": False,
            "risk_completed": False,
            "risk_blocked": False,
        }
        latest_receipt: dict[str, Any] = task.get("latest_receipt") or {}
        seen_receipts: set[str] = set()
        latest_recovery_success: dict[str, Any] = {}
        latest_protocol_violation: dict[str, Any] = {}

        def apply_receipt(receipt: dict[str, Any]) -> None:
            if not receipt:
                return
            flags["pipeline_receipt"] = True
            agent = str(receipt.get("agent") or "")
            action = str(receipt.get("action") or "")
            if agent and action:
                seen_receipts.add(f"{agent}:{action}")
            if agent and action in {"completed", "blocked"}:
                seen_receipts.add(f"{agent}:started")
            if agent == "pm" and action == "completed":
                flags["pm_started"] = True
                flags["pm_completed"] = True
            if agent == "pm" and action == "started":
                flags["pm_started"] = True
            if agent == "dev":
                flags["pm_started"] = True
                flags["pm_completed"] = True
                seen_receipts.add("pm:started")
                seen_receipts.add("pm:completed")
                if action == "started":
                    flags["dev_started"] = True
                elif action == "completed":
                    flags["dev_started"] = True
                    flags["dev_completed"] = True
                elif action == "blocked":
                    flags["dev_started"] = True
                    flags["dev_blocked"] = True
            if agent == "test":
                flags["pm_started"] = True
                flags["pm_completed"] = True
                flags["dev_started"] = True
                flags["dev_completed"] = True
                seen_receipts.add("pm:started")
                seen_receipts.add("pm:completed")
                seen_receipts.add("dev:started")
                seen_receipts.add("dev:completed")
                if action == "started":
                    flags["test_started"] = True
                elif action == "completed":
                    flags["test_started"] = True
                    flags["test_completed"] = True
                elif action == "blocked":
                    flags["test_started"] = True
                    flags["test_blocked"] = True
            if agent == "calculator":
                if action == "started":
                    flags["calculator_started"] = True
                elif action == "completed":
                    flags["calculator_started"] = True
                    flags["calculator_completed"] = True
                elif action == "blocked":
                    flags["calculator_started"] = True
                    flags["calculator_blocked"] = True
            if agent == "verifier":
                if action == "started":
                    flags["verifier_started"] = True
                elif action == "completed":
                    flags["verifier_started"] = True
                    flags["verifier_completed"] = True
                elif action == "blocked":
                    flags["verifier_started"] = True
                    flags["verifier_blocked"] = True
            if agent == "risk":
                if action == "started":
                    flags["risk_started"] = True
                elif action == "completed":
                    flags["risk_started"] = True
                    flags["risk_completed"] = True
                elif action == "blocked":
                    flags["risk_started"] = True
                    flags["risk_blocked"] = True

        apply_receipt(latest_receipt)

        for event in events:
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") or {}
            if event_type == "dispatch_started":
                flags["dispatch_started"] = True
            elif event_type == "dispatch_complete":
                flags["dispatch_completed"] = True
            elif event_type == "visible_completion":
                # 减法重构：visible_completion 只保留为审计信号，不再进入终态裁决。
                pass
            elif event_type == "stage_progress":
                flags["pipeline_progress"] = True
            elif event_type == "pipeline_receipt":
                latest_receipt = payload.get("receipt") or latest_receipt
                apply_receipt(payload.get("receipt") or {})
            elif event_type == "recovery_succeeded" and not latest_recovery_success:
                latest_recovery_success = payload
            elif event_type == "protocol_violation" and not latest_protocol_violation:
                latest_protocol_violation = payload

        evidence_level = "weak"
        if flags["pipeline_receipt"]:
            evidence_level = "strong"
        elif flags["pipeline_progress"]:
            evidence_level = "moderate"

        control_state = "received_only"
        approved_summary = "任务已接收并执行过，但没有结构化流水线证据。"
        next_action = "require_receipt_or_block"
        next_actor = ""

        blocked_reason = str(task.get("blocked_reason") or "")
        task_blocked_reason = blocked_reason
        blocked_state_locked = False
        task_status = str(task.get("status") or "")
        contract_id = str(contract.get("id") or "single_agent")
        single_agent_terminal_reply = contract_id == "single_agent" and flags["dispatch_completed"]
        
        # Bug 修复：如果 single-agent 已有结论性 dispatch_complete，
        # 不再允许因为缺少 pipeline receipt 被反向锁成 blocked。
        if task_status == "blocked" and not single_agent_terminal_reply:
            if blocked_reason == "missing_pipeline_receipt":
                control_state = "blocked_unverified"
                approved_summary = "任务缺少结构化流水线回执，守护系统已判定为阻塞。"
                next_action = "manual_or_session_recovery"
                next_actor = "guardian"
                blocked_state_locked = True
            elif blocked_reason == "control_followup_failed":
                control_state = "blocked_control_followup_failed"
                approved_summary = "守护系统尝试接回任务，但控制追问失败，任务已判定为阻塞。"
                next_action = "manual_or_session_recovery"
                next_actor = "guardian"
                blocked_state_locked = True
            else:
                # 其他阻塞原因，也标记为 blocked_unverified
                control_state = "blocked_unverified"
                approved_summary = f"任务已阻塞：{blocked_reason or '原因未知'}"
                next_action = "manual_or_session_recovery"
                next_actor = "guardian"
                blocked_state_locked = True
        elif blocked_reason == "missing_pipeline_receipt" and not single_agent_terminal_reply:
            control_state = "blocked_unverified"
            approved_summary = "任务缺少结构化流水线回执，守护系统已判定为阻塞。"
            next_action = "manual_or_session_recovery"
            next_actor = "guardian"
            blocked_state_locked = True
        elif blocked_reason == "control_followup_failed" and not single_agent_terminal_reply:
            control_state = "blocked_control_followup_failed"
            approved_summary = "守护系统尝试接回任务，但控制追问失败，任务已判定为阻塞。"
            next_action = "manual_or_session_recovery"
            next_actor = "guardian"
            blocked_state_locked = True

        required_receipts = list(contract.get("required_receipts") or [])
        missing_receipts = [item for item in required_receipts if item not in seen_receipts]

        if blocked_state_locked:
            pass
        elif contract.get("id") == "quant_guarded":
            if not missing_receipts and flags["dispatch_completed"]:
                control_state = "execution_verified"
                approved_summary = "量化/精算任务已收到完整结构化回执，但当前还缺送达证据。"
                next_action = "await_delivery_confirmation"
                next_actor = "guardian"
            elif flags["calculator_blocked"] or flags["verifier_blocked"] or flags["risk_blocked"]:
                control_state = "analysis_blocked"
                approved_summary = "精算/复核链路已阻塞。"
                next_action = "manual_or_session_recovery"
                next_actor = "guardian"
            elif "calculator:started" in missing_receipts:
                control_state = "received_only"
                approved_summary = "任务已接收，但精算节点尚未启动。"
                next_action = "require_calculator_start"
                next_actor = "calculator"
            elif "calculator:completed" in missing_receipts:
                control_state = "calculator_running"
                approved_summary = "精算节点已启动，等待结构化计算结果。"
                next_action = "await_calculator_receipt"
                next_actor = "calculator"
            elif "verifier:completed" in missing_receipts:
                control_state = "awaiting_verifier"
                approved_summary = "精算结果已返回，但复核尚未完成。"
                next_action = "require_verifier_receipt"
                next_actor = "verifier"
        elif contract.get("id") in {"delivery_pipeline", "a_share_delivery_pipeline"}:
            if not missing_receipts and flags["dispatch_completed"]:
                control_state = "execution_verified"
                approved_summary = "产品、开发、测试链路都已收到结构化回执，但当前还缺送达证据。"
                next_action = "await_delivery_confirmation"
                next_actor = "main"
            elif not missing_receipts:
                control_state = "test_running"
                approved_summary = "测试回执已完成，但主链路交付完成态尚未确认。"
                next_action = "await_delivery_confirmation"
                next_actor = "main"
            elif flags["test_blocked"]:
                control_state = "test_blocked"
                approved_summary = "测试阶段已阻塞。"
                next_action = "wait_for_test_recovery"
                next_actor = "test"
            elif flags["dev_blocked"]:
                control_state = "dev_blocked"
                approved_summary = "开发阶段已阻塞。"
                next_action = "wait_for_dev_recovery"
                next_actor = "dev"
            elif "pm:started" in missing_receipts:
                control_state = "received_only"
                approved_summary = "任务已接收，但产品梳理尚未开始。"
                next_action = "require_pm_receipt"
                next_actor = "pm"
            elif "pm:completed" in missing_receipts:
                control_state = "planning_only"
                approved_summary = "产品阶段已启动，等待方案回执。"
                next_action = "await_pm_receipt"
                next_actor = "pm"
            elif "dev:started" in missing_receipts:
                control_state = "planning_only"
                approved_summary = "方案已完成，但开发尚未启动。"
                next_action = "require_dev_receipt"
                next_actor = "dev"
            elif "dev:completed" in missing_receipts:
                control_state = "dev_running"
                approved_summary = "开发阶段已启动，存在结构化执行证据。"
                next_action = "await_dev_receipt"
                next_actor = "dev"
            elif "test:started" in missing_receipts:
                control_state = "awaiting_test"
                approved_summary = "开发回执已完成，但测试尚未启动。"
                next_action = "require_test_receipt"
                next_actor = "test"
            elif "test:completed" in missing_receipts:
                control_state = "test_running"
                approved_summary = "测试阶段已启动，等待最终测试回执。"
                next_action = "await_test_receipt"
                next_actor = "test"
        elif flags["test_completed"]:
            control_state = "execution_verified"
            approved_summary = "测试回执已完成，任务具备强执行证据，但当前还缺送达证据。"
            next_action = "await_delivery_confirmation"
            next_actor = "guardian"
        elif flags["test_blocked"]:
            control_state = "test_blocked"
            approved_summary = "测试阶段已阻塞。"
            next_action = "wait_for_test_recovery"
            next_actor = "test"
        elif flags["dev_blocked"]:
            control_state = "dev_blocked"
            approved_summary = "开发阶段已阻塞。"
            next_action = "wait_for_dev_recovery"
            next_actor = "dev"
        elif flags["dev_completed"] and not flags["test_started"]:
            control_state = "awaiting_test"
            approved_summary = "开发回执已完成，但测试尚未启动。"
            next_action = "require_test_receipt"
            next_actor = "test"
        elif flags["dev_started"]:
            control_state = "dev_running"
            approved_summary = "开发阶段已启动，存在结构化执行证据。"
            next_action = "await_dev_receipt"
            next_actor = "dev"
        elif flags["pm_completed"]:
            control_state = "planning_only"
            approved_summary = "方案已完成，但开发尚未启动。"
            next_action = "require_dev_receipt"
            next_actor = "dev"
        elif flags["pipeline_progress"]:
            control_state = "progress_only"
            approved_summary = "存在阶段进展标记，但缺少结构化回执。"
            next_action = "require_receipt_or_block"
            next_actor = "guardian"
        elif flags["dispatch_started"] and flags["dispatch_completed"]:
            if str(contract.get("id") or "single_agent") == "single_agent":
                control_state = "execution_verified"
                approved_summary = "single-agent 任务已形成结论性回复，但当前还缺送达证据。"
                next_action = "await_delivery_confirmation"
                next_actor = "guardian"
            else:
                control_state = "received_only"
                approved_summary = "任务已接收并执行过，但没有结构化流水线证据。"
                next_action = "require_receipt_or_block"
                next_actor = "guardian"

        if task.get("status") == "completed" and evidence_level == "weak":
            if str(contract.get("id") or "single_agent") == "single_agent":
                control_state = "delivery_pending"
                approved_summary = "single-agent 任务状态显示已完成，但缺少可验证执行证据与送达证据。"
                next_action = "await_delivery_confirmation"
                next_actor = "guardian"
            else:
                approved_summary = "任务状态显示已完成，但缺少可验证结构化证据。"
                next_action = "require_receipt_or_block"
                next_actor = "guardian"
        elif task.get("status") == "blocked" and evidence_level == "weak" and str(contract.get("id") or "single_agent") == "single_agent":
            control_state = "blocked_unverified"
            approved_summary = "single-agent 任务已进入 blocked 终态。"
            next_action = "manual_or_session_recovery"
            next_actor = "guardian"

        contract_id = str(contract.get("id") or "single_agent")
        pipeline_recovery = self._infer_pipeline_recovery(
            contract_id,
            flags,
            missing_receipts,
            latest_receipt=latest_receipt,
            current_stage=str(task.get("current_stage") or ""),
            task_status=str(task.get("status") or ""),
        )
        if pipeline_recovery and (
            str(task.get("status") or "") in {"blocked", "no_reply", "background"}
            or control_state in {"blocked_unverified", "blocked_control_followup_failed"}
        ):
            if control_state in {"blocked_unverified", "blocked_control_followup_failed"}:
                approved_summary = "流水线已失联，守护系统已将任务切换到恢复流程。"
            else:
                approved_summary = "流水线已派发但主链路未收到关键回执，任务进入失联恢复视图。"
            next_action = "manual_or_session_recovery"
            next_actor = str(pipeline_recovery.get("rebind_target") or "guardian")
        if latest_recovery_success and missing_receipts:
            approved_summary = "守护系统已自动发起恢复，当前等待恢复后的新结构化回执。"
            next_action = "await_receipt_after_recovery"
            next_actor = str(
                latest_recovery_success.get("rebind_target")
                or pipeline_recovery.get("rebind_target")
                or "guardian"
            )
        phase_statuses = self._build_contract_phase_statuses(contract_id, flags, seen_receipts)
        control_action = self.get_open_control_action(task_id)
        active_phase = self._infer_active_phase(task, latest_receipt, control_state)
        timing = self._resolve_timing_metadata(contract_view, active_phase)
        now = int(time.time())
        followup_stage = self._derive_followup_stage(task, control_action, timing, now=now)
        heartbeat_age = max(0, now - int(task.get("last_progress_at") or task.get("started_at") or now))
        heartbeat_ok = heartbeat_age <= int(timing.get("heartbeat_interval") or 0)
        protocol_status = {
            "request": "seen" if (flags["dispatch_started"] or flags["dispatch_completed"] or bool(task.get("question"))) else "missing",
            "confirmed": "seen" if (flags["pipeline_progress"] or flags["pipeline_receipt"] or bool(latest_receipt)) else "missing",
            "final": "seen" if control_state == "completed_verified" else "missing",
            "blocked": "seen" if control_state.startswith("blocked") or control_state.endswith("_blocked") else "missing",
            "ack_id": str((latest_receipt or {}).get("ack_id") or task.get("task_id") or ""),
        }
        native_state = {
            "source": "runtime_events",
            "dispatch_started": flags["dispatch_started"],
            "dispatch_completed": flags["dispatch_completed"],
            "pipeline_progress_seen": flags["pipeline_progress"],
            "pipeline_receipt_seen": flags["pipeline_receipt"],
            "latest_receipt": latest_receipt,
            "status": str(task.get("status") or ""),
            "stage": str(task.get("current_stage") or ""),
        }
        heuristic_state = {
            "visible_completion_seen": False,
            "question_candidate": self.get_task_question_candidate(task_id) or "",
            "latest_protocol_violation": latest_protocol_violation,
        }
        v2_truth = self._derive_v2_truth_snapshot(
            contract=contract_view,
            flags=flags,
            seen_receipts=seen_receipts,
            task_status=str(task.get("status") or ""),
            blocked_reason=blocked_reason,
        )
        derived_state = {
            "control_state": control_state,
            "approved_summary": approved_summary,
            "next_action": next_action,
            "next_actor": next_actor,
            "missing_receipts": missing_receipts,
            "contract_id": str(contract_view.get("id") or "single_agent"),
            "v2_state": v2_truth.get("state") or "unknown",
        }
        # Phase 2 简化：压缩 evidence_summary，去掉冗余的 claim 字段
        evidence_summary = (
            f"evidence={evidence_level}; control_state={control_state}; "
            f"next_action={next_action}; next_actor={next_actor or '-'}; "
            f"heartbeat_age={heartbeat_age}; followup_stage={followup_stage or 'healthy'}"
        )
        action_reason = approved_summary
        user_visible_progress = self._render_user_visible_progress(
            contract_view,
            control_state,
            approved_summary=approved_summary,
            next_action=next_action,
            missing_receipts=missing_receipts,
        )

        action_status = str((control_action or {}).get("status") or "")
        recent_runtime_window = max(int(timing.get("hard_timeout") or 0) * 2, 3600)
        followup_visible = (
            action_status in {"sent", "blocked", "resolved"}
            or int((control_action or {}).get("attempts") or 0) > 0
            or heartbeat_age <= recent_runtime_window
        )
        if control_state in {"dev_running", "test_running", "calculator_running", "awaiting_verifier", "planning_only", "progress_only", "received_only"} and followup_visible:
            if followup_stage == "blocked":
                user_visible_progress = "追证失败，已 blocked：任务缺少可验证结构化回执，主人当前可见为阻塞状态。"
            elif followup_stage in {"soft", "hard"}:
                user_visible_progress = f"超过窗口，正在追证：当前阶段={active_phase}，已进入{followup_stage}追证窗口。"
            elif heartbeat_ok and control_state not in {"received_only", "planning_only"}:
                user_visible_progress = f"已开始且心跳正常：当前阶段={active_phase}，心跳窗口={timing.get('heartbeat_interval')}s。"

        action_template = str(((control_action or {}).get("details") or {}).get("status_template") or "").strip()
        if action_template and action_status in {"sent", "blocked", "resolved"}:
            user_visible_progress = action_template

        # Phase 4 简化：删除 truth_level 和 public_control_state 的计算
        if core_supervision.get("truth_level") == "core_projection":
            workflow_state = str(core_supervision.get("workflow_state") or "")
            if task_blocked_reason == "control_followup_failed":
                control_state = "blocked_control_followup_failed"
                next_action = "manual_or_session_recovery"
                next_actor = next_actor or "guardian"
                approved_summary = "守护系统尝试接回任务，但控制追问失败，任务已判定为阻塞。"
            elif core_supervision.get("is_terminal"):
                if workflow_state == "delivered":
                    control_state = "completed_verified"
                    approved_summary = "主闭环已完成并确认送达。"
                    next_action = "none"
                    next_actor = ""
                else:
                    approved_summary = core_supervision.get("followup_summary") or approved_summary
                    next_action = "manual_or_session_recovery" if core_supervision.get("is_blocked") else next_action
            elif core_supervision.get("is_delivery_pending"):
                control_state = "delivery_pending"
                approved_summary = core_supervision.get("followup_summary") or "最终结论已形成，但当前仍在等待送达确认。"
                next_action = "await_delivery_confirmation"
                next_actor = next_actor or "main"
            elif core_supervision.get("needs_followup"):
                approved_summary = core_supervision.get("followup_summary") or approved_summary
                next_action = str(core_supervision.get("next_action") or next_action)
                next_actor = str(core_supervision.get("next_actor") or next_actor)
            if core_supervision.get("control_state"):
                control_state = str(core_supervision.get("control_state") or control_state)
            if core_supervision.get("is_blocked") and core_supervision.get("blocked_reason"):
                blocked_reason = str(core_supervision.get("blocked_reason") or blocked_reason)

        if task_blocked_reason == "control_followup_failed":
            control_state = "blocked_control_followup_failed"
            next_action = "manual_or_session_recovery"
            next_actor = next_actor or "guardian"
            approved_summary = "守护系统尝试接回任务，但控制追问失败，任务已判定为阻塞。"

        delivery_state_for_guard = normalize_delivery_state(
            (core_supervision.get("delivery_state") if isinstance(core_supervision, dict) else "")
            or (v2_truth.get("delivery_state") if isinstance(v2_truth, dict) else "")
            or "undelivered"
        )
        has_execution_evidence = evidence_level in {"strong", "moderate"} or bool(flags["dispatch_completed"])
        has_delivery_evidence = delivery_state_for_guard in TERMINAL_DELIVERY_STATES
        if control_state == "completed_verified" and not (has_execution_evidence and has_delivery_evidence):
            control_state = "delivery_pending" if has_execution_evidence else "received_only"
            if has_execution_evidence:
                approved_summary = "任务已有执行证据，但送达证据不足，已降级为 delivery_pending。"
                next_action = "await_delivery_confirmation"
                next_actor = next_actor or "guardian"
            else:
                approved_summary = "任务缺少执行证据与送达证据，禁止标记 completed_verified。"
                next_action = "require_receipt_or_block"
                next_actor = "guardian"
        return {
            "evidence_level": evidence_level,
            "evidence_summary": evidence_summary,
            "control_state": control_state,
            "approved_summary": approved_summary,
            "next_action": next_action,
            "next_actor": next_actor,
            "user_visible_progress": user_visible_progress,
            "protocol": protocol_status,
            "contract": contract_view,
            "missing_receipts": missing_receipts,
            "control_action": control_action,
            "phase_statuses": phase_statuses,
            "flags": flags,
            "latest_receipt": latest_receipt,
            "pipeline_recovery": pipeline_recovery,
            "latest_recovery": latest_recovery_success,
            "latest_protocol_violation": latest_protocol_violation,
            "native_state": native_state,
            "derived_state": derived_state,
            "v2_truth": v2_truth,
            "heuristic_state": heuristic_state,
            "core_supervision": core_supervision,
            "timing": timing,
            "active_phase": active_phase,
            "followup_stage": followup_stage,
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_ok": heartbeat_ok,
            "terminal_state_seen": bool(protocol_status["final"] == "seen" or protocol_status["blocked"] == "seen"),
        }

    def get_current_task(self, *, env_id: str | None = None, session_key: str | None = None) -> dict[str, Any] | None:
        # 优先返回当前会话的前台任务
        if session_key:
            binding = self.get_foreground_binding(session_key)
            if binding:
                root_task_id = str(binding.get("foreground_root_task_id") or "")
                if root_task_id:
                    # 找到这个 root_task 对应的 task
                    tasks = self.list_tasks(limit=100)
                    for task in tasks:
                        core = self.get_core_closure_snapshot_for_task(task["task_id"], allow_legacy_projection=False)
                        task_root = str(((core or {}).get("root_task") or {}).get("root_task_id") or "")
                        if task_root == root_task_id:
                            return task
        
        # 没有前台绑定，按状态和时间排序
        query = """
            SELECT * FROM managed_tasks
            WHERE status IN ('running', 'blocked', 'background')
        """
        params: list[Any] = []
        if env_id:
            query += " AND env_id = ?"
            params.append(env_id)
        query += """
            ORDER BY
                CASE status
                    WHEN 'running' THEN 0
                    WHEN 'blocked' THEN 1
                    WHEN 'background' THEN 2
                    ELSE 3
                END,
                updated_at DESC,
                created_at DESC
            LIMIT 1
        """
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row_to_task(row)

    def summarize_tasks(self, *, env_id: str | None = None) -> dict[str, Any]:
        query = """
            SELECT status, COUNT(*) AS cnt
            FROM managed_tasks
        """
        params: list[Any] = []
        if env_id:
            query += " WHERE env_id = ?"
            params.append(env_id)
        query += " GROUP BY status"
        counts = {
            "running": 0,
            "blocked": 0,
            "background": 0,
            "completed": 0,
            "no_reply": 0,
        }
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        for row in rows:
            status = str(row["status"] or "")
            if status in counts:
                counts[status] = int(row["cnt"] or 0)
        counts["total"] = sum(counts.values())
        return counts

    def reset_task_registry(self, *, env_id: str | None = None) -> None:
        with self._connection() as conn:
            if env_id:
                task_rows = conn.execute(
                    "SELECT task_id FROM managed_tasks WHERE env_id = ?",
                    (env_id,),
                ).fetchall()
                task_ids = [str(row["task_id"]) for row in task_rows]
                conn.execute("DELETE FROM managed_tasks WHERE env_id = ?", (env_id,))
                conn.execute("DELETE FROM task_control_actions WHERE env_id = ?", (env_id,))
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    conn.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", task_ids)
                    conn.execute(f"DELETE FROM task_contracts WHERE task_id IN ({placeholders})", task_ids)
            else:
                conn.execute("DELETE FROM managed_tasks")
                conn.execute("DELETE FROM task_events")
                conn.execute("DELETE FROM task_contracts")
                conn.execute("DELETE FROM task_control_actions")

    def summarize_control_plane(self, *, env_id: str | None = None, recent_limit: int = 50) -> dict[str, Any]:
        actions = self.list_task_control_actions(env_id=env_id, limit=recent_limit)
        status_counts = {"pending": 0, "sent": 0, "blocked": 0, "resolved": 0}
        for item in actions:
            status = str(item.get("status") or "")
            if status in status_counts:
                status_counts[status] += 1

        tasks = self.list_tasks(limit=recent_limit)
        if env_id:
            tasks = [task for task in tasks if task.get("env_id") == env_id]

        claim_counts = {
            "received_only": 0,
            "progress_only": 0,
            "phase_verified": 0,
            "execution_verified": 0,
            "completed_verified": 0,
            "blocked": 0,
        }
        next_actor_counts: dict[str, int] = {}
        recoverable = 0
        blocked = 0
        verified = 0
        protocol_violations = self.count_task_events("protocol_violation", env_id=env_id)
        for task in tasks:
            control = self.derive_task_control_state(task["task_id"])
            core = self.get_core_closure_snapshot_for_task(task["task_id"], allow_legacy_projection=False)
            core_supervision = self.derive_core_task_supervision(task["task_id"])
            # Phase 4 简化：删除 claim_level，直接使用 control_state
            control_state = str(control.get("control_state") or "")
            if control_state in claim_counts:
                claim_counts[control_state] += 1
            next_actor = str(core_supervision.get("next_actor") or control.get("next_actor") or "")
            if next_actor:
                next_actor_counts[next_actor] = next_actor_counts.get(next_actor, 0) + 1
            workflow_state = str(core.get("workflow_state") or "")
            if workflow_state in {"blocked", "delivery_failed", "dlq", "failed", "cancelled"}:
                blocked += 1
            elif core_supervision.get("needs_followup"):
                recoverable += 1
            elif str(control.get("next_action") or "") not in {"none", "manual_or_session_recovery"}:
                recoverable += 1
            if workflow_state in {"completed", "delivery_pending", "delivered"} or control_state == "completed_verified":
                verified += 1

        sent = status_counts["sent"] + status_counts["resolved"]
        blocked_or_pending = status_counts["blocked"] + status_counts["pending"]
        success_rate = round((status_counts["resolved"] / sent) * 100, 1) if sent else 0.0
        return {
            "actions": status_counts,
            "tasks": {
                "total": len(tasks),
                "verified": verified,
                "recoverable": recoverable,
                "blocked": blocked,
                "protocol_violations": protocol_violations,
                "claim_counts": claim_counts,
                "next_actor_counts": next_actor_counts,
            },
            "ack_success_rate": success_rate,
            "headline": (
                "控制面稳定" if blocked_or_pending == 0
                else f"存在 {blocked_or_pending} 个待处理控制动作"
            ),
        }

    def list_tasks_for_session(self, session_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM managed_tasks
                WHERE session_key = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (session_key, limit),
            ).fetchall()
        return [task for row in rows if (task := self._row_to_task(row))]

    def derive_session_resolution(self, session_key: str) -> dict[str, Any]:
        tasks = self.list_tasks_for_session(session_key, limit=20)
        if not tasks:
            return {
                "session_key": session_key,
                "active_task_id": None,
                "active_task_status": "none",
                "background_tasks": 0,
                "stale_results": 0,
                "late_completed_tasks": [],
                "summary": "当前会话没有已登记任务。",
            }

        foreground_root_id = str((self.get_foreground_binding(session_key) or {}).get("foreground_root_task_id") or "")
        task_core_snapshots = {
            task["task_id"]: self.get_core_closure_snapshot_for_task(
                task["task_id"],
                allow_legacy_projection=False,
            )
            for task in tasks
        }
        task_core_supervision = {
            task["task_id"]: self.derive_core_task_supervision(task["task_id"])
            for task in tasks
        }

        def _is_core_active(task: dict[str, Any]) -> bool:
            task_id = str(task.get("task_id") or "")
            snapshot = task_core_snapshots.get(task_id) or {}
            supervision = task_core_supervision.get(task_id) or {}
            workflow_state = str(snapshot.get("workflow_state") or "")
            if not workflow_state:
                return False
            if bool(supervision.get("is_terminal")):
                return False
            if (
                str(task.get("status") or "") == "completed"
                and workflow_state == "completed"
                and not bool(supervision.get("is_delivery_pending"))
                and not bool(supervision.get("needs_followup"))
            ):
                return False
            return workflow_state not in {"delivered", "failed", "dlq", "cancelled"}

        active = None
        if foreground_root_id:
            active = next(
                (
                    task
                    for task in tasks
                    if str((task_core_snapshots.get(task["task_id"], {}).get("root_task") or {}).get("root_task_id") or "") == foreground_root_id
                ),
                None,
            )
        if active is None:
            active = next(
                (
                    task
                    for task in tasks
                    if _is_core_active(task) or task.get("status") in {"running", "blocked", "background"}
                ),
                tasks[0],
            )
        active_started = max(int(active.get("created_at") or 0), int(active.get("started_at") or 0))
        late_completed = [
            {
                "task_id": task["task_id"],
                "question": task.get("question") or task.get("last_user_message") or "未知任务",
                "completed_at": int(task.get("completed_at") or 0),
            }
            for task in tasks
            if task.get("status") == "completed"
            and int(task.get("completed_at") or 0) >= active_started
            and task["task_id"] != active["task_id"]
        ]
        background_tasks = sum(1 for task in tasks if task.get("status") == "background")
        stale_results = len(late_completed)

        summary = "当前会话以最新任务为主。"
        if late_completed:
            summary = "存在旧任务迟到结果，需要与当前任务隔离展示。"
        elif active.get("status") == "blocked":
            summary = "当前会话任务已阻塞，后续追问应优先返回阻塞事实。"
        elif background_tasks:
            summary = "当前会话存在后台任务，新的追问应优先绑定当前活动任务。"

        return {
            "session_key": session_key,
            "active_task_id": active["task_id"],
            "active_task_status": active.get("status") or "unknown",
            "background_tasks": background_tasks,
            "stale_results": stale_results,
            "late_completed_tasks": late_completed[:5],
            "summary": summary,
        }

    def background_other_tasks_for_session(self, session_key: str, keep_task_id: str) -> None:
        now = int(time.time())
        with self._connection() as conn:
            # 后台化时同时设置 root_task_id，避免被判定为孤儿任务
            conn.execute(
                """
                UPDATE managed_tasks
                SET status = 'background', backgrounded_at = ?, updated_at = ?, root_task_id = ?
                WHERE session_key = ? AND task_id != ? AND status IN ('running', 'blocked')
                """,
                (now, now, keep_task_id, session_key, keep_task_id),
            )

    def record_task_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> bool:
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        event_key = hashlib.sha1(f"{event_type}|{payload_json}".encode("utf-8", errors="ignore")).hexdigest()
        now = int(time.time())
        with self._connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO task_events(task_id, event_type, event_key, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        event_type,
                        event_key,
                        payload_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        inserted = bool(getattr(cursor, "rowcount", 0))
        if inserted and self.get_task(task_id):
            try:
                snapshot = self.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
                root_task_id = str((snapshot.get("root_task") or {}).get("root_task_id") or "")
                if not root_task_id or root_task_id.startswith("legacy-root:"):
                    self.sync_legacy_task_projection(task_id)
            except Exception:
                pass
        return inserted

    def upsert_learning(
        self,
        *,
        learning_key: str,
        env_id: str,
        task_id: str,
        category: str,
        title: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
        status: str = "pending",
        promoted_target: str = "",
    ) -> dict[str, Any]:
        now = int(time.time())
        evidence_payload = json.dumps(evidence or {}, ensure_ascii=False)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id, occurrences, first_seen_at FROM learnings WHERE learning_key = ?",
                (learning_key,),
            ).fetchone()
            if row:
                occurrences = int(row["occurrences"] or 0) + 1
                first_seen_at = int(row["first_seen_at"] or now)
                conn.execute(
                    """
                    UPDATE learnings
                    SET env_id = ?, task_id = ?, category = ?, title = ?, detail = ?, status = ?,
                        evidence_json = ?, occurrences = ?, promoted_target = ?, last_seen_at = ?, updated_at = ?
                    WHERE learning_key = ?
                    """,
                    (
                        env_id,
                        task_id,
                        category,
                        title,
                        detail,
                        status,
                        evidence_payload,
                        occurrences,
                        promoted_target,
                        now,
                        now,
                        learning_key,
                    ),
                )
            else:
                occurrences = 1
                first_seen_at = now
                conn.execute(
                    """
                    INSERT INTO learnings(
                        learning_key, env_id, task_id, category, title, detail, status,
                        evidence_json, occurrences, promoted_target, first_seen_at, last_seen_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        learning_key,
                        env_id,
                        task_id,
                        category,
                        title,
                        detail,
                        status,
                        evidence_payload,
                        occurrences,
                        promoted_target,
                        first_seen_at,
                        now,
                        now,
                    ),
                )
        return self.get_learning(learning_key) or {}

    def get_learning(self, learning_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM learnings WHERE learning_key = ?",
                (learning_key,),
            ).fetchone()
        return self._row_to_learning(row)

    def list_learnings(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM learnings"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [learning for row in rows if (learning := self._row_to_learning(row))]

    def summarize_learnings(self) -> dict[str, Any]:
        summary = {"pending": 0, "promoted": 0, "reviewed": 0, "total": 0}
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM learnings GROUP BY status"
            ).fetchall()
        for row in rows:
            status = str(row["status"] or "")
            if status in summary:
                summary[status] = int(row["cnt"] or 0)
        summary["total"] = sum(v for k, v in summary.items() if k != "total")
        return summary

    def record_reflection_run(self, run_type: str, summary: dict[str, Any]) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO reflection_runs(run_type, summary_json, created_at)
                VALUES (?, ?, ?)
                """,
                (run_type, json.dumps(summary, ensure_ascii=False), int(time.time())),
            )

    def list_reflection_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT run_type, summary_json, created_at
                FROM reflection_runs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "run_type": row["run_type"],
                "summary": json.loads(row["summary_json"] or "{}"),
                "created_at": int(row["created_at"] or 0),
            }
            for row in rows
        ]

    def _normalize_problem_code(self, problem_code: str | None) -> str:
        value = str(problem_code or "").strip()
        if not value:
            raise ValueError("missing self_evolution problem_code")
        value = SELF_EVOLUTION_PROBLEM_CODE_ALIASES.get(value, value)
        if value in SELF_EVOLUTION_PROBLEM_CODES:
            return value
        raise ValueError(f"unsupported self_evolution problem_code: {problem_code}")

    def record_self_evolution_event(
        self,
        *,
        learning_key: str,
        event_type: str,
        problem_code: str,
        details: dict[str, Any] | None = None,
        root_task_id: str = "",
        workflow_run_id: str = "",
        actor: str = "guardian",
        created_at: int | None = None,
    ) -> None:
        normalized_event = str(event_type or "").strip()
        if normalized_event not in SELF_EVOLUTION_EVENT_TYPES:
            raise ValueError(f"unsupported self_evolution event_type: {event_type}")
        ts = int(created_at or time.time())
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO self_evolution_events(
                    learning_key, event_type, problem_code, root_task_id,
                    workflow_run_id, actor, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    learning_key,
                    normalized_event,
                    self._normalize_problem_code(problem_code),
                    root_task_id,
                    workflow_run_id,
                    actor,
                    json.dumps(details or {}, ensure_ascii=False),
                    ts,
                ),
            )
        self.rebuild_self_evolution_projection(learning_key=learning_key)

    def list_self_evolution_events(
        self,
        *,
        learning_key: str | None = None,
        problem_code: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM self_evolution_events"
        clauses: list[str] = []
        params: list[Any] = []
        if learning_key:
            clauses.append("learning_key = ?")
            params.append(learning_key)
        if problem_code:
            clauses.append("problem_code = ?")
            params.append(problem_code)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "details": json.loads(row["details_json"] or "{}"),
            }
            for row in rows
        ]

    def rebuild_self_evolution_projection(self, *, learning_key: str | None = None) -> None:
        query = "SELECT * FROM self_evolution_events"
        params: list[Any] = []
        if learning_key:
            query += " WHERE learning_key = ?"
            params.append(learning_key)
        query += " ORDER BY learning_key ASC, created_at ASC, id ASC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
            grouped: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                grouped.setdefault(str(row["learning_key"]), []).append(row)

            if learning_key and not grouped:
                conn.execute("DELETE FROM self_evolution_projection WHERE learning_key = ?", (learning_key,))
                return

            for current_key, event_rows in grouped.items():
                projection: dict[str, Any] = {
                    "learning_key": current_key,
                    "problem_code": "task_closure_missing",
                    "current_state": "recorded",
                    "title": "",
                    "summary": "",
                    "candidate_rule_json": "{}",
                    "adopted_rule_target": "",
                    "verified_at": 0,
                    "verified_in": "",
                    "recurrence_count": 0,
                    "last_root_task_id": "",
                    "last_workflow_run_id": "",
                    "last_evidence_json": "{}",
                    "last_actor": "",
                    "last_event_type": "",
                    "created_at": 0,
                    "updated_at": 0,
                    "closed_at": 0,
                }
                for row in event_rows:
                    details = json.loads(row["details_json"] or "{}")
                    event_type = str(row["event_type"] or "")
                    projection["problem_code"] = self._normalize_problem_code(row["problem_code"])
                    projection["last_root_task_id"] = str(row["root_task_id"] or projection["last_root_task_id"])
                    projection["last_workflow_run_id"] = str(row["workflow_run_id"] or projection["last_workflow_run_id"])
                    projection["last_actor"] = str(row["actor"] or projection["last_actor"])
                    projection["last_event_type"] = event_type
                    projection["updated_at"] = int(row["created_at"] or projection["updated_at"])
                    if not projection["created_at"]:
                        projection["created_at"] = int(row["created_at"] or 0)
                    if details.get("title"):
                        projection["title"] = str(details.get("title") or projection["title"])
                    if details.get("summary"):
                        projection["summary"] = str(details.get("summary") or projection["summary"])
                    if details.get("evidence") is not None:
                        projection["last_evidence_json"] = json.dumps(details.get("evidence") or {}, ensure_ascii=False)
                    elif details:
                        projection["last_evidence_json"] = json.dumps(details, ensure_ascii=False)

                    if event_type == "recorded":
                        projection["current_state"] = "recorded"
                    elif event_type == "candidate_rule":
                        projection["current_state"] = "candidate_rule"
                        projection["candidate_rule_json"] = json.dumps(details.get("candidate_rule") or details, ensure_ascii=False)
                    elif event_type == "adopted":
                        projection["current_state"] = "adopted"
                        projection["adopted_rule_target"] = str(details.get("rule_target") or details.get("rule_file") or "")
                    elif event_type == "verified":
                        projection["current_state"] = "verified"
                        projection["verified_at"] = int(row["created_at"] or 0)
                        projection["verified_in"] = str(details.get("scenario") or details.get("verified_in") or "")
                    elif event_type == "closed":
                        projection["current_state"] = "closed"
                        projection["closed_at"] = int(row["created_at"] or 0)
                    elif event_type == "reopened":
                        projection["current_state"] = "reopened"
                    elif event_type == "recurrence":
                        projection["recurrence_count"] = int(projection["recurrence_count"] or 0) + 1

                conn.execute(
                    """
                    INSERT INTO self_evolution_projection(
                        learning_key, problem_code, current_state, title, summary,
                        candidate_rule_json, adopted_rule_target, verified_at, verified_in,
                        recurrence_count, last_root_task_id, last_workflow_run_id,
                        last_evidence_json, last_actor, last_event_type, created_at,
                        updated_at, closed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(learning_key) DO UPDATE SET
                        problem_code = excluded.problem_code,
                        current_state = excluded.current_state,
                        title = excluded.title,
                        summary = excluded.summary,
                        candidate_rule_json = excluded.candidate_rule_json,
                        adopted_rule_target = excluded.adopted_rule_target,
                        verified_at = excluded.verified_at,
                        verified_in = excluded.verified_in,
                        recurrence_count = excluded.recurrence_count,
                        last_root_task_id = excluded.last_root_task_id,
                        last_workflow_run_id = excluded.last_workflow_run_id,
                        last_evidence_json = excluded.last_evidence_json,
                        last_actor = excluded.last_actor,
                        last_event_type = excluded.last_event_type,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        closed_at = excluded.closed_at
                    """,
                    (
                        projection["learning_key"],
                        projection["problem_code"],
                        projection["current_state"],
                        projection["title"],
                        projection["summary"],
                        projection["candidate_rule_json"],
                        projection["adopted_rule_target"],
                        projection["verified_at"],
                        projection["verified_in"],
                        projection["recurrence_count"],
                        projection["last_root_task_id"],
                        projection["last_workflow_run_id"],
                        projection["last_evidence_json"],
                        projection["last_actor"],
                        projection["last_event_type"],
                        projection["created_at"],
                        projection["updated_at"],
                        projection["closed_at"],
                    ),
                )

    def _row_to_self_evolution_projection(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        payload = dict(row)
        payload["candidate_rule"] = json.loads(payload.pop("candidate_rule_json") or "{}")
        payload["evidence"] = json.loads(payload.pop("last_evidence_json") or "{}")
        return payload

    def get_self_evolution_projection(self, learning_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM self_evolution_projection WHERE learning_key = ?",
                (learning_key,),
            ).fetchone()
        return self._row_to_self_evolution_projection(row)

    def list_self_evolution_projections(
        self,
        *,
        states: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM self_evolution_projection"
        params: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" WHERE current_state IN ({placeholders})"
            params.extend(states)
        query += " ORDER BY updated_at DESC, learning_key DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self._row_to_self_evolution_projection(row))]

    def summarize_self_evolution(self) -> dict[str, Any]:
        summary = {
            "recorded": 0,
            "candidate_rule": 0,
            "adopted": 0,
            "verified": 0,
            "closed": 0,
            "reopened": 0,
            "pending": 0,
            "reviewed": 0,
            "promoted": 0,
            "pending_total": 0,
            "total": 0,
        }
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT current_state, COUNT(*) AS cnt FROM self_evolution_projection GROUP BY current_state"
            ).fetchall()
        for row in rows:
            state = str(row["current_state"] or "")
            if state in summary:
                summary[state] = int(row["cnt"] or 0)
        summary["pending_total"] = (
            summary["recorded"]
            + summary["candidate_rule"]
            + summary["adopted"]
            + summary["reopened"]
        )
        summary["pending"] = summary["pending_total"]
        summary["reviewed"] = summary["verified"]
        summary["promoted"] = summary["closed"]
        summary["total"] = sum(
            v
            for k, v in summary.items()
            if k not in {"pending", "reviewed", "promoted", "pending_total", "total"}
        )
        return summary

    def list_learning_view(self, *, limit: int = 50) -> list[dict[str, Any]]:
        items = self.list_self_evolution_projections(limit=limit)
        if items:
            result: list[dict[str, Any]] = []
            for item in items:
                state = str(item.get("current_state") or "")
                if state in {"recorded", "candidate_rule", "adopted", "reopened"}:
                    status = "pending"
                elif state == "verified":
                    status = "reviewed"
                else:
                    status = "promoted"
                result.append(
                    {
                        "learning_key": item.get("learning_key"),
                        "env_id": "primary",
                        "task_id": item.get("last_root_task_id") or "",
                        "category": "self_evolution",
                        "title": item.get("title") or item.get("problem_code") or "未命名学习",
                        "detail": item.get("summary") or "",
                        "status": status,
                        "lifecycle_state": state,
                        "evidence": item.get("evidence") or {},
                        "occurrences": int(item.get("recurrence_count") or 0) + 1,
                        "promoted_target": item.get("adopted_rule_target") or "",
                        "first_seen_at": int(item.get("created_at") or 0),
                        "last_seen_at": int(item.get("updated_at") or 0),
                        "updated_at": int(item.get("updated_at") or 0),
                        "problem_code": item.get("problem_code") or "task_closure_missing",
                        "verified_at": int(item.get("verified_at") or 0),
                        "verified_in": item.get("verified_in") or "",
                        "rule_added": bool(item.get("adopted_rule_target")),
                    }
                )
            return result
        return self.list_learnings(limit=limit)

    def upsert_watcher_task(self, task: dict[str, Any]) -> None:
        now = int(time.time())
        payload = {
            "watcher_task_id": str(task["watcher_task_id"]),
            "env_id": str(task.get("env_id") or "primary"),
            "source_agent": str(task.get("source_agent") or ""),
            "target_agent": str(task.get("target_agent") or ""),
            "intent": str(task.get("intent") or ""),
            "current_state": str(task.get("current_state") or "registered"),
            "completed_at": int(task.get("completed_at") or 0),
            "delivered_at": int(task.get("delivered_at") or 0),
            "last_checked_at": int(task.get("last_checked_at") or 0),
            "error_count": int(task.get("error_count") or 0),
            "in_dlq": int(bool(task.get("in_dlq"))),
            "payload_json": json.dumps(task.get("payload") or {}, ensure_ascii=False),
            "updated_at": int(task.get("updated_at") or now),
        }
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO watcher_tasks(
                    watcher_task_id, env_id, source_agent, target_agent, intent, current_state,
                    completed_at, delivered_at, last_checked_at, error_count, in_dlq, payload_json, updated_at
                )
                VALUES(
                    :watcher_task_id, :env_id, :source_agent, :target_agent, :intent, :current_state,
                    :completed_at, :delivered_at, :last_checked_at, :error_count, :in_dlq, :payload_json, :updated_at
                )
                ON CONFLICT(watcher_task_id) DO UPDATE SET
                    env_id = excluded.env_id,
                    source_agent = excluded.source_agent,
                    target_agent = excluded.target_agent,
                    intent = excluded.intent,
                    current_state = excluded.current_state,
                    completed_at = excluded.completed_at,
                    delivered_at = excluded.delivered_at,
                    last_checked_at = excluded.last_checked_at,
                    error_count = excluded.error_count,
                    in_dlq = excluded.in_dlq,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                payload,
            )

    def list_watcher_tasks(self, *, env_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM watcher_tasks"
        params: list[Any] = []
        if env_id:
            query += " WHERE env_id = ?"
            params.append(env_id)
        query += " ORDER BY updated_at DESC, watcher_task_id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [task for row in rows if (task := self._row_to_watcher_task(row))]

    def summarize_watcher_tasks(self, *, env_id: str | None = None) -> dict[str, Any]:
        tasks = self.list_watcher_tasks(env_id=env_id, limit=500)
        summary = {
            "total": len(tasks),
            "completed": 0,
            "delivered": 0,
            "undelivered": 0,
            "failed": 0,
            "dlq": 0,
            "active": 0,
        }
        for item in tasks:
            state = str(item.get("current_state") or "")
            completed = int(item.get("completed_at") or 0) > 0
            delivered = int(item.get("delivered_at") or 0) > 0
            in_dlq = bool(item.get("in_dlq"))
            if completed:
                summary["completed"] += 1
            if delivered:
                summary["delivered"] += 1
            if completed and not delivered:
                summary["undelivered"] += 1
            if state in {"failed", "error"}:
                summary["failed"] += 1
            if in_dlq:
                summary["dlq"] += 1
            if state not in {"delivered", "failed", "error"} and not in_dlq:
                summary["active"] += 1
        return summary

    @staticmethod
    def _load_json_field(raw: Any, default: Any) -> Any:
        if raw in (None, ""):
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def _row_to_root_task(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "root_task_id": row["root_task_id"],
            "session_key": row["session_key"],
            "origin_request_id": row["origin_request_id"] or "",
            "origin_message_id": row["origin_message_id"] or "",
            "reply_to_message_id": row["reply_to_message_id"] or "",
            "user_goal_summary": row["user_goal_summary"] or "",
            "intent_type": row["intent_type"] or "",
            "contract_type": row["contract_type"] or "",
            "status": row["status"] or "",
            "state_reason": row["state_reason"] or "",
            "current_workflow_run_id": row["current_workflow_run_id"] or "",
            "active": bool(row["active"]),
            "foreground_priority": int(row["foreground_priority"] or 0),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "terminal_at": int(row["terminal_at"] or 0),
            "finalized_at": int(row["finalized_at"] or 0),
            "superseded_by_root_task_id": row["superseded_by_root_task_id"] or "",
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def _row_to_workflow_run(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "workflow_run_id": row["workflow_run_id"],
            "root_task_id": row["root_task_id"],
            "parent_workflow_run_id": row["parent_workflow_run_id"] or "",
            "idempotency_key": row["idempotency_key"] or "",
            "workflow_type": row["workflow_type"] or "",
            "intent_type": row["intent_type"] or "",
            "contract_type": row["contract_type"] or "",
            "current_state": row["current_state"] or "",
            "state_reason": row["state_reason"] or "",
            "current_step_run_id": row["current_step_run_id"] or "",
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "started_at": int(row["started_at"] or 0),
            "terminal_at": int(row["terminal_at"] or 0),
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def _row_to_step_run(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "step_run_id": row["step_run_id"],
            "workflow_run_id": row["workflow_run_id"],
            "root_task_id": row["root_task_id"],
            "stable_step_key": row["stable_step_key"] or "",
            "agent_id": row["agent_id"] or "",
            "phase": row["phase"] or "",
            "current_state": row["current_state"] or "",
            "state_reason": row["state_reason"] or "",
            "latest_receipt_id": row["latest_receipt_id"] or "",
            "latest_heartbeat_seq": int(row["latest_heartbeat_seq"] or 0),
            "last_heartbeat_at": int(row["last_heartbeat_at"] or 0),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "started_at": int(row["started_at"] or 0),
            "terminal_at": int(row["terminal_at"] or 0),
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def _row_to_foreground_binding(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "session_key": row["session_key"],
            "foreground_root_task_id": row["foreground_root_task_id"],
            "binding_version": int(row["binding_version"] or 0),
            "reason": row["reason"] or "",
            "updated_at": int(row["updated_at"] or 0),
            "metadata": self._load_json_field(row["metadata_json"], {}),
        }

    def _row_to_task(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        task = dict(row)
        task["latest_receipt"] = json.loads(task.pop("latest_receipt_json") or "{}")
        return task

    def _row_to_learning(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        learning = dict(row)
        learning["evidence"] = json.loads(learning.pop("evidence_json") or "{}")
        return learning

    def _row_to_watcher_task(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        task = dict(row)
        task["payload"] = json.loads(task.pop("payload_json") or "{}")
        task["in_dlq"] = bool(task.get("in_dlq"))
        return task
