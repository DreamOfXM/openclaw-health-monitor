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

    def test_task_registry_summary_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)

            store.upsert_task(
                {
                    "task_id": "task-running",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "DEV_IMPLEMENTING",
                    "question": "做一个新系统",
                    "last_user_message": "做一个新系统",
                    "started_at": 10,
                    "last_progress_at": 20,
                    "created_at": 10,
                    "updated_at": 20,
                }
            )
            store.upsert_task(
                {
                    "task_id": "task-completed",
                    "session_key": "session-b",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "补一个模块",
                    "last_user_message": "补一个模块",
                    "started_at": 30,
                    "last_progress_at": 40,
                    "created_at": 30,
                    "updated_at": 40,
                    "completed_at": 41,
                }
            )
            store.record_task_event("task-running", "dispatch_started", {"question": "做一个新系统"})
            store.record_task_event("task-running", "stage_progress", {"marker": "DEV_IMPLEMENTING"})

            current = store.get_current_task(env_id="primary")
            summary = store.summarize_tasks(env_id="primary")
            events = store.list_task_events("task-running", limit=10)

            self.assertEqual(current["task_id"], "task-running")
            self.assertEqual(summary["running"], 1)
            self.assertEqual(summary["completed"], 1)
            self.assertEqual(summary["total"], 2)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event_type"], "stage_progress")

    def test_record_task_event_deduplicates_same_second_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "任务A",
                    "last_user_message": "任务A",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            with unittest.mock.patch("time.time", return_value=100):
                store.record_task_event("task-1", "dispatch_started", {"question": "任务A"})
                store.record_task_event("task-1", "dispatch_started", {"question": "任务A"})
            events = store.list_task_events("task-1", limit=10)
            self.assertEqual(len(events), 1)

    def test_repair_task_identity_uses_dispatch_started_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "dispatching to agent (session=abc)",
                    "last_user_message": "dispatching to agent (session=abc)",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            with unittest.mock.patch("time.time", return_value=100):
                store.record_task_event("task-1", "dispatch_started", {"question": "我再提个需求"})

            repaired = store.repair_task_identity("task-1")
            task = store.get_task("task-1")

            self.assertTrue(repaired)
            self.assertEqual(task["question"], "我再提个需求")
            self.assertEqual(task["last_user_message"], "我再提个需求")


if __name__ == "__main__":
    unittest.main()
