import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from recovery_watchdog import RecoveryWatchdog
from state_store import MonitorStateStore


class RecoveryWatchdogTests(unittest.TestCase):
    def _write_inputs(self, base: Path, payload: dict) -> None:
        data_dir = base / "data"
        shared_dir = data_dir / "shared-state"
        shared_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "current-task-facts.json").write_text(json.dumps(payload), encoding="utf-8")
        (shared_dir / "task-registry-snapshot.json").write_text(json.dumps({"tasks": [], "control_queue": []}), encoding="utf-8")

    def test_detects_correction_and_dispatches_internal_hint(self):
        # Phase 2 简化后：watchdog 只检测"完成/阻塞但未送达"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {
                        "task_id": "task-completed",
                        "session_key": "agent:main:feishu:direct:user",
                        "control_state": "completed_verified",
                        "updated_at": now,
                    },
                    "current_root_task": {"root_task_id": "rt-completed", "current_workflow_run_id": "wf-completed", "workflow_state": "completed", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-completed", "current_state": "completed", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            dispatched: list[tuple[str, str]] = []
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": False,
                },
                dispatcher=lambda code_root, session_key, message: dispatched.append((session_key, message)) or {"ok": True},
            )

            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertEqual(result["dispatched_count"], 1)
            self.assertEqual(dispatched[0][0], "agent:main:main")
            self.assertIn("WATCHDOG_RECOVERY_HINT", dispatched[0][1])

    def test_cooldown_prevents_repeat_dispatch(self):
        # Phase 2 简化后：watchdog 只检测"完成/阻塞但未送达"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {"task_id": "task-completed", "session_key": "agent:main:main", "control_state": "completed_verified", "updated_at": now},
                    "current_root_task": {"root_task_id": "rt-completed", "current_workflow_run_id": "wf-completed", "workflow_state": "completed", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-completed", "current_state": "completed", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            calls = {"count": 0}
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": False,
                    "RECOVERY_WATCHDOG_COOLDOWN_SECONDS": 3600,
                },
                dispatcher=lambda code_root, session_key, message: calls.__setitem__("count", calls["count"] + 1) or {"ok": True},
            )

            first = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})
            second = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertEqual(first["dispatched_count"], 1)
            self.assertEqual(second["cooldown_skips"], 1)
            self.assertEqual(calls["count"], 1)

    def test_ollama_classifier_can_suppress_dispatch(self):
        # Phase 2 简化后：watchdog 只检测"完成/阻塞但未送达"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {"task_id": "task-completed", "session_key": "agent:main:main", "control_state": "completed_verified", "updated_at": now},
                    "current_root_task": {"root_task_id": "rt-completed", "current_workflow_run_id": "wf-completed", "workflow_state": "completed", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-completed", "current_state": "completed", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": True,
                },
                dispatcher=lambda code_root, session_key, message: {"ok": True},
                ollama_classifier=lambda candidate, context: {
                    "should_dispatch": False,
                    "target_agent": "main",
                    "severity": "low",
                    "reason": "normal_waiting",
                    "hint_title": "ignore",
                    "hint_message": "normal waiting",
                },
            )

            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertEqual(result["dispatched_count"], 0)
            self.assertEqual(result["items"][0]["should_dispatch"], False)

    def test_terminal_task_does_not_trigger_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {"task_id": "task-done", "session_key": "agent:main:main", "control_state": "completed_verified", "updated_at": now},
                    "current_root_task": {"root_task_id": "rt-done", "current_workflow_run_id": "wf-done", "workflow_state": "delivered", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-done", "current_state": "delivered", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": "confirmed"},
                },
            )
            store = MonitorStateStore(base)
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={"ENABLE_RECOVERY_WATCHDOG": True, "RECOVERY_WATCHDOG_USE_OLLAMA": False, "RECOVERY_WATCHDOG_FOLLOWUP_SECONDS": 60},
                dispatcher=lambda code_root, session_key, message: {"ok": True},
            )

            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertEqual(result["candidate_count"], 0)
            self.assertEqual(result["dispatched_count"], 0)

    def test_delivery_attempt_current_state_alias_is_terminal(self):
        self.assertTrue(
            RecoveryWatchdog._is_terminal(
                {"control_state": "completed_verified"},
                {"workflow_state": "delivery_pending"},
                {"current_state": "delivery_pending"},
                {"current_state": "delivery_confirmed"},
            )
        )

    def test_delivery_pending_with_followup_stays_non_terminal(self):
        self.assertFalse(
            RecoveryWatchdog._is_terminal(
                {
                    "control_state": "completed_verified",
                    "next_action": "await_delivery_confirmation",
                    "core_truth": {"needs_followup": True},
                },
                {"workflow_state": "delivery_pending"},
                {"current_state": "delivery_pending"},
                {},
            )
        )

    def test_blocked_task_still_triggers_user_visible_followup_hint(self):
        # Phase 2 简化后：watchdog 只检测"完成/阻塞但未送达"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {
                        "task_id": "task-blocked",
                        "session_key": "agent:main:main",
                        "control_state": "blocked_unverified",
                        "updated_at": now,
                    },
                    "current_root_task": {"root_task_id": "rt-blocked", "current_workflow_run_id": "wf-blocked", "workflow_state": "accepted", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-blocked", "current_state": "accepted", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            dispatched: list[str] = []
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": False,
                },
                dispatcher=lambda code_root, session_key, message: dispatched.append(message) or {"ok": True},
            )

            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertGreaterEqual(result["dispatched_count"], 1)
            self.assertTrue(any(item["anomaly_type"] == "blocked_not_delivered" for item in result["items"]))
            self.assertTrue(any("blocked but not delivered" in message for message in dispatched))

    def test_followup_pending_without_main_recovery_dispatches(self):
        # Phase 2 简化后：watchdog 只检测"完成/阻塞但未送达"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {
                        "task_id": "task-blocked",
                        "session_key": "agent:main:main",
                        "control_state": "blocked_unverified",
                        "updated_at": now,
                    },
                    "current_root_task": {"root_task_id": "rt-blocked", "current_workflow_run_id": "wf-blocked", "workflow_state": "accepted", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-blocked", "current_state": "accepted", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            dispatched: list[str] = []
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": False,
                },
                dispatcher=lambda code_root, session_key, message: dispatched.append(message) or {"ok": True},
            )

            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertTrue(any(item["anomaly_type"] == "blocked_not_delivered" for item in result["items"]))
            self.assertEqual(result["dispatched_count"], 1)
            self.assertTrue(any("blocked but not delivered" in message for message in dispatched))

    def test_watchdog_exhausts_after_three_attempts(self):
        # Phase 2 简化后：watchdog 只检测"完成/阻塞但未送达"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {"task_id": "task-completed", "session_key": "agent:main:main", "control_state": "completed_verified", "updated_at": now},
                    "current_root_task": {"root_task_id": "rt-completed", "current_workflow_run_id": "wf-completed", "workflow_state": "completed", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-completed", "current_state": "completed", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            dispatches = {"count": 0}
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": True,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": False,
                    "RECOVERY_WATCHDOG_COOLDOWN_SECONDS": 0,
                    "RECOVERY_WATCHDOG_MAX_ATTEMPTS": 3,
                },
                dispatcher=lambda code_root, session_key, message: dispatches.__setitem__("count", dispatches["count"] + 1) or {"ok": True},
            )

            watchdog.run({"id": "primary", "code": str(base), "home": str(base)})
            watchdog.run({"id": "primary", "code": str(base), "home": str(base)})
            watchdog.run({"id": "primary", "code": str(base), "home": str(base)})
            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertEqual(dispatches["count"], 3)
            self.assertEqual(result["items"][0]["dispatch"]["status"], "watchdog_exhausted")

    def test_missing_heartbeat_escalates_to_hard_candidate(self):
        # Phase 2 简化后：watchdog 不再检测心跳，只检测"完成/阻塞但未送达"
        # 此测试改为测试 completed_not_delivered 场景
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = int(time.time()) - 1000
            self._write_inputs(
                base,
                {
                    "current_task": {
                        "task_id": "task-completed-undelivered",
                        "session_key": "agent:main:main",
                        "control_state": "completed_verified",
                        "updated_at": now,
                    },
                    "current_root_task": {"root_task_id": "rt-completed", "current_workflow_run_id": "wf-completed", "workflow_state": "completed", "updated_at": now},
                    "current_workflow_run": {"workflow_run_id": "wf-completed", "current_state": "completed", "updated_at": now},
                    "current_delivery_attempt": {"delivery_state": ""},
                },
            )
            store = MonitorStateStore(base)
            watchdog = RecoveryWatchdog(
                base_dir=base,
                store=store,
                config={
                    "ENABLE_RECOVERY_WATCHDOG": True,
                    "ENABLE_RECOVERY_WATCHDOG_DISPATCH": False,
                    "RECOVERY_WATCHDOG_USE_OLLAMA": False,
                },
            )

            result = watchdog.run({"id": "primary", "code": str(base), "home": str(base)})

            self.assertTrue(any(item["anomaly_type"] == "completed_not_delivered" for item in result["items"]))
            self.assertTrue(any(item["severity"] == "high" for item in result["items"]))
            self.assertEqual(result["items"][0]["dispatch"]["status"], "dry_run")

    def test_dispatch_via_openclaw_uses_session_id_and_preserves_watchdog_hint_payload(self):
        sessions_payload = json.dumps(
            {
                "sessions": [
                    {"key": "agent:main:main", "sessionId": "session-main-123"},
                    {"key": "agent:pm:main", "sessionId": "session-pm-456"},
                ]
            }
        )
        calls: list[list[str]] = []

        def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            calls.append(cmd)
            if cmd[:3] == ["node", "openclaw.mjs", "sessions"]:
                return mock.Mock(returncode=0, stdout=sessions_payload, stderr="")
            if cmd[:3] == ["node", "openclaw.mjs", "agent"]:
                return mock.Mock(returncode=0, stdout='{"status":"accepted"}', stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("recovery_watchdog.subprocess.run", side_effect=fake_run):
            base = Path(tmp)
            message = RecoveryWatchdog._build_hint_message(
                {
                    "task_id": "task-1",
                    "root_task_id": "rt-1",
                    "workflow_run_id": "wf-1",
                    "session_key": "agent:main:feishu:direct:user",
                    "anomaly_type": "followup_pending_without_main_recovery",
                    "severity": "high",
                    "reason": "followup_pending_without_main_recovery",
                    "hint_title": "WATCHDOG_RECOVERY_HINT:followup_pending_without_main_recovery",
                    "hint_message": "main must not leave it pending without recovery",
                    "evidence": {"followup_stage": "hard", "next_action": "await_dev_receipt"},
                },
                "agent:main:main",
            )

            result = RecoveryWatchdog._dispatch_via_openclaw(base, "agent:main:main", message)

        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "session-main-123")
        self.assertEqual(calls[0][:6], ["node", "openclaw.mjs", "sessions", "--json", "--all-agents", "--active"])
        self.assertIn("10080", calls[0])
        self.assertEqual(calls[1][0:5], ["node", "openclaw.mjs", "agent", "--session-id", "session-main-123"])
        self.assertIn("--message", calls[1])
        sent_message = calls[1][calls[1].index("--message") + 1]
        self.assertIn("WATCHDOG_RECOVERY_HINT", sent_message)
        self.assertIn('"target_session_key": "agent:main:main"', sent_message)
        self.assertIn('"type": "WATCHDOG_RECOVERY_HINT"', sent_message)
