import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


BASE_DIR = Path("/Users/hangzhou/openclaw-health-monitor")


def _load_module(name: str, relative_path: str):
    path = BASE_DIR / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestDailyReflectionScripts(unittest.TestCase):
    def test_daily_reflection_detects_blocked_not_delivered(self):
        mod = _load_module("daily_reflection_mod", "scripts/daily_reflection.py")
        with tempfile.TemporaryDirectory() as tmp:
            facts_path = Path(tmp) / "current-task-facts.json"
            facts_path.write_text(
                json.dumps(
                    {
                        "current_task": {
                            "task_id": "task-1",
                            "question": "voice assistant fix",
                            "control_state": "blocked_unverified",
                            "evidence_level": "weak",
                            "next_action": "require_receipt_or_block",
                            "delivery_state": "",
                            "control": {
                                "flags": {"visible_completion": False},
                                "core_supervision": {
                                    "is_blocked": True,
                                    "delivery_confirmed": False,
                                },
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original = mod.FACTS_FILE
            mod.FACTS_FILE = facts_path
            try:
                issues = mod.check_undelivered_tasks()
            finally:
                mod.FACTS_FILE = original
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["reason"], "blocked_not_delivered")

    def test_daily_report_counts_blocked_undelivered(self):
        mod = _load_module("daily_report_mod", "scripts/daily_report.py")
        with tempfile.TemporaryDirectory() as tmp:
            facts_path = Path(tmp) / "current-task-facts.json"
            facts_path.write_text(
                json.dumps(
                    {
                        "current_task": {
                            "task_id": "task-2",
                            "control_state": "blocked_control_followup_failed",
                            "control": {
                                "core_supervision": {
                                    "delivery_confirmed": False,
                                }
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original = mod.FACTS_FILE
            mod.FACTS_FILE = facts_path
            try:
                stats = mod.get_system_stats()
            finally:
                mod.FACTS_FILE = original
            self.assertEqual(stats["tasks_blocked_undelivered"], 1)


if __name__ == "__main__":
    unittest.main()
