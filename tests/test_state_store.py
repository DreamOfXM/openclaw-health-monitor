import sqlite3
import tempfile
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
                store.append_runtime_event("restart_events:official", {"seq": idx}, limit=3)

            events = store.load_runtime_value("restart_events:official", [])
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

    def test_learning_round_trip_and_reflection_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            learning = store.upsert_learning(
                learning_key="abc",
                env_id="primary",
                task_id="task-1",
                category="control_plane",
                title="缺少回执",
                detail="task missing ack",
                evidence={"task_id": "task-1"},
            )
            learning = store.upsert_learning(
                learning_key="abc",
                env_id="primary",
                task_id="task-1",
                category="control_plane",
                title="缺少回执",
                detail="task missing ack",
                evidence={"task_id": "task-1"},
                status="reviewed",
            )
            self.assertEqual(learning["occurrences"], 2)
            self.assertEqual(store.summarize_learnings()["reviewed"], 1)
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
            self.assertIn("protocol", strong)
            self.assertEqual(strong["protocol"]["request"], "seen")
            self.assertEqual(strong["protocol"]["confirmed"], "seen")
            self.assertIn("evidence_summary", strong)

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
            self.assertEqual(control["truth_level"], "derived")
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


if __name__ == "__main__":
    unittest.main()
