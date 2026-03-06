#!/usr/bin/env python3
"""SQLite-backed state store for openclaw-health-monitor."""

from __future__ import annotations

import json
import sqlite3
import time
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

    def _init_db(self) -> None:
        with self._connect() as conn:
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
                """
            )

    def _load_kv(self, namespace: str, key: str) -> Any | None:
        with self._connect() as conn:
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
        with self._connect() as conn:
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

    def record_change(self, change_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        details = details or {}
        now = time.localtime()
        with self._connect() as conn:
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
        with self._connect() as conn:
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
        with self._connect() as conn:
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
