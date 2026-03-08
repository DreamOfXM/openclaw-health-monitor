#!/usr/bin/env python3
"""SQLite-backed state store for openclaw-health-monitor."""

from __future__ import annotations

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
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
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
                SELECT event_type, payload_json, created_at
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
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": int(row["created_at"] or 0),
            }
            for row in rows
        ]

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
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    int(time.time()),
                ),
            )

    def _row_to_task(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        task = dict(row)
        task["latest_receipt"] = json.loads(task.pop("latest_receipt_json") or "{}")
        return task
