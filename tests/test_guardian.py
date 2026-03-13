import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import guardian
from state_store import MonitorStateStore


class GuardianRuntimeAnomalyTests(unittest.TestCase):
    def test_collect_runtime_anomalies_flags_no_reply(self):
        lines = [
            "2026-03-06T05:00:00 dm from tester: 帮我查一下状态\n",
            "2026-03-06T05:00:01 dispatching to agent\n",
            "2026-03-06T05:00:35 dispatch complete (queuedFinal=false, replies=0)\n",
        ]

        anomalies, latest_signature = guardian.collect_runtime_anomalies(
            lines,
            now=0,
            slow_threshold=30,
            stalled_threshold=90,
        )

        self.assertEqual(latest_signature.strip(), lines[-1].strip())
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "no_reply")
        self.assertEqual(anomalies[0]["details"]["question"], "帮我查一下状态")
        self.assertEqual(anomalies[0]["details"]["duration"], 34)

    def test_collect_runtime_anomalies_flags_stage_stuck(self):
        lines = [
            "2026-03-06T05:00:00 message in room: 继续执行任务\n",
            "2026-03-06T05:00:01 dispatching to agent\n",
            "2026-03-06T05:00:10 PIPELINE_PROGRESS: planning\n",
        ]
        _, progress_ts = guardian.parse_runtime_timestamp(lines[-1])

        anomalies, _ = guardian.collect_runtime_anomalies(
            lines,
            now=(progress_ts or 0) + 120,
            slow_threshold=30,
            stalled_threshold=90,
        )

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "stage_stuck")
        self.assertEqual(anomalies[0]["details"]["marker"], "planning")
        self.assertEqual(anomalies[0]["details"]["question"], "继续执行任务")

    def test_scan_runtime_anomalies_dedupes_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我查一下状态",
                        "2026-03-06T05:00:01 dispatching to agent",
                        "2026-03-06T05:00:35 dispatch complete (queuedFinal=false, replies=0)",
                        "2026-03-06T05:01:00 Error: gateway closed (1006 abnormal closure (no close frame))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recorded_changes = []
            notifications = []

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"SLOW_RESPONSE_THRESHOLD": 30, "STALLED_RESPONSE_THRESHOLD": 90}), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "record_change_log", side_effect=lambda ctype, msg, details=None: recorded_changes.append((ctype, msg, details))), \
                mock.patch.object(guardian, "should_alert", return_value=True), \
                mock.patch.object(guardian, "notify", side_effect=lambda title, message, level="info": notifications.append((title, message, level))):
                first = guardian.scan_runtime_anomalies()
                second = guardian.scan_runtime_anomalies()

            self.assertEqual([item["type"] for item in first], ["no_reply", "gateway_ws_closed"])
            self.assertEqual(second, [])
            self.assertEqual(len(recorded_changes), 2)
            self.assertEqual(len(notifications), 2)
            seen = store.load_runtime_value("runtime_anomaly_seen", {})
            self.assertEqual(len(seen), 2)


class GuardianProgressPushTests(unittest.TestCase):
    def test_format_duration_label(self):
        self.assertEqual(guardian.format_duration_label(45), "45秒")
        self.assertEqual(guardian.format_duration_label(120), "2分钟")
        self.assertEqual(guardian.format_duration_label(220), "3分40秒")
        self.assertEqual(guardian.format_duration_label(3661), "1小时1分钟")

    def test_send_feishu_progress_push_prefixes_user_target(self):
        commands = []
        logs = []

        with mock.patch.object(
            guardian,
            "run_cmd",
            side_effect=lambda cmd: commands.append(cmd) or (0, "", ""),
        ), mock.patch.object(
            guardian,
            "log",
            side_effect=lambda msg, level="INFO": logs.append((level, msg)),
        ):
            ok = guardian.send_feishu_progress_push("ou_test", "进度正常")

        self.assertTrue(ok)
        self.assertIn('--target "user:ou_test"', commands[0])
        self.assertIn("user:ou_test", logs[0][1])

    def test_classify_guardian_followup_error(self):
        self.assertEqual(
            guardian.classify_guardian_followup_error("session file locked (timeout 10000ms)"),
            "session_lock",
        )
        self.assertEqual(
            guardian.classify_guardian_followup_error("OAuth token refresh failed for qwen-portal"),
            "model_auth",
        )
        self.assertEqual(
            guardian.classify_guardian_followup_error("HTTP 404: 404 page not found (model_not_found)"),
            "model_unavailable",
        )

    def test_restart_gateway_official_stops_primary_and_official_before_start(self):
        calls = []

        def fake_run_args(args, timeout=None):
            calls.append((list(args), timeout))
            if args[0] == str(guardian.OFFICIAL_MANAGER) and args[1] == "start":
                return (0, "started", "")
            return (0, "", "")

        with mock.patch.object(guardian, "current_env_spec", return_value={"id": "official"}), \
            mock.patch.object(guardian, "run_args", side_effect=fake_run_args), \
            mock.patch.object(guardian, "check_gateway_health", return_value=True), \
            mock.patch.object(guardian, "commit_active_binding") as commit_binding, \
            mock.patch.object(guardian, "log"), \
            mock.patch.object(guardian, "STORE") as store:
            ok = guardian.restart_gateway()

        self.assertTrue(ok)
        commit_binding.assert_called_once_with("official")
        self.assertEqual(store.append_runtime_event.call_count, 2)
        self.assertEqual(
            [call[0] for call in calls[:3]],
            [
                [str(guardian.DESKTOP_RUNTIME), "stop", "gateway"],
                [str(guardian.OFFICIAL_MANAGER), "stop"],
                [str(guardian.OFFICIAL_MANAGER), "start"],
            ],
        )

    def test_restart_gateway_primary_stops_official_before_starting_active_gateway(self):
        calls = []

        def fake_run_args(args, timeout=None):
            calls.append((list(args), timeout))
            if args[0] == str(guardian.DESKTOP_RUNTIME) and args[1:] == ["start", "gateway"]:
                return (0, "started", "")
            return (0, "", "")

        with mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}), \
            mock.patch.object(guardian, "run_args", side_effect=fake_run_args), \
            mock.patch.object(guardian, "check_gateway_health", return_value=True), \
            mock.patch.object(guardian, "commit_active_binding") as commit_binding, \
            mock.patch.object(guardian.time, "sleep"), \
            mock.patch.object(guardian, "log"), \
            mock.patch.object(guardian, "STORE") as store:
            ok = guardian.restart_gateway()

        self.assertTrue(ok)
        commit_binding.assert_called_once_with("primary")
        self.assertEqual(store.append_runtime_event.call_count, 2)
        self.assertEqual(
            [call[0] for call in calls[:3]],
            [
                [str(guardian.OFFICIAL_MANAGER), "stop"],
                [str(guardian.DESKTOP_RUNTIME), "stop", "gateway"],
                [str(guardian.DESKTOP_RUNTIME), "start", "gateway"],
            ],
        )


