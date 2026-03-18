import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from heartbeat_guardrail import (
    DurationProfile,
    HeartbeatPhase,
    build_user_visible_status_template,
    infer_duration_profile,
    resolve_timing_window,
)
from state_store import MonitorStateStore


class ReceiptGateTimingTests(unittest.TestCase):
    def test_phase_profiles_are_not_single_fixed_timeout(self):
        planning = resolve_timing_window(phase=HeartbeatPhase.PLANNING, profile=DurationProfile.SHORT)
        implementation = resolve_timing_window(
            phase=HeartbeatPhase.IMPLEMENTATION,
            profile=DurationProfile.LONG,
        )
        testing = resolve_timing_window(phase=HeartbeatPhase.TESTING, profile=DurationProfile.MEDIUM)

        self.assertLess(planning.first_ack_sla, implementation.first_ack_sla)
        self.assertLess(planning.hard_timeout, implementation.hard_timeout)
        self.assertNotEqual(testing.heartbeat_interval, implementation.heartbeat_interval)

    def test_infer_duration_profile_matches_phase_defaults(self):
        self.assertEqual(
            infer_duration_profile(phase=HeartbeatPhase.PLANNING),
            DurationProfile.SHORT,
        )
        self.assertEqual(
            infer_duration_profile(phase=HeartbeatPhase.IMPLEMENTATION),
            DurationProfile.LONG,
        )
        self.assertEqual(
            infer_duration_profile(phase=HeartbeatPhase.TESTING),
            DurationProfile.MEDIUM,
        )

    def test_status_templates_cover_started_followup_and_blocked(self):
        timing = resolve_timing_window(phase=HeartbeatPhase.IMPLEMENTATION, profile=DurationProfile.LONG)
        started = build_user_visible_status_template(
            control_state="dev_running",
            phase=HeartbeatPhase.IMPLEMENTATION,
            timing=timing,
            heartbeat_ok=True,
        )
        followup = build_user_visible_status_template(
            control_state="dev_running",
            phase=HeartbeatPhase.IMPLEMENTATION,
            timing=timing,
            heartbeat_ok=False,
            followup_stage="soft",
        )
        blocked = build_user_visible_status_template(
            control_state="blocked_unverified",
            phase=HeartbeatPhase.IMPLEMENTATION,
            timing=timing,
            heartbeat_ok=False,
        )

        self.assertIn("已开始且心跳正常", started)
        self.assertIn("正在追证", followup)
        self.assertIn("已 blocked", blocked)


class ReceiptGateStateProjectionTests(unittest.TestCase):
    def test_timing_metadata_exposes_followup_and_blocking_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            store.upsert_task(
                {
                    "task_id": "task-meta",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "running",
                    "current_stage": "implementation:started",
                    "question": "修复回执",
                    "created_at": 1,
                    "updated_at": 1,
                    "started_at": 1,
                    "last_progress_at": 1,
                    "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "started", "evidence": "files=guardian.py"},
                }
            )
            store.upsert_task_contract(
                "task-meta",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )

            control = store.derive_task_control_state("task-meta")
            self.assertEqual(control["timing"]["profile"], "long")
            self.assertEqual(control["timing"]["phase"], "implementation")
            self.assertEqual(control["timing"]["soft_followup"], 120)
            self.assertEqual(control["timing"]["hard_followup"], 420)
            self.assertEqual(control["timing"]["auto_blocked_unverified"], 2700)
            self.assertTrue(control["timing"]["blocked_user_visible"])
            # Phase 2 简化后：evidence_summary 不再包含 timing 字段，直接从 timing 对象检查
            self.assertIn("control_state=dev_running", control["evidence_summary"])
            self.assertIn("followup_stage=soft", control["evidence_summary"])

    def test_followup_template_is_surfaceable_to_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            store.upsert_task(
                {
                    "task_id": "task-1",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "running",
                    "current_stage": "planning",
                    "question": "修复回执",
                    "created_at": 1,
                    "updated_at": 1,
                    "started_at": 1,
                    "last_progress_at": 1,
                }
            )
            store.upsert_task_contract(
                "task-1",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            control = store.derive_task_control_state("task-1")
            action = store.reconcile_task_control_action(store.get_task("task-1"), control)
            details = dict(action.get("details") or {})
            details.update(
                {
                    "duration_profile": "short",
                    "phase": "planning",
                    "status_template": build_user_visible_status_template(
                        control_state="received_only",
                        phase=HeartbeatPhase.PLANNING,
                        timing=resolve_timing_window(phase=HeartbeatPhase.PLANNING, profile=DurationProfile.SHORT),
                        heartbeat_ok=False,
                        followup_stage="soft",
                    ),
                }
            )
            store.update_control_action(int(action["id"]), status="sent", attempts=1, details=details)

            projected = store.derive_task_control_state("task-1")
            self.assertIn("正在追证", projected["user_visible_progress"])
            self.assertEqual(projected["control_action"]["details"]["duration_profile"], "short")

    def test_blocked_template_overrides_user_progress_when_followup_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            store.upsert_task(
                {
                    "task_id": "task-2",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "blocked",
                    "blocked_reason": "control_followup_failed",
                    "current_stage": "planning",
                    "question": "修复回执",
                    "created_at": 1,
                    "updated_at": 1,
                    "started_at": 1,
                    "last_progress_at": 1,
                }
            )
            store.upsert_task_contract(
                "task-2",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed", "test:started", "test:completed"],
                },
            )
            control = store.derive_task_control_state("task-2")
            action = store.reconcile_task_control_action(store.get_task("task-2"), control)
            details = dict(action.get("details") or {})
            details["status_template"] = build_user_visible_status_template(
                control_state="blocked_control_followup_failed",
                phase=HeartbeatPhase.PLANNING,
                timing=resolve_timing_window(phase=HeartbeatPhase.PLANNING, profile=DurationProfile.SHORT),
                heartbeat_ok=False,
            )
            store.update_control_action(int(action["id"]), status="blocked", details=details)

            projected = store.derive_task_control_state("task-2")
            self.assertEqual(projected["control_state"], "blocked_control_followup_failed")
            self.assertIn("已 blocked", projected["user_visible_progress"])


