import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from state_store import MonitorStateStore, RetryingSQLiteConnection


class StateStoreTests(unittest.TestCase):
    def test_retrying_connection_retries_locked_call(self):
        conn = object.__new__(RetryingSQLiteConnection)
        calls = {"count": 0}

        def flaky_call():
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch("time.sleep") as sleep:
            result = conn._retry_sqlite_call(flaky_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)
        sleep.assert_called_once()

    def test_init_db_lock_is_swallowed_during_store_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(
                MonitorStateStore,
                "_init_db",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                store = MonitorStateStore(base)

        self.assertEqual(store.db_path, base / "data" / "monitor.db")

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

    def test_append_runtime_event_keeps_recent_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)

            for idx in range(5):
                store.append_runtime_event("restart_events:primary", {"seq": idx}, limit=3)

            events = store.load_runtime_value("restart_events:primary", [])
            self.assertEqual([item["seq"] for item in events], [2, 3, 4])

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
            with mock.patch("time.time", return_value=100):
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
            with mock.patch("time.time", return_value=100):
                store.record_task_event("task-1", "dispatch_started", {"question": "我再提个需求"})

            repaired = store.repair_task_identity("task-1")
            task = store.get_task("task-1")

            self.assertTrue(repaired)
            self.assertEqual(task["question"], "我再提个需求")
            self.assertEqual(task["last_user_message"], "我再提个需求")

    def test_derive_session_resolution_detects_late_completed_task(self):
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
                    "current_stage": "处理中",
                    "question": "新任务",
                    "last_user_message": "新任务",
                    "started_at": 10,
                    "last_progress_at": 20,
                    "created_at": 10,
                    "updated_at": 20,
                }
            )
            store.upsert_task(
                {
                    "task_id": "task-old",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "旧任务",
                    "last_user_message": "旧任务",
                    "started_at": 1,
                    "last_progress_at": 18,
                    "created_at": 1,
                    "updated_at": 21,
                    "completed_at": 21,
                }
            )
            resolution = store.derive_session_resolution("session-a")
            self.assertEqual(resolution["active_task_id"], "task-running")
            self.assertEqual(resolution["stale_results"], 1)
            self.assertIn("隔离", resolution["summary"])

    def test_derive_session_resolution_prefers_foreground_root_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            session_key = "session-foreground"
            for task_id, status, ts in (("task-a", "running", 20), ("task-b", "background", 21)):
                store.upsert_task(
                    {
                        "task_id": task_id,
                        "session_key": session_key,
                        "env_id": "primary",
                        "channel": "feishu_dm",
                        "status": status,
                        "current_stage": "处理中",
                        "question": task_id,
                        "last_user_message": task_id,
                        "started_at": 10,
                        "last_progress_at": ts,
                        "created_at": 10,
                        "updated_at": ts,
                    }
                )
                store.record_task_event(task_id, "dispatch_started", {"question": task_id})
                store.sync_legacy_task_projection(task_id)

            store.switch_foreground_root_task(
                session_key=session_key,
                next_root_task_id="legacy-root:task-b",
                reason="test_override",
            )

            resolution = store.derive_session_resolution(session_key)

            self.assertEqual(resolution["active_task_id"], "task-b")

    def test_core_root_workflow_and_step_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)

            store.upsert_root_task(
                {
                    "root_task_id": "rt-1",
                    "session_key": "session-core",
                    "origin_request_id": "req-1",
                    "origin_message_id": "msg-1",
                    "user_goal_summary": "实现主闭环",
                    "intent_type": "delivery",
                    "contract_type": "delivery_pipeline",
                    "status": "open",
                    "current_workflow_run_id": "wr-1",
                    "metadata": {"owner_agent": "main"},
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wr-1",
                    "root_task_id": "rt-1",
                    "workflow_type": "pm_dev_test",
                    "intent_type": "delivery",
                    "contract_type": "delivery_pipeline",
                    "current_state": "accepted",
                    "metadata": {"run_version": 1},
                }
            )
            store.upsert_step_run(
                {
                    "step_run_id": "sr-1",
                    "workflow_run_id": "wr-1",
                    "root_task_id": "rt-1",
                    "stable_step_key": "dev:implementation",
                    "agent_id": "dev",
                    "phase": "implementation",
                    "current_state": "started",
                    "latest_heartbeat_seq": 2,
                }
            )

            root_task = store.get_root_task("rt-1")
            workflow = store.get_workflow_run("wr-1")
            steps = store.list_step_runs("wr-1")

            self.assertEqual(root_task["current_workflow_run_id"], "wr-1")
            self.assertEqual(root_task["metadata"]["owner_agent"], "main")
            self.assertEqual(workflow["workflow_type"], "pm_dev_test")
            self.assertEqual(steps[0]["stable_step_key"], "dev:implementation")
            self.assertEqual(steps[0]["latest_heartbeat_seq"], 2)

    def test_core_events_reduce_to_delivery_pending_then_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_root_task(
                {
                    "root_task_id": "rt-2",
                    "session_key": "session-core",
                    "user_goal_summary": "交付结果",
                    "status": "open",
                    "current_workflow_run_id": "wr-2",
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wr-2",
                    "root_task_id": "rt-2",
                    "workflow_type": "direct_main",
                    "current_state": "accepted",
                }
            )
            store.upsert_step_run(
                {
                    "step_run_id": "sr-2",
                    "workflow_run_id": "wr-2",
                    "root_task_id": "rt-2",
                    "stable_step_key": "main:delivery",
                    "agent_id": "main",
                    "phase": "delivery",
                    "current_state": "started",
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-3",
                    "root_task_id": "rt-2",
                    "workflow_run_id": "wr-2",
                    "step_run_id": "sr-2",
                    "event_type": "receipt_adopted_completed",
                    "event_ts": 30,
                    "event_seq": 1,
                    "payload": {"reason": "executor_completed"},
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-2",
                    "root_task_id": "rt-2",
                    "workflow_run_id": "wr-2",
                    "step_run_id": "sr-2",
                    "event_type": "step_started",
                    "event_ts": 20,
                    "event_seq": 1,
                    "payload": {"reason": "executor_started"},
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-4",
                    "root_task_id": "rt-2",
                    "workflow_run_id": "wr-2",
                    "event_type": "finalizer_finalized",
                    "event_ts": 40,
                    "event_seq": 1,
                    "payload": {"reason": "finalizer_done", "finalization_id": "fin-1"},
                }
            )

            projection = store.rebuild_workflow_projection("wr-2")
            self.assertEqual(projection["current_state"], "delivery_pending")
            self.assertTrue(projection["finalized"])
            self.assertFalse(projection["delivered"])
            self.assertEqual(projection["current_step_run_id"], "sr-2")

            store.record_core_event(
                {
                    "event_id": "ev-5",
                    "root_task_id": "rt-2",
                    "workflow_run_id": "wr-2",
                    "delivery_attempt_id": "da-1",
                    "event_type": "delivery_confirmed",
                    "event_ts": 50,
                    "event_seq": 1,
                    "payload": {"reason": "channel_ack", "delivery_attempt_id": "da-1"},
                }
            )
            projection = store.rebuild_workflow_projection("wr-2")
            workflow = store.get_workflow_run("wr-2")
            root_task = store.get_root_task("rt-2")
            self.assertEqual(projection["current_state"], "delivered")
            self.assertTrue(projection["delivered"])
            self.assertEqual(workflow["current_step_run_id"], "sr-2")
            self.assertEqual(root_task["status"], "closed")
            self.assertFalse(root_task["active"])

    def test_reducer_resolves_open_followups_and_updates_delivery_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_root_task(
                {
                    "root_task_id": "rt-follow",
                    "session_key": "session-follow",
                    "user_goal_summary": "交付后关闭 followup",
                    "status": "open",
                    "current_workflow_run_id": "wr-follow",
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wr-follow",
                    "root_task_id": "rt-follow",
                    "workflow_type": "direct_main",
                    "current_state": "completed",
                }
            )
            store.upsert_finalizer_record(
                {
                    "finalization_id": "fin-follow",
                    "root_task_id": "rt-follow",
                    "workflow_run_id": "wr-follow",
                    "decision_state": "pending_decision",
                    "final_status": "completed",
                    "trigger_reason": "test",
                }
            )
            store.upsert_delivery_attempt(
                {
                    "delivery_attempt_id": "da-follow",
                    "root_task_id": "rt-follow",
                    "workflow_run_id": "wr-follow",
                    "finalization_id": "fin-follow",
                    "attempt_no": 1,
                    "channel": "feishu_dm",
                    "target": "session-follow",
                    "current_state": "delivery_pending",
                    "idempotency_key": "da-follow",
                }
            )
            store.upsert_followup(
                {
                    "followup_id": "fu-follow",
                    "root_task_id": "rt-follow",
                    "workflow_run_id": "wr-follow",
                    "followup_type": "delivery_retry",
                    "current_state": "open",
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-follow-1",
                    "root_task_id": "rt-follow",
                    "workflow_run_id": "wr-follow",
                    "event_type": "finalizer_finalized",
                    "event_ts": 10,
                    "event_seq": 1,
                    "payload": {"reason": "ready", "finalization_id": "fin-follow"},
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-follow-2",
                    "root_task_id": "rt-follow",
                    "workflow_run_id": "wr-follow",
                    "delivery_attempt_id": "da-follow",
                    "event_type": "delivery_confirmed",
                    "event_ts": 20,
                    "event_seq": 1,
                    "payload": {"reason": "channel_ack", "delivery_attempt_id": "da-follow"},
                }
            )

            projection = store.rebuild_workflow_projection("wr-follow")
            finalizer = store.get_finalizer_record("fin-follow")
            delivery = store.get_delivery_attempt("da-follow")
            followup = store.get_followup("fu-follow")

            self.assertEqual(projection["current_state"], "delivered")
            self.assertEqual(finalizer["decision_state"], "finalized")
            self.assertEqual(finalizer["delivery_state"], "delivery_confirmed")
            self.assertEqual(delivery["current_state"], "delivery_confirmed")
            self.assertEqual(delivery["confirmation_level"], "delivery_confirmed")
            self.assertEqual(followup["current_state"], "resolved")

    def test_core_events_are_sorted_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.record_core_event(
                {
                    "event_id": "ev-b",
                    "root_task_id": "rt-sort",
                    "workflow_run_id": "wr-sort",
                    "event_type": "workflow_queued",
                    "event_ts": 100,
                    "event_seq": 2,
                    "payload": {},
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-a",
                    "root_task_id": "rt-sort",
                    "workflow_run_id": "wr-sort",
                    "event_type": "workflow_accepted",
                    "event_ts": 100,
                    "event_seq": 1,
                    "payload": {},
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-c",
                    "root_task_id": "rt-sort",
                    "workflow_run_id": "wr-sort",
                    "event_type": "step_started",
                    "event_ts": 101,
                    "event_seq": 1,
                    "payload": {},
                }
            )

            events = store.list_core_events(workflow_run_id="wr-sort", limit=10)
            self.assertEqual([event["event_id"] for event in events], ["ev-a", "ev-b", "ev-c"])

    def test_foreground_binding_switch_uses_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)

            ok = store.switch_foreground_root_task(
                session_key="session-bind",
                next_root_task_id="rt-1",
                reason="new_root_created",
            )
            self.assertTrue(ok)
            failed = store.switch_foreground_root_task(
                session_key="session-bind",
                next_root_task_id="rt-2",
                reason="explicit_switch",
                expected_foreground_root_task_id="rt-mismatch",
            )
            self.assertFalse(failed)
            ok = store.switch_foreground_root_task(
                session_key="session-bind",
                next_root_task_id="rt-2",
                reason="explicit_switch",
                expected_foreground_root_task_id="rt-1",
            )
            self.assertTrue(ok)

            binding = store.get_foreground_binding("session-bind")
            self.assertEqual(binding["foreground_root_task_id"], "rt-2")
            self.assertEqual(binding["binding_version"], 2)
            events = store.list_core_events(root_task_id="rt-2", limit=10)
            self.assertIn("foreground_binding_switched", [event["event_type"] for event in events])

    def test_current_workflow_run_switch_uses_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_root_task(
                {
                    "root_task_id": "rt-run",
                    "session_key": "session-run",
                    "user_goal_summary": "切换当前 run",
                    "current_workflow_run_id": "wr-1",
                }
            )
            ok = store.switch_current_workflow_run(
                root_task_id="rt-run",
                next_workflow_run_id="wr-2",
                reason="binding.run_pointer_switched",
                expected_workflow_run_id="wr-1",
            )
            self.assertTrue(ok)
            failed = store.switch_current_workflow_run(
                root_task_id="rt-run",
                next_workflow_run_id="wr-3",
                reason="binding.run_pointer_switched",
                expected_workflow_run_id="wr-mismatch",
            )
            self.assertFalse(failed)
            root = store.get_root_task("rt-run")
            self.assertEqual(root["current_workflow_run_id"], "wr-2")
            events = store.list_core_events(root_task_id="rt-run", limit=10)
            self.assertIn("workflow_run_pointer_switched", [event["event_type"] for event in events])

    def test_reason_codes_are_normalized_and_unknowns_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_root_task(
                {
                    "root_task_id": "rt-reason",
                    "session_key": "session-reason",
                    "user_goal_summary": "校验 reason code",
                    "state_reason": "totally_freeform_reason",
                }
            )
            root = store.get_root_task("rt-reason")
            self.assertEqual(root["state_reason"], "unknown_reason")
            self.assertEqual(root["metadata"]["original_reason_code"], "totally_freeform_reason")
            store.record_core_event(
                {
                    "event_id": "ev-reason",
                    "root_task_id": "rt-reason",
                    "workflow_run_id": "wr-reason",
                    "event_type": "workflow_accepted",
                    "event_ts": 1,
                    "payload": {"reason": "dispatch_started"},
                }
            )
            event = store.list_core_events(root_task_id="rt-reason", limit=1)[0]
            self.assertEqual(event["payload"]["reason"], "legacy.dispatch_started")

    def test_retarget_and_correction_events_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            self.assertTrue(
                store.record_retarget_event(
                    source_root_task_id="rt-source",
                    workflow_run_id="wr-source",
                    target_root_task_id="rt-target",
                    reason="binding.retarget_existing_root",
                )
            )
            self.assertTrue(
                store.record_correction_event(
                    root_task_id="rt-source",
                    workflow_run_id="wr-source",
                    correction_type="reply_to_rebind",
                    reason="correction.applied",
                )
            )
            events = store.list_core_events(root_task_id="rt-source", limit=10)
            event_types = [event["event_type"] for event in events]
            self.assertIn("retarget_to_existing_root", event_types)
            self.assertIn("correction_applied", event_types)

    def test_sync_legacy_task_projection_builds_core_objects_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-legacy",
                    "session_key": "session-legacy",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "实现一个新功能",
                    "last_user_message": "实现一个新功能",
                    "started_at": 10,
                    "last_progress_at": 40,
                    "created_at": 10,
                    "updated_at": 50,
                    "completed_at": 60,
                }
            )
            store.upsert_task_contract(
                "task-legacy",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-legacy", "dispatch_started", {"question": "实现一个新功能"})
            store.record_task_event(
                "task-legacy",
                "pipeline_receipt",
                {
                    "receipt": {
                        "agent": "dev",
                        "phase": "implementation",
                        "action": "completed",
                        "evidence": "files=engine.py",
                    }
                },
            )
            store.record_task_event("task-legacy", "visible_completion", {"message": "已完成"})

            store.sync_legacy_task_projection("task-legacy")

            root_task = store.get_root_task("legacy-root:task-legacy")
            workflow = store.get_workflow_run("legacy-run:task-legacy")
            steps = store.list_step_runs("legacy-run:task-legacy")
            events = store.list_core_events(workflow_run_id="legacy-run:task-legacy", limit=20)
            projection = store.rebuild_workflow_projection("legacy-run:task-legacy")
            binding = store.get_foreground_binding("session-legacy")
            delivery_attempts = store.list_delivery_attempts(root_task_id="legacy-root:task-legacy", limit=10)
            finalizers = store.list_finalizer_records(root_task_id="legacy-root:task-legacy", limit=10)

            self.assertEqual(root_task["current_workflow_run_id"], "legacy-run:task-legacy")
            self.assertEqual(workflow["workflow_type"], "delivery_pipeline")
            self.assertEqual(steps[0]["agent_id"], "dev")
            self.assertEqual(steps[0]["current_state"], "completed")
            self.assertIn("receipt_adopted_completed", [event["event_type"] for event in events])
            # 减法重构：visible_completion 不再自动投影为 finalizer_finalized / delivery_confirmed
            # 真正的送达必须来自结构化 delivery 记录
            self.assertNotIn("finalizer_finalized", [event["event_type"] for event in events])
            self.assertNotIn("delivery_confirmed", [event["event_type"] for event in events])
            # workflow 可以基于 receipt 显示 completed，但 delivery 未确认
            self.assertEqual(projection["current_state"], "completed")
            self.assertEqual(len(delivery_attempts), 0)
            self.assertEqual(len(finalizers), 0)
            self.assertEqual(binding["foreground_root_task_id"], "legacy-root:task-legacy")

    def test_sync_legacy_task_projection_bridges_open_control_actions_to_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-followup",
                    "session_key": "session-followup",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "等待结构化回执",
                    "question": "继续推进任务",
                    "last_user_message": "继续推进任务",
                    "created_at": 10,
                    "updated_at": 20,
                }
            )
            store.reconcile_task_control_action(
                store.get_task("task-followup"),
                {
                    "contract": {"id": "delivery_pipeline", "protocol_version": "hm.v1"},
                    "approved_summary": "缺少开发回执，继续追问",
                    "missing_receipts": ["dev:completed"],
                    "next_action": "followup_required",
                    "control_state": "blocked_unverified",
                    "phase_statuses": [],
                    "claim_level": "received_only",
                    "action_reason": "missing_pipeline_receipt",
                },
            )

            store.sync_legacy_task_projection("task-followup")

            followups = store.list_followups(root_task_id="legacy-root:task-followup", limit=10)
            events = store.list_core_events(workflow_run_id="legacy-run:task-followup", limit=20)
            self.assertEqual(len(followups), 1)
            self.assertEqual(followups[0]["created_by"], "guardian")
            self.assertEqual(followups[0]["followup_type"], "followup_required")
            self.assertEqual(followups[0]["current_state"], "open")
            self.assertEqual(followups[0]["trigger_reason"], "followup.blocked_unverified")
            self.assertIn("followup_requested", [event["event_type"] for event in events])

    def test_summarize_main_closure_reports_roots_events_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_root_task(
                {
                    "root_task_id": "rt-sum",
                    "session_key": "session-sum",
                    "user_goal_summary": "汇总主闭环",
                    "status": "open",
                    "current_workflow_run_id": "wr-sum",
                    "active": True,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wr-sum",
                    "root_task_id": "rt-sum",
                    "workflow_type": "direct_main",
                    "current_state": "delivery_failed",
                }
            )
            store.upsert_followup(
                {
                    "followup_id": "fu-sum",
                    "root_task_id": "rt-sum",
                    "workflow_run_id": "wr-sum",
                    "followup_type": "delivery_retry",
                    "current_state": "open",
                }
            )
            store.switch_foreground_root_task(
                session_key="session-sum",
                next_root_task_id="rt-sum",
                reason="new_root_created",
            )
            store.record_core_event(
                {
                    "event_id": "ev-sum",
                    "root_task_id": "rt-sum",
                    "workflow_run_id": "wr-sum",
                    "event_type": "late_result_recorded",
                    "event_ts": 10,
                    "event_seq": 1,
                    "payload": {},
                }
            )

            summary = store.summarize_main_closure(limit_roots=10, limit_events=10)
            self.assertEqual(summary["foreground_root_task_id"], "rt-sum")
            self.assertEqual(summary["active_root_count"], 1)
            self.assertEqual(summary["delivery_failed_count"], 1)
            self.assertEqual(summary["adoption_pending_count"], 1)
            self.assertEqual(summary["late_result_count"], 1)
            self.assertEqual(summary["roots"][0]["workflow_state"], "delivery_failed")
            self.assertIn("purity_metrics", summary)
            self.assertIn("workflow_total", summary["purity_metrics"])

    def test_summarize_main_closure_sets_purity_gate_when_shadow_state_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-shadow",
                    "session_key": "session-shadow",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "shadow",
                    "last_user_message": "shadow",
                    "created_at": 10,
                    "updated_at": 20,
                    "completed_at": 20,
                }
            )
            summary = store.summarize_main_closure(limit_roots=10, limit_events=10)
            self.assertFalse(summary["purity_metrics"]["purity_gate_ok"])
            self.assertIn("shadow_state_detected", summary["purity_metrics"]["purity_gate_reasons"])

    def test_get_core_closure_snapshot_for_task_returns_root_workflow_and_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-closure",
                    "session_key": "session-core",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "实现主闭环",
                    "last_user_message": "实现主闭环",
                    "started_at": 10,
                    "last_progress_at": 20,
                    "created_at": 10,
                    "updated_at": 20,
                }
            )
            store.record_task_event("task-closure", "dispatch_started", {"question": "实现主闭环"})
            store.record_task_event(
                "task-closure",
                "pipeline_receipt",
                {"receipt": {"agent": "dev", "phase": "implementation", "action": "started", "ack_id": "ack-dev"}},
            )
            store.reconcile_task_control_action(
                store.get_task("task-closure"),
                {
                    "control_state": "dev_running",
                    "next_action": "await_dev_receipt",
                    "approved_summary": "等待开发回执",
                    "required_receipts": ["dev:completed"],
                    "next_actor": "dev",
                    "claim_level": "phase_verified",
                    "contract": {"id": "delivery_pipeline"},
                    "phase_statuses": [{"agent": "dev", "label": "开发", "state": "running"}],
                },
            )
            store.sync_legacy_task_projection("task-closure")

            snapshot = store.get_core_closure_snapshot_for_task("task-closure")

            self.assertTrue(snapshot["has_core_projection"])
            self.assertEqual(snapshot["root_task"]["root_task_id"], "legacy-root:task-closure")
            self.assertEqual(snapshot["current_workflow_run"]["workflow_run_id"], "legacy-run:task-closure")
            self.assertEqual(snapshot["workflow_state"], "started")
            self.assertTrue(snapshot["needs_followup"])

    def test_get_core_closure_snapshot_for_task_can_skip_legacy_projection_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-no-legacy",
                    "session_key": "session-no-legacy",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "不要自动回落",
                    "last_user_message": "不要自动回落",
                    "created_at": 10,
                    "updated_at": 20,
                }
            )

            snapshot = store.get_core_closure_snapshot_for_task(
                "task-no-legacy",
                allow_legacy_projection=False,
            )

            self.assertFalse(snapshot["has_core_projection"])
            self.assertFalse(snapshot["legacy_projection_used"])
            self.assertIsNone(store.get_root_task("legacy-root:task-no-legacy"))

    def test_summarize_main_closure_sets_purity_gate_when_legacy_projection_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-legacy-purity",
                    "session_key": "session-legacy-purity",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "legacy purity",
                    "last_user_message": "legacy purity",
                    "created_at": 10,
                    "updated_at": 20,
                }
            )
            store.sync_legacy_task_projection("task-legacy-purity")

            summary = store.summarize_main_closure(limit_roots=10, limit_events=10)

            self.assertFalse(summary["purity_metrics"]["purity_gate_ok"])
            self.assertEqual(summary["purity_metrics"]["legacy_projection_root_count"], 1)
            self.assertIn("legacy_projection_detected", summary["purity_metrics"]["purity_gate_reasons"])

    def test_learning_round_trip_and_reflection_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.record_self_evolution_event(
                learning_key="abc",
                event_type="recorded",
                problem_code="missing_pipeline_receipt",
                root_task_id="task-1",
                actor="guardian",
                details={"title": "缺少回执", "summary": "task missing ack", "evidence": {"task_id": "task-1"}},
            )
            store.record_self_evolution_event(
                learning_key="abc",
                event_type="verified",
                problem_code="missing_pipeline_receipt",
                root_task_id="task-1",
                actor="main",
                details={"title": "缺少回执", "summary": "task missing ack", "scenario": "真实任务复测"},
            )
            learning = store.get_self_evolution_projection("abc")
            self.assertEqual(learning["current_state"], "verified")
            self.assertEqual(store.summarize_self_evolution()["reviewed"], 1)
            store.record_reflection_run("scheduled", {"promoted": 1})
            runs = store.list_reflection_runs(limit=5)
            self.assertEqual(runs[0]["summary"]["promoted"], 1)

    def test_watcher_task_round_trip_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_watcher_task(
                {
                    "watcher_task_id": "req-1",
                    "env_id": "primary",
                    "source_agent": "main",
                    "target_agent": "codex",
                    "intent": "THREADED_EXECUTION",
                    "current_state": "completed",
                    "completed_at": 100,
                    "delivered_at": 0,
                    "last_checked_at": 120,
                    "error_count": 1,
                    "payload": {"request_id": "req-1"},
                }
            )
            store.upsert_watcher_task(
                {
                    "watcher_task_id": "req-2",
                    "env_id": "primary",
                    "source_agent": "main",
                    "target_agent": "codex",
                    "intent": "THREADED_EXECUTION",
                    "current_state": "delivered",
                    "completed_at": 90,
                    "delivered_at": 110,
                    "payload": {"request_id": "req-2"},
                }
            )
            store.upsert_watcher_task(
                {
                    "watcher_task_id": "req-3",
                    "env_id": "primary",
                    "source_agent": "main",
                    "target_agent": "codex",
                    "intent": "THREADED_EXECUTION",
                    "current_state": "failed",
                    "in_dlq": True,
                    "payload": {"request_id": "req-3"},
                }
            )
            tasks = store.list_watcher_tasks(env_id="primary", limit=10)
            summary = store.summarize_watcher_tasks(env_id="primary")
            self.assertEqual(len(tasks), 3)
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary["completed"], 2)
            self.assertEqual(summary["delivered"], 1)
            self.assertEqual(summary["undelivered"], 1)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["dlq"], 1)

    def test_summarize_control_plane_reports_ack_and_next_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-ctrl",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "DEV_IMPLEMENTING",
                    "question": "构建量化系统",
                    "last_user_message": "构建量化系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "started"},
                }
            )
            store.upsert_task_contract(
                "task-ctrl",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:started",
                        "pm:completed",
                        "dev:started",
                        "dev:completed",
                        "test:started",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event(
                "task-ctrl",
                "pipeline_receipt",
                {"receipt": {"agent": "pm", "phase": "planning", "action": "completed"}},
            )
            store.record_task_event(
                "task-ctrl",
                "pipeline_receipt",
                {"receipt": {"agent": "dev", "phase": "implementation", "action": "started"}},
            )
            store.reconcile_task_control_action(store.get_task("task-ctrl"), store.derive_task_control_state("task-ctrl"))
            summary = store.summarize_control_plane(env_id="primary")
            self.assertEqual(summary["tasks"]["recoverable"], 1)
            self.assertEqual(summary["tasks"]["next_actor_counts"]["dev"], 1)

    def test_summarize_control_plane_counts_protocol_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-proto",
                    "session_key": "session-proto",
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
            store.record_task_event("task-proto", "protocol_violation", {"violation_kind": "duplicate_final"})

            summary = store.summarize_control_plane(env_id="primary")

            self.assertEqual(summary["tasks"]["protocol_violations"], 1)

    def test_derive_task_control_state_distinguishes_weak_and_strong_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-weak",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "任务A",
                    "last_user_message": "任务A",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "completed_at": 3,
                }
            )
            store.record_task_event("task-weak", "dispatch_started", {"question": "任务A"})
            store.record_task_event("task-weak", "dispatch_complete", {"status": "completed"})

            weak = store.derive_task_control_state("task-weak")
            self.assertEqual(weak["evidence_level"], "weak")
            self.assertEqual(weak["control_state"], "received_only")

            now = int(time.time())
            store.upsert_task(
                {
                    "task_id": "task-strong",
                    "session_key": "session-b",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "implementation:started",
                    "question": "任务B",
                    "last_user_message": "任务B",
                    "started_at": now - 10,
                    "last_progress_at": now - 5,
                    "created_at": now - 10,
                    "updated_at": now - 5,
                }
            )
            store.record_task_event("task-strong", "dispatch_started", {"question": "任务B"})
            store.record_task_event(
                "task-strong",
                "pipeline_receipt",
                {
                    "receipt": {
                        "agent": "dev",
                        "phase": "implementation",
                        "action": "started",
                        "evidence": "files=3",
                    }
                },
            )
            strong = store.derive_task_control_state("task-strong")
            self.assertEqual(strong["evidence_level"], "strong")
            self.assertEqual(strong["control_state"], "dev_running")
            self.assertIn("protocol", strong)
            self.assertEqual(strong["protocol"]["request"], "seen")
            self.assertEqual(strong["protocol"]["confirmed"], "seen")
            self.assertIn("evidence_summary", strong)
            self.assertEqual(strong["active_phase"], "implementation")
            self.assertEqual(strong["timing"]["profile"], "long")
            self.assertTrue(strong["heartbeat_ok"])

    def test_derive_task_control_state_marks_missing_heartbeat_followup_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            now = int(time.time())
            store.upsert_task(
                {
                    "task_id": "task-heartbeat-stale",
                    "session_key": "session-heartbeat",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "implementation:started",
                    "question": "实现一个新系统",
                    "last_user_message": "实现一个新系统",
                    "started_at": now - 600,
                    "last_progress_at": now - 400,
                    "created_at": now - 600,
                    "updated_at": now - 400,
                }
            )
            store.upsert_task_contract(
                "task-heartbeat-stale",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-heartbeat-stale", "dispatch_started", {"question": "实现一个新系统"})
            store.record_task_event(
                "task-heartbeat-stale",
                "pipeline_receipt",
                {"receipt": {"agent": "dev", "phase": "implementation", "action": "started", "evidence": "files=2"}},
            )
            control = store.derive_task_control_state("task-heartbeat-stale")
            self.assertEqual(control["control_state"], "dev_running")
            self.assertEqual(control["followup_stage"], "soft")
            self.assertFalse(control["heartbeat_ok"])
            self.assertIn("正在追证", control["user_visible_progress"])

    def test_derive_task_control_state_marks_missing_receipt_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-blocked",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "等待结构化回执",
                    "question": "任务A",
                    "last_user_message": "任务A",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            control = store.derive_task_control_state("task-blocked")
            self.assertEqual(control["control_state"], "blocked_unverified")
            self.assertEqual(control["next_action"], "manual_or_session_recovery")
            self.assertEqual(control["truth_level"], "core_projection")
            self.assertEqual(control["core_supervision"]["workflow_state"], "blocked")
            self.assertEqual(control["contract"]["mode"], "observation_template")

    def test_derive_task_control_state_detects_pipeline_detached_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-detached",
                    "session_key": "session-detached",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "implementation:started",
                    "question": "开发一个记事本系统",
                    "last_user_message": "开发一个记事本系统",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 30,
                    "created_at": 1,
                    "updated_at": 30,
                }
            )
            store.upsert_task_contract(
                "task-detached",
                {
                    "id": "delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-detached", "dispatch_started", {"question": "开发一个记事本系统"})
            store.record_task_event("task-detached", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm"}})
            store.record_task_event("task-detached", "stage_progress", {"marker": "implementation:started", "stage": "implementation:started"})
            control = store.derive_task_control_state("task-detached")
            self.assertEqual(control["next_action"], "manual_or_session_recovery")
            self.assertEqual(control["pipeline_recovery"]["rebind_target"], "dev")
            self.assertIn("流水线", control["approved_summary"])
            self.assertTrue(control["native_state"]["pipeline_receipt_seen"])
            self.assertFalse(control["heuristic_state"]["visible_completion_seen"])

    def test_derive_task_control_state_waits_for_receipt_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-recovering",
                    "session_key": "session-recovering",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "恢复中:pipeline_detached",
                    "question": "开发一个记事本系统",
                    "last_user_message": "开发一个记事本系统",
                    "blocked_reason": "",
                    "started_at": 1,
                    "last_progress_at": 40,
                    "created_at": 1,
                    "updated_at": 40,
                }
            )
            store.upsert_task_contract(
                "task-recovering",
                {
                    "id": "delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-recovering", "dispatch_started", {"question": "开发一个记事本系统"})
            store.record_task_event("task-recovering", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm"}})
            store.record_task_event(
                "task-recovering",
                "recovery_succeeded",
                {"recovery_kind": "pipeline_detached", "rebind_target": "dev"},
            )

            control = store.derive_task_control_state("task-recovering")

            self.assertEqual(control["next_action"], "await_receipt_after_recovery")
            self.assertEqual(control["next_actor"], "dev")

    def test_visible_completion_corrects_native_delivery_pending_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            now = int(time.time())
            store.upsert_task(
                {
                    "task_id": "task-native",
                    "session_key": "session-native",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "已形成结论",
                    "question": "修复主脑闭环",
                    "last_user_message": "修复主脑闭环",
                    "started_at": now - 20,
                    "last_progress_at": now - 10,
                    "created_at": now - 20,
                    "updated_at": now - 5,
                }
            )
            store.upsert_root_task(
                {
                    "root_task_id": "rt-native",
                    "session_key": "session-native",
                    "origin_request_id": "task-native",
                    "user_goal_summary": "修复主脑闭环",
                    "status": "open",
                    "current_workflow_run_id": "wf-native",
                    "active": True,
                    "created_at": now - 20,
                    "updated_at": now - 5,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wf-native",
                    "root_task_id": "rt-native",
                    "workflow_type": "direct_main",
                    "current_state": "delivery_pending",
                    "state_reason": "awaiting_visible_reply",
                    "created_at": now - 20,
                    "updated_at": now - 5,
                }
            )
            store.upsert_finalizer_record(
                {
                    "finalization_id": "fin-native",
                    "root_task_id": "rt-native",
                    "workflow_run_id": "wf-native",
                    "decision_state": "finalized",
                    "final_status": "completed",
                    "delivery_state": "delivery_pending",
                    "user_visible_summary": "修复已完成",
                    "created_at": now - 10,
                    "updated_at": now - 5,
                }
            )
            store.upsert_followup(
                {
                    "followup_id": "fu-native",
                    "root_task_id": "rt-native",
                    "workflow_run_id": "wf-native",
                    "followup_type": "delivery_retry",
                    "trigger_reason": "await_delivery_confirmation",
                    "current_state": "open",
                    "suggested_action": "await_delivery_confirmation",
                    "created_by": "guardian",
                    "created_at": now - 4,
                    "updated_at": now - 4,
                }
            )
            store.record_task_event("task-native", "visible_completion", {"message": "主人，修复已经完成，你现在可以直接使用了。"})

            snapshot = store.get_core_closure_snapshot_for_task("task-native", allow_legacy_projection=False)
            supervision = store.derive_core_task_supervision("task-native")
            control = store.derive_task_control_state("task-native")

            # 减法重构：visible_completion 不再自动确认 delivery
            # 真正的 delivered 必须来自结构化 delivery 记录
            self.assertEqual(snapshot["delivery_state"], "delivery_pending")
            self.assertEqual(snapshot["delivery_confirmation_level"], "")
            self.assertTrue(snapshot["visible_completion_seen"])
            # 没有 delivery_confirmed，任务仍需 followup
            self.assertTrue(snapshot["needs_followup"])
            self.assertFalse(snapshot["is_terminal"])
            self.assertFalse(supervision["delivery_confirmed"])
            # next_action 仍要求 delivery 确认
            self.assertIn("delivery", control["next_action"] or "await")

    def test_derive_task_control_state_detects_pipeline_detached_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-detached",
                    "session_key": "session-detached",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "implementation:started",
                    "question": "开发一个记事本系统",
                    "last_user_message": "开发一个记事本系统",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 30,
                    "created_at": 1,
                    "updated_at": 30,
                }
            )
            store.upsert_task_contract(
                "task-detached",
                {
                    "id": "delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-detached", "dispatch_started", {"question": "开发一个记事本系统"})
            store.record_task_event("task-detached", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm"}})
            store.record_task_event("task-detached", "stage_progress", {"marker": "implementation:started", "stage": "implementation:started"})
            control = store.derive_task_control_state("task-detached")
            self.assertEqual(control["next_action"], "manual_or_session_recovery")
            self.assertEqual(control["pipeline_recovery"]["rebind_target"], "dev")
            self.assertIn("流水线", control["approved_summary"])

    def test_delivery_contract_requires_dev_receipt_after_pm_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-pipeline",
                    "session_key": "session-pipeline",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "planning:completed",
                    "question": "实现一个新系统",
                    "last_user_message": "实现一个新系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-pipeline",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:started",
                        "pm:completed",
                        "dev:started",
                        "dev:completed",
                        "test:started",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event("task-pipeline", "dispatch_started", {"question": "实现一个新系统"})
            store.record_task_event(
                "task-pipeline",
                "pipeline_receipt",
                {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "evidence": "方案完成"}},
            )

            control = store.derive_task_control_state("task-pipeline")
            self.assertEqual(control["control_state"], "planning_only")
            self.assertEqual(control["next_action"], "require_dev_receipt")
            self.assertEqual(control["next_actor"], "dev")
            self.assertEqual(control["claim_level"], "phase_verified")
            self.assertIn("dev:started", control["missing_receipts"])
            self.assertEqual(control["phase_statuses"][0]["state"], "completed")
            self.assertEqual(control["phase_statuses"][1]["state"], "pending")

    def test_quant_contract_requires_verifier_after_calculator_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-quant",
                    "session_key": "session-quant",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "calculator:completed",
                    "question": "做一轮量化回测",
                    "last_user_message": "做一轮量化回测",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-quant",
                {
                    "id": "quant_guarded",
                    "required_receipts": [
                        "calculator:started",
                        "calculator:completed",
                        "verifier:completed",
                    ],
                },
            )
            store.record_task_event("task-quant", "dispatch_started", {"question": "做一轮量化回测"})
            store.record_task_event(
                "task-quant",
                "pipeline_receipt",
                {"receipt": {"agent": "calculator", "phase": "analysis", "action": "completed", "evidence": "收益率=12%"}},
            )

            control = store.derive_task_control_state("task-quant")
            self.assertEqual(control["control_state"], "awaiting_verifier")
            self.assertEqual(control["next_action"], "require_verifier_receipt")
            self.assertEqual(control["next_actor"], "verifier")
            self.assertIn("verifier:completed", control["missing_receipts"])
            self.assertEqual(control["phase_statuses"][0]["state"], "completed")
            self.assertEqual(control["phase_statuses"][1]["state"], "pending")

    def test_reconcile_task_control_action_creates_pending_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-pipeline",
                    "session_key": "session-pipeline",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "planning:completed",
                    "question": "实现一个新系统",
                    "last_user_message": "实现一个新系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-pipeline",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:started",
                        "pm:completed",
                        "dev:started",
                        "dev:completed",
                        "test:started",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event("task-pipeline", "dispatch_started", {"question": "实现一个新系统"})
            store.record_task_event(
                "task-pipeline",
                "pipeline_receipt",
                {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "evidence": "方案完成"}},
            )

            control = store.derive_task_control_state("task-pipeline")
            action = store.reconcile_task_control_action(store.get_task("task-pipeline"), control)

            self.assertIsNotNone(action)
            self.assertEqual(action["action_type"], "require_dev_receipt")
            self.assertEqual(action["status"], "pending")
            self.assertIn("dev:started", action["required_receipts"])

    def test_reconcile_task_control_action_supersedes_old_action_after_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-pipeline",
                    "session_key": "session-pipeline",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "planning:completed",
                    "question": "实现一个新系统",
                    "last_user_message": "实现一个新系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-pipeline",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:started",
                        "pm:completed",
                        "dev:started",
                        "dev:completed",
                        "test:started",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event("task-pipeline", "dispatch_started", {"question": "实现一个新系统"})
            store.record_task_event(
                "task-pipeline",
                "pipeline_receipt",
                {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "evidence": "方案完成"}},
            )
            initial = store.derive_task_control_state("task-pipeline")
            store.reconcile_task_control_action(store.get_task("task-pipeline"), initial)

            store.record_task_event(
                "task-pipeline",
                "pipeline_receipt",
                {"receipt": {"agent": "dev", "phase": "implementation", "action": "started", "evidence": "files=2"}},
            )
            next_control = store.derive_task_control_state("task-pipeline")
            next_action = store.reconcile_task_control_action(store.get_task("task-pipeline"), next_control)
            actions = store.list_task_control_actions(task_id="task-pipeline", limit=10)

            self.assertEqual(next_action["action_type"], "await_dev_receipt")
            self.assertTrue(any(item["action_type"] == "require_dev_receipt" and item["status"] == "resolved" for item in actions))
            self.assertTrue(any(item["action_type"] == "await_dev_receipt" and item["status"] in {"pending", "sent"} for item in actions))

    def test_reconcile_task_control_action_resolves_when_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-quant",
                    "session_key": "session-quant",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "做一轮量化回测",
                    "last_user_message": "做一轮量化回测",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "completed_at": 3,
                }
            )
            store.upsert_task_contract(
                "task-quant",
                {
                    "id": "quant_guarded",
                    "required_receipts": [
                        "calculator:started",
                        "calculator:completed",
                        "verifier:completed",
                    ],
                },
            )
            store.record_task_event("task-quant", "dispatch_started", {"question": "做一轮量化回测"})
            store.record_task_event(
                "task-quant",
                "pipeline_receipt",
                {"receipt": {"agent": "calculator", "phase": "analysis", "action": "completed", "evidence": "收益率=12%"}},
            )
            control = store.derive_task_control_state("task-quant")
            store.reconcile_task_control_action(store.get_task("task-quant"), control)
            store.record_task_event(
                "task-quant",
                "pipeline_receipt",
                {"receipt": {"agent": "verifier", "phase": "review", "action": "completed", "evidence": "复核通过"}},
            )
            store.record_task_event("task-quant", "dispatch_complete", {"status": "completed"})
            completed_control = store.derive_task_control_state("task-quant")
            store.reconcile_task_control_action(store.get_task("task-quant"), completed_control)
            action = store.get_open_control_action("task-quant")

            self.assertEqual(completed_control["control_state"], "completed_verified")
            self.assertIsNone(action)

    def test_derive_task_control_state_for_native_root_uses_core_followup_not_legacy_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-native",
                    "session_key": "session-native",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "implementation:started",
                    "question": "继续推进主闭环",
                    "last_user_message": "继续推进主闭环",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_root_task(
                {
                    "root_task_id": "rt-native",
                    "session_key": "session-native",
                    "origin_request_id": "task-native",
                    "origin_message_id": "msg-native",
                    "user_goal_summary": "继续推进主闭环",
                    "intent_type": "delivery",
                    "contract_type": "delivery_pipeline",
                    "status": "open",
                    "current_workflow_run_id": "wr-native",
                    "active": True,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wr-native",
                    "root_task_id": "rt-native",
                    "idempotency_key": "wr-native",
                    "workflow_type": "delivery_pipeline",
                    "intent_type": "delivery",
                    "contract_type": "delivery_pipeline",
                    "current_state": "started",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_followup(
                {
                    "followup_id": "fu-native",
                    "root_task_id": "rt-native",
                    "workflow_run_id": "wr-native",
                    "followup_type": "delivery_retry",
                    "trigger_reason": "delivery.failed",
                    "current_state": "open",
                    "suggested_action": "delivery_retry",
                    "created_by": "main",
                    "created_at": 3,
                    "updated_at": 4,
                    "metadata": {
                        "summary": "送达失败，需要重试。",
                        "details": {"recovery_attempt": 2, "next_actor": "main"},
                    },
                }
            )
            store.record_core_event(
                {
                    "event_id": "ev-native-followup",
                    "root_task_id": "rt-native",
                    "workflow_run_id": "wr-native",
                    "followup_id": "fu-native",
                    "event_type": "followup_requested",
                    "event_ts": 4,
                    "event_seq": 1,
                    "payload": {"reason": "delivery.failed", "followup_id": "fu-native"},
                }
            )
            store.rebuild_workflow_projection("wr-native")

            control = store.derive_task_control_state("task-native")
            action = store.reconcile_task_control_action(store.get_task("task-native"), control)

            self.assertEqual(control["truth_level"], "core_projection")
            self.assertEqual(control["control_action"]["source"], "core_followup")
            self.assertEqual(control["control_action"]["action_type"], "delivery_retry")
            self.assertEqual(control["control_action"]["attempts"], 2)
            self.assertIsNone(store.get_open_control_action("task-native"))
            self.assertEqual(action["source"], "core_followup")

    def test_get_core_snapshot_for_native_root_purges_same_task_legacy_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-native-clean",
                    "session_key": "session-native-clean",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "native clean",
                    "last_user_message": "native clean",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.sync_legacy_task_projection("task-native-clean")
            self.assertIsNotNone(store.get_root_task("legacy-root:task-native-clean"))
            store.upsert_root_task(
                {
                    "root_task_id": "rt-native-clean",
                    "session_key": "session-native-clean",
                    "origin_request_id": "task-native-clean",
                    "origin_message_id": "msg-native-clean",
                    "user_goal_summary": "native clean",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "status": "open",
                    "current_workflow_run_id": "wf-native-clean",
                    "active": True,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wf-native-clean",
                    "root_task_id": "rt-native-clean",
                    "idempotency_key": "wf-native-clean",
                    "workflow_type": "pm_dev_test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "current_state": "started",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )

            snapshot = store.get_core_closure_snapshot_for_task("task-native-clean", allow_legacy_projection=False)

            self.assertEqual(snapshot["root_task_id"], "rt-native-clean")
            self.assertIsNone(store.get_root_task("legacy-root:task-native-clean"))
            self.assertIsNone(store.get_workflow_run("legacy-run:task-native-clean"))


    def test_derive_task_control_state_exposes_v2_truth_completed_vs_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-v2",
                    "session_key": "session-v2",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "testing:completed",
                    "question": "集成主链路",
                    "last_user_message": "集成主链路",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-v2",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                    "terminal_receipts": ["test:completed", "dev:blocked", "test:blocked"],
                },
            )
            store.record_task_event("task-v2", "dispatch_started", {"question": "集成主链路"})
            for receipt in [
                {"agent": "pm", "phase": "planning", "action": "completed", "evidence": "plan=ready"},
                {"agent": "dev", "phase": "implementation", "action": "completed", "evidence": "files=3"},
                {"agent": "test", "phase": "testing", "action": "completed", "evidence": "tests=12/12"},
            ]:
                store.record_task_event("task-v2", "pipeline_receipt", {"receipt": receipt})

            before_delivery = store.derive_task_control_state("task-v2")
            self.assertEqual(before_delivery["control_state"], "test_running")
            self.assertEqual(before_delivery["v2_truth"]["state"], "awaiting_delivery")
            self.assertTrue(before_delivery["v2_truth"]["completed"])
            self.assertFalse(before_delivery["v2_truth"]["delivered"])

            store.record_task_event("task-v2", "dispatch_complete", {"status": "completed"})
            store.update_task_fields("task-v2", status="completed", completed_at=3, updated_at=3)
            delivered = store.derive_task_control_state("task-v2")
            self.assertEqual(delivered["control_state"], "completed_verified")
            self.assertEqual(delivered["v2_truth"]["state"], "delivered")
            self.assertTrue(delivered["v2_truth"]["delivered"])
            self.assertEqual(delivered["public_control_state"], "delivered")

    def test_derive_core_task_supervision_delivery_pending_without_confirmation(self):
        """当 workflow_state == 'delivery_pending' 且 delivery_confirmed == False 时，control_state 应为 'delivery_pending'"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            task_id = "task-delivery-pending-no-conf"
            store.upsert_task(
                {
                    "task_id": task_id,
                    "session_key": "session-delivery-pending",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "delivery_pending",
                    "question": "delivery pending test",
                    "last_user_message": "delivery pending test",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_root_task(
                {
                    "root_task_id": "rt-delivery-pending",
                    "session_key": "session-delivery-pending",
                    "origin_request_id": task_id,
                    "origin_message_id": "msg-delivery-pending",
                    "user_goal_summary": "delivery pending test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "status": "open",
                    "current_workflow_run_id": "wf-delivery-pending",
                    "active": True,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            # 设置 workflow_state == "delivery_pending"，但不设置 delivery_confirmed
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wf-delivery-pending",
                    "root_task_id": "rt-delivery-pending",
                    "idempotency_key": "wf-delivery-pending",
                    "workflow_type": "pm_dev_test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "current_state": "delivery_pending",  # 关键：设置 workflow_state
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            result = store.derive_core_task_supervision(task_id)
            self.assertEqual(result["control_state"], "delivery_pending", 
                "当 workflow_state == 'delivery_pending' 且 delivery_confirmed == False 时，control_state 应为 'delivery_pending'")

    def test_derive_core_task_supervision_delivery_pending_with_confirmation(self):
        """当 workflow_state == 'delivery_pending' 且 delivery_confirmed == True 时，control_state 应为 'completed_verified'"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            task_id = "task-delivery-pending-conf"
            store.upsert_task(
                {
                    "task_id": task_id,
                    "session_key": "session-delivery-pending-conf",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "delivery_pending",
                    "question": "delivery pending confirmed test",
                    "last_user_message": "delivery pending confirmed test",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_root_task(
                {
                    "root_task_id": "rt-delivery-pending-conf",
                    "session_key": "session-delivery-pending-conf",
                    "origin_request_id": task_id,
                    "origin_message_id": "msg-delivery-pending-conf",
                    "user_goal_summary": "delivery pending confirmed test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "status": "open",
                    "current_workflow_run_id": "wf-delivery-pending-conf",
                    "active": True,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wf-delivery-pending-conf",
                    "root_task_id": "rt-delivery-pending-conf",
                    "idempotency_key": "wf-delivery-pending-conf",
                    "workflow_type": "pm_dev_test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "current_state": "delivery_pending",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            # 设置 delivery_confirmed = True
            store.upsert_delivery_attempt(
                {
                    "delivery_attempt_id": "da-delivery-pending-conf",
                    "root_task_id": "rt-delivery-pending-conf",
                    "workflow_run_id": "wf-delivery-pending-conf",
                    "delivery_state": "confirmed",
                    "current_state": "confirmed",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            result = store.derive_core_task_supervision(task_id)
            self.assertEqual(result["control_state"], "completed_verified",
                "当 workflow_state == 'delivery_pending' 且 delivery_confirmed == True 时，control_state 应为 'completed_verified'")

    def test_derive_core_task_supervision_delivered_state(self):
        """当 workflow_state == 'delivered' 时，control_state 应为 'completed_verified'"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            task_id = "task-delivered"
            store.upsert_task(
                {
                    "task_id": task_id,
                    "session_key": "session-delivered",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "delivered",
                    "question": "delivered test",
                    "last_user_message": "delivered test",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_root_task(
                {
                    "root_task_id": "rt-delivered",
                    "session_key": "session-delivered",
                    "origin_request_id": task_id,
                    "origin_message_id": "msg-delivered",
                    "user_goal_summary": "delivered test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "status": "completed",
                    "current_workflow_run_id": "wf-delivered",
                    "active": True,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wf-delivered",
                    "root_task_id": "rt-delivered",
                    "idempotency_key": "wf-delivered",
                    "workflow_type": "pm_dev_test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "current_state": "delivered",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            result = store.derive_core_task_supervision(task_id)
            self.assertEqual(result["control_state"], "completed_verified",
                "当 workflow_state == 'delivered' 时，control_state 应为 'completed_verified'")

    def test_derive_task_control_state_native_completed_without_delivery_stays_followup_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            task_id = "task-native-completed"
            store.upsert_task(
                {
                    "task_id": task_id,
                    "session_key": "session-native-completed",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "testing:completed",
                    "question": "native completed without delivery",
                    "last_user_message": "native completed without delivery",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_root_task(
                {
                    "root_task_id": "rt-native-completed",
                    "session_key": "session-native-completed",
                    "origin_request_id": task_id,
                    "origin_message_id": "msg-native-completed",
                    "user_goal_summary": "native completed without delivery",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "status": "open",
                    "current_workflow_run_id": "wf-native-completed",
                    "active": True,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_workflow_run(
                {
                    "workflow_run_id": "wf-native-completed",
                    "root_task_id": "rt-native-completed",
                    "idempotency_key": "wf-native-completed",
                    "workflow_type": "pm_dev_test",
                    "intent_type": "delivery",
                    "contract_type": "pm_dev_test",
                    "current_state": "completed",
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            control = store.derive_task_control_state(task_id)
            self.assertNotEqual(control["control_state"], "completed_verified")
            self.assertEqual(control["next_action"], "await_delivery_confirmation")
            self.assertEqual(control["public_control_state"], "followup_pending")


if __name__ == "__main__":
    unittest.main()