class GuardianLearningDelegationTests(unittest.TestCase):
    def test_capture_control_plane_learnings_skips_when_openclaw_artifacts_ready(self):
        with mock.patch.object(guardian, "CONFIG", {**guardian.DEFAULT_CONFIG, "ENABLE_EVOLUTION_PLANE": True}), \
            mock.patch.object(guardian, "should_delegate_learning_ownership_to_openclaw", return_value=True):
            captured = guardian.capture_control_plane_learnings([
                {"task_id": "task-1", "action": "blocked", "control_state": "blocked_unverified"}
            ])
        self.assertEqual(captured, [])

    def test_run_reflection_cycle_returns_delegated_when_openclaw_artifacts_ready(self):
        with mock.patch.object(guardian, "CONFIG", {**guardian.DEFAULT_CONFIG, "ENABLE_EVOLUTION_PLANE": True}), \
            mock.patch.object(guardian, "should_delegate_learning_ownership_to_openclaw", return_value=True):
            result = guardian.run_reflection_cycle()
        self.assertEqual(result["status"], "delegated")
        self.assertEqual(result["promoted"], 0)

    def test_push_runtime_progress_updates_prefers_guardian_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-progress",
                    "session_key": "agent:main:feishu:direct:ou_test",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "DEV_IMPLEMENTING",
                    "question": "帮我继续处理",
                    "last_user_message": "帮我继续处理",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                        "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                        "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _, progress_ts = guardian.parse_runtime_timestamp(
                "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n"
            )
            followups = []

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "PROGRESS_PUSH_INTERVAL": 180,
                        "PROGRESS_PUSH_COOLDOWN": 300,
                        "PROGRESS_ESCALATION_INTERVAL": 600,
                    },
                ), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "time", wraps=guardian.time) as mock_time, \
                mock.patch.object(
                    guardian,
                    "send_guardian_followup",
                    side_effect=lambda session_key, message, deliver=True: followups.append(
                        (session_key, message, deliver)
                    )
                    or (True, None),
                ), \
                mock.patch.object(guardian, "send_feishu_progress_push") as feishu_push, \
                mock.patch.object(guardian, "record_change_log"):
                mock_time.time.return_value = (progress_ts or 0) + 220
                result = guardian.push_runtime_progress_updates()

            self.assertEqual(result[0]["type"], "progress_push")
            self.assertEqual(followups[0][0], "agent:main:feishu:direct:ou_test")
            self.assertIn("GUARDIAN_FOLLOWUP:", followups[0][1])
            self.assertEqual(result[0]["delivery_channel"], "session")
            feishu_push.assert_not_called()
            events = store.list_task_events("task-progress", limit=10)
            self.assertTrue(any(item["event_type"] == "guardian_progress_push" for item in events))

    def test_deliver_guardian_progress_update_retries_then_falls_back(self):
        dispatch = {
            "session_key": "agent:main:feishu:direct:ou_test",
            "requester_open_id": "ou_test",
        }
        logs = []

        with mock.patch.object(
            guardian,
            "CONFIG",
            {"GUARDIAN_FOLLOWUP_RETRIES": 2, "GUARDIAN_FOLLOWUP_RETRY_DELAY": 0},
        ), mock.patch.object(
            guardian,
            "send_guardian_followup",
            side_effect=[(False, "unknown"), (False, "unknown")],
        ) as followup, mock.patch.object(
            guardian,
            "send_feishu_progress_push",
            return_value=True,
        ) as feishu_push, mock.patch.object(
            guardian,
            "log",
            side_effect=lambda msg, level="INFO": logs.append((level, msg)),
        ):
            channel, reason = guardian.deliver_guardian_progress_update(
                dispatch,
                followup_message="GUARDIAN_FOLLOWUP: test",
                fallback_message="任务暂时没有新的可见进展",
            )

        self.assertEqual(channel, "feishu")
        self.assertEqual(reason, "unknown")
        self.assertEqual(followup.call_count, 2)
        feishu_push.assert_called_once_with("ou_test", "任务暂时没有新的可见进展")
        self.assertTrue(any(level == "WARNING" and "降级" in msg for level, msg in logs))

    def test_collect_open_runtime_dispatches_tracks_latest_progress(self):
        lines = [
            "2026-03-06T05:00:00 dm from tester: 帮我继续处理\n",
            "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
            "2026-03-06T05:01:00 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n",
        ]

        dispatches = guardian.collect_open_runtime_dispatches(lines)

        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["question"], "帮我继续处理")
        self.assertEqual(dispatches[0]["marker"], "DEV_IMPLEMENTING")
        self.assertEqual(dispatches[0]["requester_open_id"], "ou_test")

    def test_collect_open_runtime_dispatches_keeps_latest_for_same_session(self):
        lines = [
            "2026-03-06T05:00:00 dm from tester: 旧问题\n",
            "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
            "2026-03-06T13:00:00 dm from tester: 新问题\n",
            "2026-03-06T13:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
            "2026-03-06T13:00:30 PIPELINE_PROGRESS: TEST_RUNNING\n",
        ]

        dispatches = guardian.collect_open_runtime_dispatches(lines)

        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["question"], "新问题")
        self.assertEqual(dispatches[0]["marker"], "TEST_RUNNING")

    def test_sync_runtime_task_registry_tracks_current_and_completed_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            lines = [
                "2026-03-06T05:00:00 dm from tester: 帮我做一个系统\n",
                "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
                "2026-03-06T05:00:10 PIPELINE_PROGRESS: PM_ANALYZING\n",
                "2026-03-06T05:00:20 PIPELINE_RECEIPT: agent=pm | phase=planning | action=completed | evidence=read=req,repo\n",
                "2026-03-06T05:00:35 dispatch complete (queuedFinal=true, replies=1)\n",
                "2026-03-06T05:01:00 dm from tester: 再加一个模块\n",
                "2026-03-06T05:01:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
                "2026-03-06T05:01:15 PIPELINE_RECEIPT: agent=dev | phase=implementation | action=blocked | evidence=test spawn rejected\n",
            ]

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_TASK_REGISTRY": True}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                guardian.sync_runtime_task_registry(lines)

            tasks = store.list_tasks(limit=10)
            self.assertEqual(len(tasks), 2)
            latest_receipt_task = next(task for task in tasks if (task.get("latest_receipt") or {}).get("agent") == "dev")
            dispatch_complete_task = next(task for task in tasks if task["task_id"] != latest_receipt_task["task_id"])
            self.assertIn(latest_receipt_task["status"], {"blocked", "background"})
            self.assertIn(dispatch_complete_task["status"], {"running", "background"})
            events = store.list_task_events(latest_receipt_task["task_id"], limit=10)
            self.assertTrue(any(item["event_type"] == "pipeline_receipt" for item in events))
            completed_events = store.list_task_events(dispatch_complete_task["task_id"], limit=10)
            self.assertTrue(any(item["event_type"] == "dispatch_complete" for item in completed_events))
            summary_file = base / "data" / "task-registry-summary.json"
            facts_file = base / "data" / "current-task-facts.json"
            self.assertTrue(summary_file.exists())
            self.assertTrue(facts_file.exists())

    def test_sync_runtime_task_registry_creates_initial_control_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            lines = [
                "2026-03-06T05:00:00 dm from tester: 做一轮量化回测\n",
                "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
            ]

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_TASK_REGISTRY": True}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                guardian.sync_runtime_task_registry(lines)

            tasks = store.list_tasks(limit=10)
            self.assertEqual(len(tasks), 1)
            actions = store.list_task_control_actions(task_id=tasks[0]["task_id"], limit=5)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["action_type"], "require_calculator_start")
            self.assertEqual(actions[0]["status"], "pending")

    def test_sync_runtime_task_registry_keeps_dispatch_open_after_queued_final_until_receipts_arrive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            lines = [
                "2026-03-06T05:00:00 dm from tester: 开发一个系统\n",
                "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
                "2026-03-06T05:00:10 dispatch complete (queuedFinal=true, replies=1)\n",
                "2026-03-06T05:00:20 PIPELINE_RECEIPT: agent=pm | phase=planning | action=completed | evidence=plan ready\n",
                "2026-03-06T05:00:25 PIPELINE_RECEIPT: agent=dev | phase=implementation | action=completed | evidence=repo changed\n",
            ]

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_TASK_REGISTRY": True}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                guardian.sync_runtime_task_registry(lines)

            tasks = store.list_tasks(limit=10)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["status"], "running")
            self.assertEqual(tasks[0]["latest_receipt"]["agent"], "dev")
            events = store.list_task_events(tasks[0]["task_id"], limit=10)
            self.assertTrue(any(item["event_type"] == "pipeline_receipt" for item in events))

    def test_extract_pipeline_receipt_rejects_empty_evidence(self):
        self.assertIsNone(
            guardian.extract_pipeline_receipt(
                "2026-03-06T05:00:20 PIPELINE_RECEIPT: agent=dev | phase=implementation | action=started | evidence="
            )
        )

    def test_sync_runtime_task_registry_autofills_missing_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            lines = [
                "2026-03-06T05:00:00 dm from tester: 开发一个系统\n",
                "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
                "2026-03-06T05:00:20 PIPELINE_RECEIPT: agent=pm | phase=planning | action=completed | evidence=plan ready\n",
            ]

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_TASK_REGISTRY": True}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                guardian.sync_runtime_task_registry(lines)

            task = store.list_tasks(limit=5)[0]
            self.assertTrue(task["latest_receipt"]["ack_id"])
            events = store.list_task_events(task["task_id"], limit=10)
            self.assertTrue(any(item["event_type"] == "pipeline_receipt" for item in events))

    def test_sync_runtime_task_registry_rejects_duplicate_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            lines = [
                "2026-03-06T05:00:00 dm from tester: 开发一个系统\n",
                "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
                "2026-03-06T05:00:10 任务已完成 ✅\n",
                "2026-03-06T05:00:20 任务已完成 ✅\n",
            ]

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_TASK_REGISTRY": True}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                guardian.sync_runtime_task_registry(lines)

            task = store.list_tasks(limit=5)[0]
            events = store.list_task_events(task["task_id"], limit=10)
            final_events = [item for item in events if item["event_type"] == "visible_completion"]
            violations = [item for item in events if item["event_type"] == "protocol_violation"]
            self.assertEqual(len(final_events), 1)
            self.assertTrue(any(item["payload"]["violation_kind"] == "duplicate_final" for item in violations))

    def test_extract_runtime_question_strips_json_runtime_metadata(self):
        line = '{"0":"{\\"subsystem\\":\\"gateway/channels/feishu\\"}","1":"Feishu[default] DM from ou_test: 我再提个需求，就是做一个系统","_meta":{"runtime":"node"}}'
        self.assertEqual(
            guardian.extract_runtime_question(line),
            "我再提个需求，就是做一个系统",
        )

    def test_normalize_task_question_rejects_internal_lines(self):
        self.assertEqual(guardian.normalize_task_question("dispatching to agent (session=abc)"), "未知任务")
        self.assertEqual(
            guardian.normalize_task_question("Feishu[default] DM from ou_test: 我再提个需求"),
            "我再提个需求",
        )

    def test_collect_open_runtime_dispatches_stops_after_visible_completion(self):
        lines = [
            "2026-03-06T05:00:00 dm from tester: 帮我继续处理\n",
            "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)\n",
            "2026-03-06T05:01:00 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n",
            "2026-03-06T05:02:00 主人，任务已完成！✅\n",
        ]

        dispatches = guardian.collect_open_runtime_dispatches(lines)

        self.assertEqual(dispatches, [])

    def test_is_visible_completion_message_filters_internal_lines(self):
        self.assertTrue(guardian.is_visible_completion_message("2026-03-06T05:02:00 任务已完成 ✅"))
        self.assertFalse(
            guardian.is_visible_completion_message(
                "2026-03-06T05:02:00 dispatch complete (queuedFinal=true, replies=1)"
            )
        )
        self.assertFalse(
            guardian.is_visible_completion_message(
                "2026-03-06T05:02:00 已完成：当前恢复阶段已识别，等待 dev 继续推进"
            )
        )

    def test_push_runtime_progress_updates_only_when_idle(self):
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
                    "current_stage": "DEV_IMPLEMENTING",
                    "question": "帮我继续处理",
                    "last_user_message": "帮我继续处理",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                        "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                        "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _, progress_ts = guardian.parse_runtime_timestamp(
                "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n"
            )
            pushes = []
            change_logs = []

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "PROGRESS_PUSH_INTERVAL": 180,
                        "PROGRESS_PUSH_COOLDOWN": 300,
                        "PROGRESS_ESCALATION_INTERVAL": 600,
                    },
                ), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "time", wraps=guardian.time) as mock_time, \
                mock.patch.object(
                    guardian,
                    "send_guardian_followup",
                    side_effect=lambda session_key, message, deliver=True: pushes.append(
                        (session_key, message, deliver)
                    )
                    or (True, None),
                ), \
                mock.patch.object(guardian, "send_feishu_progress_push"), \
                mock.patch.object(
                    guardian,
                    "record_change_log",
                    side_effect=lambda ctype, msg, details=None: change_logs.append((ctype, msg, details)),
                ):
                mock_time.time.return_value = (progress_ts or 0) + 120
                first = guardian.push_runtime_progress_updates()
                mock_time.time.return_value = (progress_ts or 0) + 220
                second = guardian.push_runtime_progress_updates()

            self.assertEqual(first, [])
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["type"], "progress_push")
            self.assertEqual(second[0]["idle"], 220)
            self.assertEqual(pushes[0][0], "agent:main:feishu:direct:ou_test")
            self.assertIn("GUARDIAN_FOLLOWUP:", pushes[0][1])
            self.assertEqual(change_logs[0][2]["idle"], 220)
            self.assertEqual(change_logs[0][2]["delivery_channel"], "session")
            events = store.list_task_events("task-1", limit=10)
            self.assertTrue(any(item["event_type"] == "guardian_progress_push" for item in events))

    def test_attach_background_result_if_late_marks_completed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
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
                    "last_progress_at": 10,
                    "created_at": 1,
                    "updated_at": 10,
                    "completed_at": 50,
                }
            )
            store.upsert_task(
                {
                    "task_id": "task-new",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "新任务",
                    "last_user_message": "新任务",
                    "started_at": 20,
                    "last_progress_at": 60,
                    "created_at": 60,
                    "updated_at": 60,
                }
            )
            with mock.patch.object(guardian, "STORE", store):
                guardian.attach_background_result_if_late("task-old", "session-a", completed_at=60, status="completed")
            events = store.list_task_events("task-old", limit=10)
            self.assertTrue(any(item["event_type"] == "background_result" for item in events))

    def test_reconcile_background_results_for_sessions_marks_late_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
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
                    "last_progress_at": 10,
                    "created_at": 1,
                    "updated_at": 10,
                    "completed_at": 80,
                }
            )
            store.upsert_task(
                {
                    "task_id": "task-new",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "处理中",
                    "question": "新任务",
                    "last_user_message": "新任务",
                    "started_at": 20,
                    "last_progress_at": 61,
                    "created_at": 61,
                    "updated_at": 61,
                }
            )
            with mock.patch.object(guardian, "STORE", store):
                guardian.reconcile_background_results_for_sessions({"session-a"})
            events = store.list_task_events("task-old", limit=10)
            self.assertTrue(any(item["event_type"] == "background_result" for item in events))

    def test_should_record_control_plane_anomaly_deduplicates_recent_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            with mock.patch.object(guardian, "STORE", store), mock.patch("time.time", return_value=1000):
                self.assertTrue(guardian.should_record_control_plane_anomaly("task-1", "missing_pipeline_receipt", interval=600))
            with mock.patch.object(guardian, "STORE", store), mock.patch("time.time", return_value=1200):
                self.assertFalse(guardian.should_record_control_plane_anomaly("task-1", "missing_pipeline_receipt", interval=600))
            with mock.patch.object(guardian, "STORE", store), mock.patch("time.time", return_value=1701):
                self.assertTrue(guardian.should_record_control_plane_anomaly("task-1", "missing_pipeline_receipt", interval=600))

    def test_push_runtime_progress_updates_resets_after_new_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                        "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                        "2026-03-06T05:01:00 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _, first_progress_ts = guardian.parse_runtime_timestamp(
                "2026-03-06T05:01:00 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n"
            )
            pushes = []

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "PROGRESS_PUSH_INTERVAL": 180,
                        "PROGRESS_PUSH_COOLDOWN": 300,
                        "PROGRESS_ESCALATION_INTERVAL": 600,
                    },
                ), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "time", wraps=guardian.time) as mock_time, \
                mock.patch.object(
                    guardian,
                    "send_guardian_followup",
                    side_effect=lambda session_key, message, deliver=True: pushes.append(
                        (session_key, message, deliver)
                    )
                    or (True, None),
                ), \
                mock.patch.object(guardian, "send_feishu_progress_push"), \
                mock.patch.object(guardian, "record_change_log"):
                mock_time.time.return_value = (first_progress_ts or 0) + 220
                guardian.push_runtime_progress_updates()

                runtime_log.write_text(
                    "\n".join(
                        [
                            "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                            "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                            "2026-03-06T05:01:00 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                            "2026-03-06T05:05:30 PIPELINE_PROGRESS: TEST_RUNNING",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                _, second_progress_ts = guardian.parse_runtime_timestamp(
                    "2026-03-06T05:05:30 PIPELINE_PROGRESS: TEST_RUNNING\n"
                )
                mock_time.time.return_value = (second_progress_ts or 0) + 120
                second = guardian.push_runtime_progress_updates()
                mock_time.time.return_value = (second_progress_ts or 0) + 220
                third = guardian.push_runtime_progress_updates()

            self.assertEqual(len(pushes), 2)
            self.assertEqual(second, [])
            self.assertEqual(third[0]["type"], "progress_push")
            self.assertIn("GUARDIAN_FOLLOWUP:", pushes[-1][1] or "")
            self.assertIn("3分40秒", pushes[-1][1] or "")

    def test_push_runtime_progress_updates_falls_back_to_feishu_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                        "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                        "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _, progress_ts = guardian.parse_runtime_timestamp(
                "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n"
            )
            messages = []
            change_logs = []

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "PROGRESS_PUSH_INTERVAL": 180,
                        "PROGRESS_PUSH_COOLDOWN": 300,
                        "PROGRESS_ESCALATION_INTERVAL": 600,
                        "GUARDIAN_FOLLOWUP_RETRIES": 2,
                        "GUARDIAN_FOLLOWUP_RETRY_DELAY": 0,
                    },
                ), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "time", wraps=guardian.time) as mock_time, \
                mock.patch.object(guardian, "send_guardian_followup", return_value=(False, "session_lock")) as session_push, \
                mock.patch.object(
                    guardian,
                    "send_feishu_progress_push",
                    side_effect=lambda open_id, message: messages.append((open_id, message)) or True,
                ), \
                mock.patch.object(
                    guardian,
                    "record_change_log",
                    side_effect=lambda ctype, msg, details=None: change_logs.append((ctype, msg, details)),
                ):
                mock_time.time.return_value = (progress_ts or 0) + 220
                result = guardian.push_runtime_progress_updates()

            self.assertEqual(result[0]["delivery_channel"], "feishu")
            self.assertEqual(session_push.call_count, 1)
            self.assertEqual(messages[0][0], "ou_test")
            self.assertIn("任务暂时没有新的可见进展", messages[0][1])
            self.assertEqual(change_logs[0][2]["delivery_channel"], "feishu")
            self.assertEqual(change_logs[0][2]["blocked_reason"], "session_lock")

    def test_push_runtime_progress_updates_enters_blocked_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                        "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                        "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _, progress_ts = guardian.parse_runtime_timestamp(
                "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n"
            )
            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "PROGRESS_PUSH_INTERVAL": 180,
                        "PROGRESS_PUSH_COOLDOWN": 300,
                        "PROGRESS_ESCALATION_INTERVAL": 600,
                        "GUARDIAN_FOLLOWUP_RETRIES": 1,
                        "GUARDIAN_FOLLOWUP_RETRY_DELAY": 0,
                        "GUARDIAN_BLOCKED_COOLDOWN": 900,
                    },
                ), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "time", wraps=guardian.time) as mock_time, \
                mock.patch.object(guardian, "send_guardian_followup", return_value=(False, "session_lock")), \
                mock.patch.object(guardian, "send_feishu_progress_push", return_value=True), \
                mock.patch.object(guardian, "record_change_log"):
                mock_time.time.return_value = (progress_ts or 0) + 220
                first = guardian.push_runtime_progress_updates()
                mock_time.time.return_value = (progress_ts or 0) + 260
                second = guardian.push_runtime_progress_updates()

            self.assertEqual(first[0]["blocked_reason"], "session_lock")
            self.assertEqual(second, [])

    def test_push_runtime_progress_updates_sends_blocked_notice_after_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            runtime_log = base / "runtime.log"
            runtime_log.write_text(
                "\n".join(
                    [
                        "2026-03-06T05:00:00 dm from tester: 帮我继续处理",
                        "2026-03-06T05:00:01 dispatching to agent (session=agent:main:feishu:direct:ou_test)",
                        "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _, progress_ts = guardian.parse_runtime_timestamp(
                "2026-03-06T05:04:30 PIPELINE_PROGRESS: DEV_IMPLEMENTING\n"
            )
            messages = []

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "PROGRESS_PUSH_INTERVAL": 180,
                        "PROGRESS_PUSH_COOLDOWN": 300,
                        "PROGRESS_ESCALATION_INTERVAL": 600,
                        "GUARDIAN_FOLLOWUP_RETRIES": 1,
                        "GUARDIAN_FOLLOWUP_RETRY_DELAY": 0,
                        "GUARDIAN_BLOCKED_COOLDOWN": 60,
                        "GUARDIAN_BLOCKED_NOTICE_INTERVAL": 120,
                    },
                ), \
                mock.patch.object(guardian, "resolve_runtime_gateway_log", return_value=runtime_log), \
                mock.patch.object(guardian, "time", wraps=guardian.time) as mock_time, \
                mock.patch.object(guardian, "send_guardian_followup", return_value=(False, "session_lock")), \
                mock.patch.object(
                    guardian,
                    "send_feishu_progress_push",
                    side_effect=lambda open_id, message: messages.append((open_id, message)) or True,
                ), \
                mock.patch.object(guardian, "record_change_log"):
                mock_time.time.return_value = (progress_ts or 0) + 220
                first = guardian.push_runtime_progress_updates()
                mock_time.time.return_value = (progress_ts or 0) + 340
                second = guardian.push_runtime_progress_updates()

            self.assertEqual(first[0]["type"], "progress_push")
            self.assertEqual(second[0]["type"], "blocked_notice")
            self.assertIn("任务当前已阻塞", messages[-1][1])

    def test_enforce_task_registry_control_plane_marks_ops_attention_for_weak_task_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-weak",
                    "session_key": "agent:main:feishu:direct:ou_test",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "做量化回测",
                    "last_user_message": "做量化回测",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "completed_at": 3,
                }
            )
            store.record_task_event("task-weak", "dispatch_started", {"question": "做量化回测"})
            store.record_task_event("task-weak", "dispatch_complete", {"status": "completed"})
            store.upsert_task_contract(
                "task-weak",
                {
                    "id": "quant_guarded",
                    "required_receipts": [
                        "calculator:started",
                        "calculator:completed",
                        "verifier:completed",
                    ],
                },
            )

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "ENABLE_TASK_REGISTRY": True,
                        "ENABLE_INTRUSIVE_TASK_CONTROL": False,
                        "TASK_REGISTRY_RETENTION": 20,
                        "TASK_CONTROL_RECEIPT_GRACE": 10,
                        "TASK_CONTROL_FOLLOWUP_COOLDOWN": 60,
                        "TASK_CONTROL_MAX_ATTEMPTS": 2,
                        "TASK_CONTROL_BLOCK_TIMEOUT": 300,
                    },
                ), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "send_guardian_followup", return_value=(True, None)) as followup, \
                mock.patch.object(guardian, "record_change_log"):
                with mock.patch.object(guardian.time, "time", return_value=100):
                    outcomes = guardian.enforce_task_registry_control_plane()

            self.assertEqual(outcomes[0]["action"], "ops_attention_needed")
            self.assertEqual(followup.call_count, 0)
            events = store.list_task_events("task-weak", limit=10)
            self.assertTrue(any(item["event_type"] == "ops_attention_needed" for item in events))

    def test_enforce_task_registry_control_plane_blocks_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-weak",
                    "session_key": "agent:main:feishu:direct:ou_test",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "已完成",
                    "question": "做量化回测",
                    "last_user_message": "做量化回测",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "completed_at": 3,
                }
            )
            store.record_task_event("task-weak", "dispatch_started", {"question": "做量化回测"})
            store.record_task_event("task-weak", "dispatch_complete", {"status": "completed"})
            store.upsert_task_contract(
                "task-weak",
                {
                    "id": "quant_guarded",
                    "required_receipts": [
                        "calculator:started",
                        "calculator:completed",
                        "verifier:completed",
                    ],
                },
            )
            action = store.reconcile_task_control_action(
                store.get_task("task-weak"),
                store.derive_task_control_state("task-weak"),
            )
            store.update_control_action(
                int(action["id"]),
                attempts=2,
                last_followup_at=0,
                last_error="unknown",
            )

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "ENABLE_TASK_REGISTRY": True,
                        "TASK_REGISTRY_RETENTION": 20,
                        "TASK_CONTROL_RECEIPT_GRACE": 10,
                        "TASK_CONTROL_FOLLOWUP_COOLDOWN": 60,
                        "TASK_CONTROL_MAX_ATTEMPTS": 2,
                        "TASK_CONTROL_BLOCK_TIMEOUT": 300,
                    },
                ), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}), \
                mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "record_change_log"):
                with mock.patch.object(guardian.time, "time", return_value=100):
                    outcomes = guardian.enforce_task_registry_control_plane()

            task = store.get_task("task-weak")
            self.assertEqual(outcomes[0]["action"], "blocked")
            self.assertEqual(task["status"], "blocked")
            self.assertEqual(task["blocked_reason"], "missing_pipeline_receipt")

    def test_build_control_plane_followup_targets_dev_start(self):
        task = {
            "task_id": "task-dev",
            "question": "做一个新模块",
            "last_user_message": "做一个新模块",
            "current_stage": "planning:completed",
        }
        control = {
            "control_state": "planning_only",
            "next_action": "require_dev_receipt",
            "contract": {"id": "delivery_pipeline"},
            "missing_receipts": ["dev:started", "dev:completed", "test:started", "test:completed"],
        }

        message = guardian.build_control_plane_followup(task, control, idle=300, total=600)
        self.assertIn("任务合同=delivery_pipeline", message)
        self.assertIn("dev", message)
        self.assertIn("缺失回执=dev:started, dev:completed, test:started, test:completed", message)

    def test_capture_control_plane_learnings_records_blocked_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "等待结构化回执",
                    "question": "做一个系统",
                    "last_user_message": "做一个系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "blocked_reason": "missing_pipeline_receipt",
                }
            )
            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_EVOLUTION_PLANE": True}), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                learnings = guardian.capture_control_plane_learnings(
                    [{"task_id": "task-1", "action": "blocked", "blocked_reason": "missing_pipeline_receipt", "control_state": "blocked_unverified"}]
                )
            self.assertEqual(learnings[0]["category"], "control_plane")
            self.assertEqual(store.summarize_learnings()["pending"], 1)

    def test_run_reflection_cycle_promotes_repeated_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_learning(
                learning_key="lk-1",
                env_id="primary",
                task_id="task-1",
                category="control_plane",
                title="缺少回执",
                detail="missing ack",
                evidence={"task_id": "task-1"},
            )
            store.upsert_learning(
                learning_key="lk-1",
                env_id="primary",
                task_id="task-1",
                category="control_plane",
                title="缺少回执",
                detail="missing ack",
                evidence={"task_id": "task-1"},
            )
            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "ENABLE_EVOLUTION_PLANE": True,
                        "LEARNING_PROMOTION_THRESHOLD": 2,
                        "REFLECTION_INTERVAL_SECONDS": 3600,
                    },
                ), \
                mock.patch.object(guardian.time, "time", return_value=100):
                summary = guardian.run_reflection_cycle(force=True)
            self.assertEqual(summary["promoted"], 1)
            learning = store.get_learning("lk-1")
            self.assertEqual(learning["status"], "promoted")
            self.assertEqual(learning["promoted_target"], "contract")

    def test_current_task_facts_exports_user_visible_progress_for_a_share_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            home = base / ".openclaw"
            home.mkdir()
            (home / "openclaw.json").write_text("{}", encoding="utf-8")
            store.upsert_task(
                {
                    "task_id": "task-a-share",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "planning:completed",
                    "question": "实现A股闭环采样策略",
                    "last_user_message": "实现A股闭环采样策略",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "latest_receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm", "evidence": "plan"},
                }
            )
            store.upsert_task_contract(
                "task-a-share",
                {
                    "id": "a_share_delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                    "user_progress_rules": {"planning_only": "A股闭环方案已完成，但开发尚未启动。"},
                },
            )
            store.record_task_event("task-a-share", "dispatch_started", {"question": "实现A股闭环采样策略"})
            store.record_task_event("task-a-share", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm", "evidence": "plan"}})
            with mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {**guardian.DEFAULT_CONFIG, "ENABLE_BOOTSTRAP_INIT": True, "BOOTSTRAP_WRITE_MISSING": False}), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary", "home": home}), \
                mock.patch.object(guardian, "all_env_specs", return_value={"primary": {"id": "primary", "port": 18789}, "official": {"id": "official", "port": 19021}}), \
                mock.patch.object(guardian, "get_system_metrics", return_value={"cpu": 1.0, "mem_used": 1.0, "mem_total": 8.0}), \
                mock.patch.object(guardian, "check_process_running", return_value=True), \
                mock.patch.object(guardian, "check_gateway_health", return_value=True):
                guardian.write_task_registry_snapshot()
            facts = json.loads((base / "data" / "current-task-facts.json").read_text(encoding="utf-8"))
            self.assertEqual(facts["current_task"]["control_state"], "planning_only")
            self.assertEqual(facts["current_task"]["user_visible_progress"], "A股闭环方案已完成，但开发尚未启动。")
            self.assertEqual(
                [item["agent"] for item in facts["current_task"]["phase_statuses"]],
                ["pm", "dev", "test"],
            )
            self.assertEqual(facts["current_task"]["phase_statuses"][0]["state"], "completed")
            self.assertEqual(facts["current_task"]["phase_statuses"][1]["state"], "pending")
            self.assertEqual(facts["current_task"]["phase_statuses"][2]["state"], "pending")

    def test_write_task_registry_snapshot_exports_shared_state_and_memory_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            home = base / ".openclaw"
            home.mkdir()
            (home / "openclaw.json").write_text("{}", encoding="utf-8")
            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "DEV_IMPLEMENTING",
                    "question": "做一个系统",
                    "last_user_message": "做一个系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "started", "ack_id": "ack-1"},
                }
            )
            store.upsert_task_contract(
                "task-1",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-1", "dispatch_started", {"question": "做一个系统"})
            store.record_task_event("task-1", "pipeline_receipt", {"receipt": {"agent": "dev", "phase": "implementation", "action": "started", "ack_id": "ack-1"}})
            store.upsert_learning(
                learning_key="lk-export",
                env_id="primary",
                task_id="task-1",
                category="control_plane",
                title="缺少回执",
                detail="task missing ack",
                evidence={"task_id": "task-1"},
            )
            with mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {**guardian.DEFAULT_CONFIG, "ENABLE_BOOTSTRAP_INIT": True, "BOOTSTRAP_WRITE_MISSING": False}), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary", "home": home}), \
                mock.patch.object(guardian, "all_env_specs", return_value={"primary": {"id": "primary", "port": 18789}, "official": {"id": "official", "port": 19021}}), \
                mock.patch.object(guardian, "get_system_metrics", return_value={"cpu": 1.0, "mem_used": 1.0, "mem_total": 8.0}), \
                mock.patch.object(guardian, "check_process_running", return_value=True), \
                mock.patch.object(guardian, "check_gateway_health", return_value=True):
                guardian.write_task_registry_snapshot()
            self.assertTrue((base / "data" / "shared-state" / "runtime-health.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "learning-promotion-policy.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "bootstrap-status.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "watcher-summary.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "learning-runtime-status.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "reflection-freshness.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "memory-freshness.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "reuse-evidence-summary.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "self-check-runtime-status.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "self-check-events.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "main-closure-runtime-status.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "main-closure-events.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "restart-runtime-status.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "restart-events.json").exists())
            self.assertTrue((base / "data" / "shared-state" / "README.md").exists())
            self.assertTrue((base / ".learnings" / "ERRORS.md").exists())
            self.assertTrue((base / "MEMORY.md").exists())
            self.assertTrue((home / ".learnings" / "ERRORS.md").exists())
            self.assertTrue((home / "shared-context" / "monitor-tasks" / "tasks.jsonl").exists())

    def test_build_main_closure_supervision_summary_reads_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            closure_dir = home / "shared-context" / "main-closure"
            closure_dir.mkdir(parents=True)
            now = int(time.time())
            (closure_dir / "main-closure-runtime-status.json").write_text(
                json.dumps(
                    {
                        "env_id": "primary",
                        "foreground_root_task_id": "rt-1",
                        "active_root_count": 1,
                        "background_root_count": 1,
                        "adoption_pending_count": 1,
                        "finalization_pending_count": 1,
                        "delivery_failed_count": 1,
                        "late_result_count": 1,
                        "binding_source_counts": {"reply_to": 1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (closure_dir / "main-closure-events.json").write_text(
                json.dumps(
                    {"events": [{"event_type": "final_delivery_failed", "created_at": now - 5}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary", "home": home}):
                payload = guardian.build_main_closure_supervision_summary()
            self.assertEqual(payload["main_closure_artifact_status"], "ready")
            self.assertEqual(payload["foreground_root_task_id"], "rt-1")
            self.assertEqual(payload["delivery_failed_count"], 1)
            self.assertEqual(payload["recent_event_types"][0], "final_delivery_failed")

    def test_commit_active_binding_updates_runtime_binding_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = {
                **guardian.DEFAULT_CONFIG,
                "OPENCLAW_HOME": str(base / ".openclaw"),
                "OPENCLAW_CODE": str(base / "code-primary"),
                "OPENCLAW_OFFICIAL_STATE": str(base / ".openclaw-official"),
                "OPENCLAW_OFFICIAL_CODE": str(base / "code-official"),
                "GATEWAY_PORT": 18789,
                "OPENCLAW_OFFICIAL_PORT": 19021,
            }
            store = MonitorStateStore(base)
            with mock.patch.object(guardian, "BASE_DIR", base), \
                mock.patch.object(guardian, "CONFIG", cfg), \
                mock.patch.object(guardian, "STORE", store):
                guardian.commit_active_binding("official")
            binding = store.load_runtime_value("active_openclaw_env", {})
            audit = store.load_runtime_value("binding_audit_events", [])
            self.assertEqual(binding.get("env_id"), "official")
            self.assertEqual(binding.get("switch_state"), "committed")
            self.assertTrue(audit)
            self.assertEqual(audit[-1]["env_id"], "official")

    def test_sync_shared_context_watcher_tasks_imports_completed_and_dlq(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            monitor_dir = home / "shared-context" / "monitor-tasks"
            monitor_dir.mkdir(parents=True)
            (monitor_dir / "tasks.jsonl").write_text(
                json.dumps(
                    {
                        "request_id": "req-1",
                        "source_agent": "main",
                        "target_agent": "codex",
                        "intent": "THREADED_EXECUTION",
                        "current_state": "completed",
                        "completed_at": 100,
                        "delivered_at": 0,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (monitor_dir / "dlq.jsonl").write_text(
                json.dumps({"request_id": "req-2", "current_state": "failed"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            store = MonitorStateStore(base)
            with mock.patch.object(guardian, "STORE", store):
                summary = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
            self.assertEqual(summary["imported"], 2)
            self.assertEqual(summary["summary"]["completed"], 1)
            self.assertEqual(summary["summary"]["undelivered"], 1)
            self.assertEqual(summary["summary"]["dlq"], 1)

    def test_sync_shared_context_watcher_tasks_bridges_a_share_receipt_by_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            monitor_dir = home / "shared-context" / "monitor-tasks"
            monitor_dir.mkdir(parents=True)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-a-share",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "处理中",
                    "question": "实现A股闭环采样策略",
                    "last_user_message": "实现A股闭环采样策略",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-a-share",
                {
                    "id": "a_share_delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-a-share", "dispatch_started", {"question": "实现A股闭环采样策略"})
            (monitor_dir / "tasks.jsonl").write_text(
                json.dumps(
                    {
                        "request_id": "req-bridge-1",
                        "session_key": "session-a",
                        "payload": {
                            "receipt": {
                                "session_key": "session-a",
                                "agent": "pm",
                                "phase": "planning",
                                "action": "completed",
                                "evidence": "plan=ashare-prd",
                                "timestamp": "2026-03-12T09:00:00+08:00"
                            }
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(guardian, "STORE", store):
                summary = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
            task = store.get_task("task-a-share")
            control = store.derive_task_control_state("task-a-share")
            events = store.list_task_events("task-a-share", limit=10)
            self.assertEqual(summary["receipt_bridge"]["bridged"], 1)
            self.assertEqual(task["latest_receipt"]["agent"], "pm")
            self.assertEqual(task["blocked_reason"], "")
            self.assertEqual(control["control_state"], "planning_only")
            self.assertTrue(any(item["event_type"] == "pipeline_receipt" for item in events))

    def test_sync_shared_context_watcher_tasks_dedupes_repeated_bridge_and_reaches_completed_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            monitor_dir = home / "shared-context" / "monitor-tasks"
            monitor_dir.mkdir(parents=True)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-a-share",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "implementation:completed",
                    "question": "实现A股闭环采样策略",
                    "last_user_message": "实现A股闭环采样策略",
                    "started_at": 1,
                    "last_progress_at": 5,
                    "created_at": 1,
                    "updated_at": 5,
                    "completed_at": 5,
                    "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "completed", "ack_id": "ack-dev", "evidence": "files=engine.py"},
                }
            )
            store.upsert_task_contract(
                "task-a-share",
                {
                    "id": "a_share_delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-a-share", "dispatch_started", {"question": "实现A股闭环采样策略"})
            store.record_task_event("task-a-share", "dispatch_complete", {"status": "completed"})
            store.record_task_event("task-a-share", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "started", "ack_id": "ack-pm-start", "evidence": "read=req"}})
            store.record_task_event("task-a-share", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm-done", "evidence": "plan=ready"}})
            store.record_task_event("task-a-share", "pipeline_receipt", {"receipt": {"agent": "dev", "phase": "implementation", "action": "started", "ack_id": "ack-dev-start", "evidence": "files=spec.py"}})
            store.record_task_event("task-a-share", "pipeline_receipt", {"receipt": {"agent": "dev", "phase": "implementation", "action": "completed", "ack_id": "ack-dev", "evidence": "files=engine.py"}})
            watcher_payload = {
                "request_id": "req-bridge-2",
                "task_id": "task-a-share",
                "payload": {
                    "receipt": {
                        "task_id": "task-a-share",
                        "agent": "test",
                        "phase": "testing",
                        "action": "completed",
                        "evidence": "tests=pytest 12/12",
                        "timestamp": "2026-03-12T09:05:00+08:00"
                    }
                }
            }
            serialized = json.dumps(watcher_payload, ensure_ascii=False) + "\n"
            (monitor_dir / "tasks.jsonl").write_text(serialized, encoding="utf-8")
            (monitor_dir / "dlq.jsonl").write_text(serialized, encoding="utf-8")
            with mock.patch.object(guardian, "STORE", store):
                summary_first = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
                summary_second = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
            events = [item for item in store.list_task_events("task-a-share", limit=20) if item["event_type"] == "pipeline_receipt"]
            control = store.derive_task_control_state("task-a-share")
            task = store.get_task("task-a-share")
            test_receipts = [item for item in events if (item["payload"].get("receipt") or {}).get("agent") == "test"]
            self.assertEqual(summary_first["receipt_bridge"]["bridged"], 1)
            self.assertEqual(summary_second["receipt_bridge"]["bridged"], 0)
            self.assertEqual(len(test_receipts), 1)
            self.assertEqual(task["latest_receipt"]["agent"], "test")
            self.assertEqual(control["control_state"], "completed_verified")

    def test_sync_shared_context_watcher_tasks_bridges_dev_started_by_question_when_single_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            monitor_dir = home / "shared-context" / "monitor-tasks"
            monitor_dir.mkdir(parents=True)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-a-share",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "处理中",
                    "question": "实现A股闭环采样策略",
                    "last_user_message": "请继续实现A股闭环采样策略",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-a-share",
                {
                    "id": "a_share_delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-a-share", "dispatch_started", {"question": "实现A股闭环采样策略"})
            (monitor_dir / "tasks.jsonl").write_text(
                json.dumps(
                    {
                        "request_id": "req-bridge-direct-dev",
                        "payload": {
                            "question": "请继续实现A股闭环采样策略",
                            "agent": "dev",
                            "phase": "implementation",
                            "action": "started",
                            "evidence": "opened=guardian.py",
                            "timestamp": "2026-03-12T09:02:00+08:00",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(guardian, "STORE", store):
                summary = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
            task = store.get_task("task-a-share")
            control = store.derive_task_control_state("task-a-share")
            self.assertEqual(summary["receipt_bridge"]["bridged"], 1)
            self.assertEqual(task["latest_receipt"]["agent"], "dev")
            self.assertEqual(control["control_state"], "dev_running")

    def test_sync_shared_context_watcher_tasks_ignores_question_hint_when_multiple_active_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            monitor_dir = home / "shared-context" / "monitor-tasks"
            monitor_dir.mkdir(parents=True)
            store = MonitorStateStore(base)
            for task_id, question in (("task-a", "实现A股闭环采样策略"), ("task-b", "做一个记事本系统")):
                store.upsert_task(
                    {
                        "task_id": task_id,
                        "session_key": f"session-{task_id}",
                        "env_id": "primary",
                        "channel": "feishu_dm",
                        "status": "blocked",
                        "current_stage": "处理中",
                        "question": question,
                        "last_user_message": question,
                        "blocked_reason": "missing_pipeline_receipt",
                        "started_at": 1,
                        "last_progress_at": 2,
                        "created_at": 1,
                        "updated_at": 2,
                    }
                )
                store.record_task_event(task_id, "dispatch_started", {"question": question})
            (monitor_dir / "tasks.jsonl").write_text(
                json.dumps(
                    {
                        "request_id": "req-bridge-ambiguous",
                        "payload": {
                            "question": "实现A股闭环采样策略",
                            "agent": "dev",
                            "phase": "implementation",
                            "action": "started",
                            "evidence": "opened=guardian.py",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(guardian, "STORE", store):
                summary = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
            self.assertEqual(summary["receipt_bridge"]["observed_unbound"], 1)
            self.assertEqual([item for item in store.list_task_events("task-a", limit=10) if item["event_type"] == "pipeline_receipt"], [])
            self.assertEqual([item for item in store.list_task_events("task-b", limit=10) if item["event_type"] == "pipeline_receipt"], [])

    def test_sync_shared_context_watcher_tasks_does_not_mask_denied_as_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            monitor_dir = home / "shared-context" / "monitor-tasks"
            monitor_dir.mkdir(parents=True)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-a-share",
                    "session_key": "session-a",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "处理中",
                    "question": "实现A股闭环采样策略",
                    "last_user_message": "实现A股闭环采样策略",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
            )
            store.upsert_task_contract(
                "task-a-share",
                {
                    "id": "a_share_delivery_pipeline",
                    "protocol_version": "hm.v1",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            store.record_task_event("task-a-share", "dispatch_started", {"question": "实现A股闭环采样策略"})
            (monitor_dir / "tasks.jsonl").write_text(
                json.dumps(
                    {
                        "request_id": "req-denied",
                        "session_key": "session-a",
                        "current_state": "failed",
                        "error": "agent-to-agent messaging denied / forbidden",
                        "payload": {"message": "agent-to-agent messaging denied / forbidden"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(guardian, "STORE", store):
                summary = guardian.sync_shared_context_watcher_tasks({"id": "primary", "home": home})
            control = store.derive_task_control_state("task-a-share")
            events = [item for item in store.list_task_events("task-a-share", limit=10) if item["event_type"] == "pipeline_receipt"]
            self.assertEqual(summary["receipt_bridge"]["ignored"], 1)
            self.assertEqual(control["control_state"], "blocked_unverified")
            self.assertEqual(events, [])

    def test_enforce_single_active_runtime_guard_records_dual_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "active_env_id", return_value="primary"), \
                mock.patch.object(guardian, "all_env_specs", return_value={"primary": {"id": "primary", "port": 18789}, "official": {"id": "official", "port": 19021}}), \
                mock.patch.object(guardian, "get_listener_pid", side_effect=[1111, 2222]), \
                mock.patch.object(guardian, "run_args") as run_args, \
                mock.patch.object(guardian, "record_change_log") as record_change_log, \
                mock.patch.object(guardian, "notify"):
                issues = guardian.enforce_single_active_runtime_guard()
            self.assertEqual(issues[0]["code"], "dual_listener")
            record_change_log.assert_called_once()
            self.assertEqual(run_args.call_args.args[0], [str(guardian.OFFICIAL_MANAGER), "stop"])

    def test_enforce_task_registry_control_plane_marks_pipeline_recovery_as_ops_attention_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_task(
                {
                    "task_id": "task-detached",
                    "session_key": "agent:main:feishu:direct:ou_test",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "blocked",
                    "current_stage": "implementation:started",
                    "question": "开发一个记事本系统",
                    "last_user_message": "开发一个记事本系统",
                    "blocked_reason": "missing_pipeline_receipt",
                    "started_at": 1,
                    "last_progress_at": 10,
                    "created_at": 1,
                    "updated_at": 10,
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
            store.record_task_event(
                "task-detached",
                "pipeline_receipt",
                {"receipt": {"agent": "pm", "phase": "planning", "action": "completed", "ack_id": "ack-pm"}},
            )
            store.record_task_event("task-detached", "stage_progress", {"marker": "implementation:started", "stage": "implementation:started"})

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(
                    guardian,
                    "CONFIG",
                    {
                        "ENABLE_TASK_REGISTRY": True,
                        "ENABLE_INTRUSIVE_TASK_CONTROL": False,
                        "TASK_REGISTRY_RETENTION": 20,
                        "TASK_CONTROL_RECEIPT_GRACE": 5,
                        "TASK_CONTROL_FOLLOWUP_COOLDOWN": 0,
                        "TASK_CONTROL_MAX_ATTEMPTS": 2,
                        "TASK_CONTROL_BLOCK_TIMEOUT": 900,
                    },
                ), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}), \
                mock.patch.object(guardian.time, "time", return_value=100), \
                mock.patch.object(guardian, "send_guardian_followup", return_value=(True, "")), \
                mock.patch.object(guardian, "record_change_log"), \
                mock.patch.object(guardian, "write_task_registry_snapshot"), \
                mock.patch.object(guardian, "capture_control_plane_learnings"):
                outcomes = guardian.enforce_task_registry_control_plane()

            task = store.get_task("task-detached")
            events = store.list_task_events("task-detached", limit=20)
            action = store.get_open_control_action("task-detached")
            self.assertEqual(task["status"], "blocked")
            self.assertEqual(task["blocked_reason"], "missing_pipeline_receipt")
            self.assertFalse(any(item["event_type"] == "recovery_started" for item in events))
            self.assertFalse(any(item["event_type"] == "recovery_succeeded" for item in events))
            self.assertTrue(any(item["event_type"] == "ops_attention_needed" for item in events))
            self.assertEqual(action["status"], "pending")
            self.assertEqual(action["details"]["policy"], "observe_only")
            self.assertEqual(outcomes[0]["action"], "ops_attention_needed")


if __name__ == "__main__":
    unittest.main()
