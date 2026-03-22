import sqlite3
import tempfile
import unittest
from pathlib import Path

from state_store import MonitorStateStore


class StateStoreMigrationTests(unittest.TestCase):
    def test_init_db_dedupes_existing_task_event_keys_before_recreating_unique_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "monitor.db"

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_key TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO task_events(task_id, event_type, event_key, payload_json, created_at) VALUES (?, ?, ?, ?, ?) ",
                ("task-1", "visible_completion", "dup-key", "{}", 1),
            )
            conn.execute(
                "INSERT INTO task_events(task_id, event_type, event_key, payload_json, created_at) VALUES (?, ?, ?, ?, ?) ",
                ("task-1", "visible_completion", "dup-key", "{}", 2),
            )
            conn.commit()
            conn.close()

            store = MonitorStateStore(base)
            with store._connection() as check_conn:
                rows = check_conn.execute(
                    "SELECT task_id, event_type, event_key, COUNT(*) FROM task_events GROUP BY 1,2,3"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][3], 1)
                indexes = check_conn.execute(
                    "PRAGMA index_list('task_events')"
                ).fetchall()
                self.assertTrue(any("idx_task_events_dedupe" == row[1] for row in indexes))


if __name__ == "__main__":
    unittest.main()