class ReceiptGateFollowupChainTests(unittest.TestCase):
    """回归测试：验证 received_only -> soft followup -> hard followup -> blocked_unverified 完整链路"""

    def test_received_only_enters_soft_followup_after_first_ack_sla(self):
        """验证 received_only 在 first_ack_sla 后进入 soft followup"""
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            now = int(1000)
            store.upsert_task(
                {
                    "task_id": "task-soft",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "running",
                    "current_stage": "planning",
                    "question": "测试软追证",
                    "created_at": now - 100,
                    "updated_at": now - 100,
                    "started_at": now - 100,
                    "last_progress_at": now - 100,
                }
            )
            store.upsert_task_contract(
                "task-soft",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started"],
                },
            )

            # 验证 timing 字段存在且包含 followup 阈值
            control = store.derive_task_control_state("task-soft")
            self.assertEqual(control["control_state"], "received_only")
            self.assertIn("soft_followup", control["timing"])
            self.assertIn("hard_followup", control["timing"])
            self.assertTrue(control["timing"]["blocked_user_visible"])

    def test_soft_followup_escapes_to_hard_followup_after_heartbeat_timeout(self):
        """验证 soft followup 在 heartbeat 超时后进入 hard followup"""
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            now = int(1000)
            store.upsert_task(
                {
                    "task_id": "task-hard",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "running",
                    "current_stage": "implementation",
                    "question": "测试硬追证",
                    "created_at": now - 500,
                    "updated_at": now - 500,
                    "started_at": now - 500,
                    "last_progress_at": now - 500,  # heartbeat 超时
                }
            )
            store.upsert_task_contract(
                "task-hard",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started"],
                },
            )
            # 记录一次追证尝试
            action = store.reconcile_task_control_action(
                store.get_task("task-hard"),
                store.derive_task_control_state("task-hard"),
            )
            store.update_control_action(int(action["id"]), attempts=1, last_followup_at=now - 200)

            # 验证 timing 字段包含 hard_followup 阈值
            control = store.derive_task_control_state("task-hard")
            self.assertIn("hard_followup", control["timing"])
            self.assertFalse(control["heartbeat_ok"])

    def test_hard_followup_failure_transitions_to_blocked_unverified(self):
        """验证 hard followup 失败后转为 blocked_unverified"""
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            now = int(1000)
            store.upsert_task(
                {
                    "task_id": "task-blocked",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "blocked",
                    "blocked_reason": "missing_pipeline_receipt",
                    "current_stage": "implementation",
                    "question": "测试阻塞",
                    "created_at": now - 1000,
                    "updated_at": now - 100,
                    "started_at": now - 1000,
                    "last_progress_at": now - 500,
                }
            )
            store.upsert_task_contract(
                "task-blocked",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started"],
                },
            )
            # 记录两次追证失败
            action = store.reconcile_task_control_action(
                store.get_task("task-blocked"),
                store.derive_task_control_state("task-blocked"),
            )
            store.update_control_action(int(action["id"]), attempts=2, last_followup_at=now - 100)

            control = store.derive_task_control_state("task-blocked")
            self.assertEqual(control["control_state"], "blocked_unverified")
            self.assertTrue(control["timing"]["blocked_user_visible"])
            # 验证用户可见播报包含阻塞信息
            self.assertIn("阻塞", control["user_visible_progress"])

    def test_heartbeat_timeout_triggers_hard_followup_for_running_task(self):
        """验证运行中任务 heartbeat 超窗触发 hard followup"""
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            now = int(1000)
            # 创建一个有 dev receipt 的任务
            store.upsert_task(
                {
                    "task_id": "task-heartbeat",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "running",
                    "current_stage": "implementation",
                    "question": "测试心跳超时",
                    "created_at": now - 600,
                    "updated_at": now - 600,
                    "started_at": now - 600,
                    "last_progress_at": now - 600,  # heartbeat 超时
                    "latest_receipt": {
                        "agent": "dev",
                        "phase": "implementation",
                        "action": "started",
                        "evidence": "files=engine.py",
                    },
                }
            )
            store.upsert_task_contract(
                "task-heartbeat",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started", "dev:completed"],
                },
            )
            # 记录一次追证
            action = store.reconcile_task_control_action(
                store.get_task("task-heartbeat"),
                store.derive_task_control_state("task-heartbeat"),
            )
            store.update_control_action(int(action["id"]), attempts=1, last_followup_at=now - 200)

            control = store.derive_task_control_state("task-heartbeat")
            self.assertEqual(control["control_state"], "dev_running")
            self.assertFalse(control["heartbeat_ok"])
            self.assertIn("hard_followup", control["timing"])

    def test_followup_failure_auto_blocks_and_visible_to_user(self):
        """验证追证失败后自动 blocked 且对用户可见"""
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            now = int(1000)
            store.upsert_task(
                {
                    "task_id": "task-auto-block",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "blocked",
                    "blocked_reason": "control_followup_failed",
                    "current_stage": "planning",
                    "question": "测试自动阻塞",
                    "created_at": now - 2000,
                    "updated_at": now - 100,
                    "started_at": now - 2000,
                    "last_progress_at": now - 1000,
                }
            )
            store.upsert_task_contract(
                "task-auto-block",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed"],
                },
            )

            control = store.derive_task_control_state("task-auto-block")
            self.assertEqual(control["control_state"], "blocked_control_followup_failed")
            self.assertTrue(control["timing"]["blocked_user_visible"])
            # 验证用户可见播报包含阻塞信息
            self.assertIn("阻塞", control["user_visible_progress"])
            # Phase 2 简化后：evidence_summary 不再包含 blocked_user_visible，直接从 timing 对象检查
            self.assertIn("control_state=blocked", control["evidence_summary"]) or self.assertIn("followup_stage=blocked", control["evidence_summary"])

    def test_complete_chain_received_to_blocked(self):
        """完整链路测试：验证 timing 字段和 blocked_user_visible 标志正确传递"""
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp))
            base_time = int(1000)

            # 创建任务
            store.upsert_task(
                {
                    "task_id": "task-chain",
                    "session_key": "agent:main:main",
                    "env_id": "primary",
                    "status": "running",
                    "current_stage": "planning",
                    "question": "完整链路测试",
                    "created_at": base_time,
                    "updated_at": base_time,
                    "started_at": base_time,
                    "last_progress_at": base_time,
                }
            )
            store.upsert_task_contract(
                "task-chain",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["pm:started", "pm:completed", "dev:started"],
                },
            )

            # 验证 timing 字段完整
            control = store.derive_task_control_state("task-chain")
            self.assertIn("soft_followup", control["timing"])
            self.assertIn("hard_followup", control["timing"])
            self.assertIn("auto_blocked_unverified", control["timing"])
            self.assertTrue(control["timing"]["blocked_user_visible"])

            # 模拟追证失败后 blocked
            store.upsert_task(
                {
                    "task_id": "task-chain",
                    "session_key": "agent:main:main",
                    "status": "blocked",
                    "blocked_reason": "missing_pipeline_receipt",
                }
            )
            action = store.reconcile_task_control_action(
                store.get_task("task-chain"),
                store.derive_task_control_state("task-chain"),
            )
            store.update_control_action(int(action["id"]), attempts=2)
            control = store.derive_task_control_state("task-chain")
            self.assertEqual(control["control_state"], "blocked_unverified")
            self.assertTrue(control["timing"]["blocked_user_visible"])
            # 验证用户可见播报包含阻塞信息
            self.assertIn("阻塞", control["user_visible_progress"])


if __name__ == "__main__":
    unittest.main()
