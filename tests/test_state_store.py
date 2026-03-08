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
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
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
            self.assertIn("dev:started", control["missing_receipts"])

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
            self.assertIn("verifier:completed", control["missing_receipts"])

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


if __name__ == "__main__":
    unittest.main()
