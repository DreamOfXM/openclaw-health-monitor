import unittest
from unittest import mock

import dashboard


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


if __name__ == "__main__":
    unittest.main()
