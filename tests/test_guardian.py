import tempfile
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

    def test_push_runtime_progress_updates_prefers_guardian_followup(self):
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
            self.assertEqual(tasks[0]["status"], "blocked")
            self.assertEqual(tasks[0]["latest_receipt"]["agent"], "dev")
            self.assertEqual(tasks[1]["status"], "completed")
            self.assertEqual(tasks[1]["current_stage"], "已完成")
            events = store.list_task_events(tasks[0]["task_id"], limit=10)
            self.assertTrue(any(item["event_type"] == "pipeline_receipt" for item in events))
            completed_events = store.list_task_events(tasks[1]["task_id"], limit=10)
            self.assertTrue(any(item["event_type"] == "dispatch_complete" for item in completed_events))
            summary_file = base / "data" / "task-registry-summary.json"
            self.assertTrue(summary_file.exists())

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

    def test_push_runtime_progress_updates_only_when_idle(self):
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


if __name__ == "__main__":
    unittest.main()
