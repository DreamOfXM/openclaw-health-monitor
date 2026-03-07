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
                    "send_feishu_progress_push",
                    side_effect=lambda open_id, message: pushes.append((open_id, message)) or True,
                ), \
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
            self.assertEqual(pushes[0][0], "ou_test")
            self.assertIn("没有新的可见进展", pushes[0][1])
            self.assertEqual(change_logs[0][2]["idle"], 220)

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
                    "send_feishu_progress_push",
                    side_effect=lambda open_id, message: pushes.append((open_id, message)) or True,
                ), \
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
            self.assertIn("当前阶段", pushes[-1][1] or "")
            self.assertIn("220 秒", pushes[-1][1] or "")


if __name__ == "__main__":
    unittest.main()
