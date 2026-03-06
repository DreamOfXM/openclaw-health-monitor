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


if __name__ == "__main__":
    unittest.main()
