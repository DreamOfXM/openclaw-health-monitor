#!/usr/bin/env python3
"""SQLite-backed state store for openclaw-health-monitor."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class MonitorStateStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.db_path = base_dir / "data" / "monitor.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(task_events)").fetchall()
            }
            if "event_key" not in columns:
                conn.execute("ALTER TABLE task_events ADD COLUMN event_key TEXT NOT NULL DEFAULT ''")
            conn.execute("DROP INDEX IF EXISTS idx_task_events_dedupe")
            rows = conn.execute(
                "SELECT id, task_id, event_type, payload_json FROM task_events WHERE event_key = '' OR event_key IS NULL"
            ).fetchall()
            dedupe_map: dict[tuple[str, str, str], int] = {}
            duplicate_ids: list[int] = []
            for row in rows:
                event_key = hashlib.sha1(
                    f"{row['event_type']}|{row['payload_json'] or '{}'}".encode("utf-8", errors="ignore")
                ).hexdigest()
                marker = (str(row["task_id"] or ""), str(row["event_type"] or ""), event_key)
                if marker in dedupe_map:
                    duplicate_ids.append(int(row["id"]))
                    continue
                dedupe_map[marker] = int(row["id"])
                conn.execute(
                    "UPDATE task_events SET event_key = ? WHERE id = ?",
                    (event_key, row["id"]),
                )
            if duplicate_ids:
                placeholders = ",".join("?" for _ in duplicate_ids)
                conn.execute(
                    f"DELETE FROM task_events WHERE id IN ({placeholders})",
                    duplicate_ids,
                )
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
                ORDER BY updated_at DESC, created_at DESC
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
        if contract_id == "delivery_pipeline":
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
    def _summarize_claim_level(
        control_state: str,
        evidence_level: str,
        missing_receipts: list[str],
    ) -> str:
        if control_state in {"completed_verified"}:
            return "completed_verified"
        if control_state in {"blocked_unverified", "blocked_control_followup_failed", "dev_blocked", "test_blocked", "analysis_blocked"}:
            return "blocked"
        if evidence_level == "strong" and not missing_receipts:
            return "execution_verified"
        if evidence_level == "strong":
            return "phase_verified"
        if evidence_level == "moderate":
            return "progress_only"
        return "received_only"

    def reconcile_task_control_action(
        self,
        task: dict[str, Any],
        control: dict[str, Any],
    ) -> dict[str, Any] | None:
        now = int(time.time())
        task_id = task["task_id"]
        env_id = str(task.get("env_id") or "primary")
        next_action = str(control.get("next_action") or "none")
        summary = str(control.get("approved_summary") or "")
        missing = list(control.get("missing_receipts") or [])
        control_state = str(control.get("control_state") or "unknown")
        details_payload = {
            "contract_id": ((control.get("contract") or {}).get("id") or "single_agent"),
            "next_action": next_action,
            "next_actor": control.get("next_actor") or "",
            "claim_level": control.get("claim_level") or "received_only",
            "phase_statuses": control.get("phase_statuses") or [],
        }
        existing = self.list_task_control_actions(
            task_id=task_id,
            statuses=["pending", "sent", "blocked"],
            limit=20,
        )

        if next_action in {"none", "manual_or_session_recovery"}:
            final_status = "resolved" if next_action == "none" else "blocked"
            with self._connection() as conn:
                conn.execute(
                    """
                    UPDATE task_control_actions
                    SET status = ?, summary = ?, control_state = ?, updated_at = ?, resolved_at = ?
                    WHERE task_id = ? AND status IN ('pending', 'sent', 'blocked')
                    """,
                    (final_status, summary, control_state, now, now, task_id),
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
        with self._connection() as conn:
            conn.execute(
                f"UPDATE task_control_actions SET {', '.join(fields)} WHERE id = :action_id",
                params,
            )

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

    def derive_task_control_state(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            return {
                "evidence_level": "none",
                "control_state": "unknown",
                "approved_summary": "任务不存在",
                "next_action": "none",
                "next_actor": "",
                "claim_level": "received_only",
                "contract": {"id": "single_agent", "required_receipts": []},
                "missing_receipts": [],
                "control_action": None,
                "phase_statuses": [],
                "flags": {},
            }

        events = self.list_task_events(task_id, limit=50)
        contract = self.get_task_contract(task_id) or {
            "id": "single_agent",
            "required_receipts": [],
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
                if action == "started":
                    flags["dev_started"] = True
                elif action == "completed":
                    flags["dev_started"] = True
                    flags["dev_completed"] = True
                elif action == "blocked":
                    flags["dev_started"] = True
                    flags["dev_blocked"] = True
            if agent == "test":
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
                flags["visible_completion"] = True
            elif event_type == "stage_progress":
                flags["pipeline_progress"] = True
            elif event_type == "pipeline_receipt":
                latest_receipt = payload.get("receipt") or latest_receipt
                apply_receipt(payload.get("receipt") or {})

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
        blocked_state_locked = False
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

        required_receipts = list(contract.get("required_receipts") or [])
        missing_receipts = [item for item in required_receipts if item not in seen_receipts]

        if blocked_state_locked:
            pass
        elif contract.get("id") == "quant_guarded":
            if not missing_receipts and flags["dispatch_completed"]:
                control_state = "completed_verified"
                approved_summary = "量化/精算任务已收到完整结构化回执。"
                next_action = "none"
                next_actor = ""
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
        elif contract.get("id") == "delivery_pipeline":
            if not missing_receipts and flags["dispatch_completed"]:
                control_state = "completed_verified"
                approved_summary = "产品、开发、测试链路都已收到结构化回执。"
                next_action = "none"
                next_actor = ""
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
            control_state = "completed_verified"
            approved_summary = "测试回执已完成，任务具备强证据完成状态。"
            next_action = "none"
            next_actor = ""
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
            control_state = "received_only"
            approved_summary = "任务已接收并执行过，但没有结构化流水线证据。"
            next_action = "require_receipt_or_block"
            next_actor = "guardian"

        if task.get("status") == "completed" and flags["visible_completion"] and evidence_level == "weak":
            approved_summary = "任务已给出可见完成回复，但没有流水线级结构化证据。"
            next_action = "require_receipt_or_block"
            next_actor = "guardian"

        contract_id = str(contract.get("id") or "single_agent")
        phase_statuses = self._build_contract_phase_statuses(contract_id, flags, seen_receipts)
        claim_level = self._summarize_claim_level(control_state, evidence_level, missing_receipts)

        return {
            "evidence_level": evidence_level,
            "control_state": control_state,
            "approved_summary": approved_summary,
            "next_action": next_action,
            "next_actor": next_actor,
            "claim_level": claim_level,
            "contract": contract,
            "missing_receipts": missing_receipts,
            "control_action": self.get_open_control_action(task_id),
            "phase_statuses": phase_statuses,
            "flags": flags,
            "latest_receipt": latest_receipt,
        }

    def get_current_task(self, *, env_id: str | None = None) -> dict[str, Any] | None:
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
        for task in tasks:
            control = self.derive_task_control_state(task["task_id"])
            claim = str(control.get("claim_level") or "received_only")
            if claim in claim_counts:
                claim_counts[claim] += 1
            next_actor = str(control.get("next_actor") or "")
            if next_actor:
                next_actor_counts[next_actor] = next_actor_counts.get(next_actor, 0) + 1
            control_state = str(control.get("control_state") or "")
            if control_state.startswith("blocked") or control_state.endswith("_blocked"):
                blocked += 1
            elif str(control.get("next_action") or "") not in {"none", "manual_or_session_recovery"}:
                recoverable += 1
            if claim in {"execution_verified", "completed_verified"}:
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

        active = next(
            (
                task
                for task in tasks
                if task.get("status") in {"running", "blocked", "background"}
            ),
            tasks[0],
        )
        active_updated = int(active.get("updated_at") or 0)
        late_completed = [
            {
                "task_id": task["task_id"],
                "question": task.get("question") or task.get("last_user_message") or "未知任务",
                "completed_at": int(task.get("completed_at") or 0),
            }
            for task in tasks
            if task.get("status") == "completed"
            and int(task.get("completed_at") or 0) >= active_updated
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
            conn.execute(
                """
                UPDATE managed_tasks
                SET status = 'background', backgrounded_at = ?, updated_at = ?
                WHERE session_key = ? AND task_id != ? AND status IN ('running', 'blocked')
                """,
                (now, now, session_key, keep_task_id),
            )

    def record_task_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        event_key = hashlib.sha1(f"{event_type}|{payload_json}".encode("utf-8", errors="ignore")).hexdigest()
        now = int(time.time())
        with self._connection() as conn:
            try:
                conn.execute(
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
                return

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
