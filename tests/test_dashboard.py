import unittest
from unittest import mock
from pathlib import Path
import tempfile

import dashboard
from state_store import MonitorStateStore


class DashboardMemoryTests(unittest.TestCase):
    def test_parse_mem_value_to_gb(self):
        self.assertEqual(dashboard.parse_mem_value_to_gb("1024M"), 1.0)
        self.assertEqual(dashboard.parse_mem_value_to_gb("2G"), 2.0)
        self.assertEqual(dashboard.parse_mem_value_to_gb("512K"), 0.0)

    def test_summarize_memory_usage_splits_process_and_system(self):
        metrics = {
            "mem_used": 32.0,
            "mem_total": 32.0,
            "mem_wired": 2.5,
            "mem_compressed": 1.5,
        }
        top_processes = [
            {"mem_mb": 1536},
            {"mem_mb": 1024},
            {"mem_mb": 512},
        ]

        summary = dashboard.summarize_memory_usage(metrics, top_processes)

        self.assertEqual(summary["top15_gb"], 3.0)
        self.assertEqual(summary["unattributed_gb"], 29.0)
        self.assertEqual(summary["process_coverage_percent"], 9.4)
        self.assertEqual(summary["items"][0]["name"], "Top 15 进程")
        self.assertEqual(summary["items"][1]["name"], "Kernel / Wired")
        self.assertEqual(summary["items"][2]["name"], "Compressed")

    @mock.patch("dashboard.get_process_info")
    @mock.patch("dashboard.get_process_info_by_pid")
    @mock.patch("dashboard.load_pid_file")
    def test_get_guardian_process_info_prefers_pid_file(self, load_pid_file, by_pid, by_name):
        load_pid_file.return_value = 4321
        by_pid.return_value = {"pid": 4321, "cpu": 0.1, "mem": 0.2, "cmd": "/usr/bin/python guardian.py"}

        info = dashboard.get_guardian_process_info()

        self.assertEqual(info["pid"], 4321)
        by_name.assert_not_called()

    @mock.patch("dashboard.get_process_info")
    @mock.patch("dashboard.get_process_info_by_pid")
    @mock.patch("dashboard.load_pid_file")
    def test_get_guardian_process_info_falls_back_to_name_match(self, load_pid_file, by_pid, by_name):
        load_pid_file.return_value = None
        by_pid.return_value = None
        by_name.return_value = {"pid": 5678, "cpu": 0.1, "mem": 0.2, "cmd": "python guardian.py"}

        info = dashboard.get_guardian_process_info()

        self.assertEqual(info["pid"], 5678)
        by_name.assert_called_once_with(r"[g]uardian\.py")

    def test_active_env_id_defaults_to_primary(self):
        self.assertEqual(dashboard.active_env_id({}), "primary")
        self.assertEqual(dashboard.active_env_id({"ACTIVE_OPENCLAW_ENV": "official"}), "official")
        self.assertEqual(dashboard.active_env_id({"ACTIVE_OPENCLAW_ENV": "weird"}), "primary")

    @mock.patch("dashboard.check_gateway_health_for_env")
    @mock.patch("dashboard.read_git_head")
    @mock.patch("dashboard.get_listener_pid")
    def test_list_openclaw_environments_marks_active_environment(self, listener_pid, read_git_head, health):
        listener_pid.side_effect = [1111, None]
        read_git_head.side_effect = ["abc123", "def456"]
        health.side_effect = [True]
        config = {
            "ACTIVE_OPENCLAW_ENV": "primary",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
        }

        envs = dashboard.list_openclaw_environments(config)

        self.assertEqual(len(envs), 2)
        self.assertEqual(envs[0]["id"], "primary")
        self.assertTrue(envs[0]["active"])
        self.assertTrue(envs[0]["running"])
        self.assertTrue(envs[0]["healthy"])
        self.assertEqual(envs[1]["id"], "official")
        self.assertFalse(envs[1]["active"])
        self.assertFalse(envs[1]["running"])

    def test_get_task_registry_payload_includes_summary_and_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(Path(base))
            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "agent:main:feishu:direct:ou_test",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "running",
                    "current_stage": "DEV_IMPLEMENTING",
                    "question": "帮我做一个系统",
                    "last_user_message": "帮我做一个系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "started"},
                }
            )
            store.record_task_event("task-1", "dispatch_started", {"question": "帮我做一个系统"})
            store.record_task_event("task-1", "stage_progress", {"marker": "DEV_IMPLEMENTING"})

            with mock.patch.object(dashboard, "STORE", store), \
                mock.patch.object(dashboard, "load_config", return_value={"ENABLE_TASK_REGISTRY": True}), \
                mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary"}):
                payload = dashboard.get_task_registry_payload(limit=5)

            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["summary"]["running"], 1)
            self.assertEqual(payload["current"]["task_id"], "task-1")
            self.assertEqual(payload["current"]["receipt_summary"]["agent"], "dev")
            self.assertEqual(payload["current"]["control"]["control_state"], "dev_running")
            self.assertEqual(len(payload["current"]["timeline"]), 2)


if __name__ == "__main__":
    unittest.main()
