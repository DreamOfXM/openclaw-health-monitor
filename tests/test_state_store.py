import tempfile
import unittest
from pathlib import Path

from state_store import MonitorStateStore


class StateStoreTests(unittest.TestCase):
    def test_round_trip_alerts_versions_and_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)

            alerts = {"gateway_down": {"last_alert": 1, "count": 2}}
            versions = {"current": "v1", "history": [{"version": "v1"}]}

            store.save_alerts(alerts)
            store.save_versions(versions)
            store.record_change("config", "changed", {"key": "AUTO_UPDATE"})
            store.record_health_sample(
                process_running=True,
                gateway_healthy=False,
                cpu=10.0,
                mem_used=2,
                mem_total=16,
            )

            self.assertEqual(store.load_alerts(base / "alerts.json"), alerts)
            self.assertEqual(store.load_versions(base / "versions.json"), versions)
            changes = store.list_recent_changes(days=7, limit=10)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["type"], "config")
            self.assertTrue((base / "data" / "monitor.db").exists())

    def test_task_registry_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)

            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "agent:main:feishu:direct:ou_test",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "帮我做一个量化系统",
                    "last_user_message": "帮我做一个量化系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "latest_receipt": {"agent": "pm", "action": "started"},
                }
            )
            store.background_other_tasks_for_session(
                "agent:main:feishu:direct:ou_test",
                "task-1",
            )
            store.record_task_event("task-1", "created", {"source": "test"})

            task = store.get_task("task-1")
            latest = store.get_latest_task_for_session("agent:main:feishu:direct:ou_test")
            tasks = store.list_tasks(limit=5)

            self.assertIsNotNone(task)
            self.assertEqual(task["latest_receipt"]["agent"], "pm")
            self.assertEqual(latest["task_id"], "task-1")
            self.assertEqual(tasks[0]["task_id"], "task-1")


if __name__ == "__main__":
    unittest.main()
