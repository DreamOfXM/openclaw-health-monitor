import tempfile
import time
import unittest
from pathlib import Path

from guardian import capture_control_plane_learnings
from recovery_watchdog import detect_recurrence_problem_code
from self_evolution import (
    adopt_rule,
    close_learning,
    generate_daily_evolution_report,
    mark_recurrence,
    propose_rule,
    record_learning,
    verify_learning,
    write_state_snapshot,
)
from state_store import MonitorStateStore


class SelfEvolutionTests(unittest.TestCase):
    def test_learning_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            learning = record_learning(
                store,
                problem_code="missing_pipeline_receipt",
                title="缺少结构化回执",
                summary="任务因缺少结构化回执而阻塞",
                evidence={"task_id": "task-1"},
                root_task_id="task-1",
            )
            self.assertEqual(learning["current_state"], "recorded")

            learning = propose_rule(
                store,
                learning_key=learning["learning_key"],
                rule_target="EXECUTION_PROTOCOL.md",
                rule_content="没有结构化回执不得宣布完成",
            )
            self.assertEqual(learning["current_state"], "candidate_rule")

            learning = adopt_rule(
                store,
                learning_key=learning["learning_key"],
                rule_target="EXECUTION_PROTOCOL.md",
            )
            self.assertEqual(learning["current_state"], "adopted")

            learning = verify_learning(
                store,
                learning_key=learning["learning_key"],
                scenario="真实任务 task-2 再次跑通",
                evidence={"task_id": "task-2"},
            )
            self.assertEqual(learning["current_state"], "verified")

            learning = close_learning(store, learning_key=learning["learning_key"])
            self.assertEqual(learning["current_state"], "closed")

    def test_reopened_and_recurrence_are_separated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            learning = record_learning(
                store,
                problem_code="wrong_task_binding",
                title="串任务",
                summary="短句追问被错误绑到新任务",
            )
            key = learning["learning_key"]
            verify_learning(store, learning_key=key, scenario="首次修复后验证")

            reopened = record_learning(
                store,
                learning_key=key,
                problem_code="wrong_task_binding",
                title="串任务",
                summary="短句追问再次误绑",
                evidence={"task_id": "task-9"},
            )
            self.assertEqual(reopened["current_state"], "reopened")
            self.assertEqual(int(reopened["recurrence_count"] or 0), 0)

            recurrent = mark_recurrence(store, learning_key=key, evidence={"task_id": "task-10"})
            self.assertEqual(recurrent["current_state"], "reopened")
            self.assertEqual(int(recurrent["recurrence_count"] or 0), 1)

    def test_daily_report_and_state_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            learning = record_learning(
                store,
                problem_code="no_reply_after_commit",
                title="承诺后未回复",
                summary="承诺继续后未给用户反馈",
            )
            propose_rule(
                store,
                learning_key=learning["learning_key"],
                rule_target="AGENTS.md",
                rule_content="承诺后必须给出结果或失败说明",
            )
            adopt_rule(store, learning_key=learning["learning_key"], rule_target="AGENTS.md")
            verify_learning(store, learning_key=learning["learning_key"], scenario="task-2")
            close_learning(store, learning_key=learning["learning_key"])
            reopened = record_learning(
                store,
                problem_code="no_reply_after_commit",
                title="承诺后未回复",
                summary="再次出现承诺后未回复",
                learning_key=learning["learning_key"],
            )
            mark_recurrence(store, learning_key=reopened["learning_key"], evidence={"task_id": "task-3"})

            report = generate_daily_evolution_report(store, now=int(time.time()))
            self.assertEqual(report["issues_found"], 1)
            self.assertEqual(report["issues_reopened"], 1)
            self.assertEqual(report["recurrence_events"], 1)
            self.assertEqual(report["rules_added"], 1)
            self.assertEqual(report["candidate_rules"], 1)
            self.assertEqual(report["pending_verification"], 1)

            snapshot = write_state_snapshot(base, store)
            self.assertIn(learning["learning_key"], snapshot["pending_learnings"])
            self.assertIn(learning["learning_key"], snapshot["lifecycle_view"]["reopened"])
            self.assertTrue(snapshot["lifecycle_view"]["recurrence"])
            self.assertTrue((base / "self-evolution" / "state.json").exists())

    def test_unknown_problem_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            with self.assertRaises(ValueError):
                record_learning(
                    store,
                    problem_code="not_a_real_problem_code",
                    title="未知",
                    summary="不应该被吞掉",
                )

    def test_watchdog_taxonomy_is_strict(self):
        self.assertEqual(
            detect_recurrence_problem_code({"anomaly_type": "heartbeat_missing_hard"}),
            "heartbeat_missing_hard",
        )
        self.assertEqual(
            detect_recurrence_problem_code({"anomaly_type": "received_only_requires_main_followup"}),
            "received_only_requires_main_followup",
        )
        self.assertEqual(
            detect_recurrence_problem_code({"anomaly_type": "something-else"}),
            "task_closure_missing",
        )

    def test_guardian_records_machine_readable_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
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
            import guardian
            from unittest import mock

            with mock.patch.object(guardian, "STORE", store), \
                mock.patch.object(guardian, "CONFIG", {"ENABLE_EVOLUTION_PLANE": True}), \
                mock.patch.object(guardian, "current_env_spec", return_value={"id": "primary"}):
                learnings = capture_control_plane_learnings(
                    [{"task_id": "task-1", "action": "blocked", "blocked_reason": "missing_pipeline_receipt", "control_state": "blocked_unverified"}]
                )
            self.assertEqual(learnings[0]["problem_code"], "missing_pipeline_receipt")
            self.assertTrue(str(learnings[0]["title"]).startswith("control_observation:"))
            self.assertIn("control_state=blocked_unverified", str(learnings[0]["summary"]))


if __name__ == "__main__":
    unittest.main()
