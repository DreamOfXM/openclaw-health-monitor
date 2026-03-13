import unittest
from unittest import mock
from pathlib import Path
import tempfile
import json
import time

import dashboard_backend as dashboard
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

    @mock.patch("dashboard_backend.get_process_info")
    @mock.patch("dashboard_backend.get_process_info_by_pid")
    @mock.patch("dashboard_backend.load_pid_file")
    def test_get_guardian_process_info_prefers_pid_file(self, load_pid_file, by_pid, by_name):
        load_pid_file.return_value = 4321
        by_pid.return_value = {"pid": 4321, "cpu": 0.1, "mem": 0.2, "cmd": "/usr/bin/python guardian.py"}

        info = dashboard.get_guardian_process_info()

        self.assertEqual(info["pid"], 4321)
        by_name.assert_not_called()

    @mock.patch("dashboard_backend.get_process_info")
    @mock.patch("dashboard_backend.get_process_info_by_pid")
    @mock.patch("dashboard_backend.load_pid_file")
    def test_get_guardian_process_info_falls_back_to_name_match(self, load_pid_file, by_pid, by_name):
        load_pid_file.return_value = None
        by_pid.return_value = None
        by_name.return_value = {"pid": 5678, "cpu": 0.1, "mem": 0.2, "cmd": "python guardian.py"}

        info = dashboard.get_guardian_process_info()

        self.assertEqual(info["pid"], 5678)
        by_name.assert_called_once_with(r"[g]uardian\.py")

    def test_active_env_id_defaults_to_primary(self):
        with mock.patch.object(dashboard, "active_binding", return_value={}):
            self.assertEqual(dashboard.active_env_id({"ACTIVE_OPENCLAW_ENV": "primary"}), "primary")
            self.assertEqual(dashboard.active_env_id({"ACTIVE_OPENCLAW_ENV": "official"}), "official")
            self.assertEqual(dashboard.active_env_id({"ACTIVE_OPENCLAW_ENV": "weird"}), "primary")

    def test_env_dashboard_url_includes_token_and_gateway_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "openclaw.json").write_text(
                json.dumps({"gateway": {"auth": {"token": "abc123"}}}),
                encoding="utf-8",
            )
            spec = {"home": str(home), "port": 19021}
            url = dashboard.env_dashboard_url(spec)
            self.assertEqual(
                url,
                "http://127.0.0.1:19021/#token=abc123&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A19021",
            )

    @mock.patch("dashboard_backend.check_gateway_health_for_env")
    @mock.patch("dashboard_backend.env_has_control_ui_assets")
    @mock.patch("dashboard_backend.read_git_head")
    @mock.patch("dashboard_backend.get_listener_pid")
    def test_list_openclaw_environments_marks_active_environment(self, listener_pid, read_git_head, control_ui_ready, health):
        listener_pid.side_effect = [1111, None]
        read_git_head.side_effect = ["abc123", "def456"]
        control_ui_ready.side_effect = [True, True]
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

        with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            envs = dashboard.list_openclaw_environments(config)

        self.assertEqual(len(envs), 2)
        self.assertEqual(envs[0]["id"], "primary")
        self.assertTrue(envs[0]["active"])
        self.assertTrue(envs[0]["running"])
        self.assertTrue(envs[0]["healthy"])
        self.assertTrue(envs[0]["control_ui_ready"])

    @mock.patch("dashboard_backend.Path.exists")
    @mock.patch("dashboard_backend.check_gateway_health_for_env")
    @mock.patch("dashboard_backend.env_has_control_ui_assets")
    @mock.patch("dashboard_backend.read_git_head")
    @mock.patch("dashboard_backend.get_listener_pid")
    def test_list_openclaw_environments_auto_update_follows_config(
        self,
        listener_pid,
        read_git_head,
        control_ui_ready,
        health,
        path_exists,
    ):
        listener_pid.side_effect = [1111, 2222]
        read_git_head.side_effect = ["abc123", "def456"]
        control_ui_ready.side_effect = [True, True]
        health.side_effect = [True, True]
        path_exists.return_value = True
        config = {
            "ACTIVE_OPENCLAW_ENV": "official",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
            "OPENCLAW_OFFICIAL_AUTO_UPDATE": False,
        }

        with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "official"}):
            envs = dashboard.list_openclaw_environments(config)

        official = next(item for item in envs if item["id"] == "official")
        self.assertFalse(official["auto_update_enabled"])
        self.assertFalse(official["auto_update_expected"])
        self.assertTrue(official["auto_update_installed"])
        self.assertTrue(official["auto_update_drift"])
        self.assertEqual(envs[0]["listener_pid"], 1111)
        self.assertEqual(envs[1]["id"], "official")
        self.assertTrue(envs[1]["active"])
        self.assertTrue(envs[1]["running"])

    def test_detect_environment_inconsistencies_reports_dual_listener(self):
        envs = [
            {"id": "primary", "active": True, "running": True},
            {"id": "official", "active": False, "running": True},
        ]
        with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            issues = dashboard.detect_environment_inconsistencies(envs, "primary")
        codes = {item["code"] for item in issues}
        self.assertIn("dual_listener", codes)
        self.assertIn("official_running_while_primary_active", codes)
        self.assertIn("unbound_listener_official", codes)

    def test_detect_environment_inconsistencies_reports_binding_mismatch(self):
        envs = [
            {"id": "primary", "active": True, "running": True},
            {"id": "official", "active": False, "running": False},
        ]
        with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "official"}):
            issues = dashboard.detect_environment_inconsistencies(envs, "primary")
        codes = {item["code"] for item in issues}
        self.assertIn("binding_config_mismatch", codes)
        self.assertIn("bound_env_not_running_official", codes)

    def test_build_model_failure_summary_classifies_auth_failure(self):
        summary = dashboard.build_model_failure_summary(
            [{"time": "11:00:00", "message": "provider 401 oauth token refresh failed"}],
            [],
        )
        self.assertEqual(summary["primary_type"], "auth_failure")

    def test_build_context_lifecycle_readiness_reports_missing_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openclaw"
            code = Path(tmp) / "code"
            home.mkdir(parents=True)
            code.mkdir(parents=True)
            (home / "openclaw.json").write_text(json.dumps({"session": {"resetOnExit": False}}), encoding="utf-8")
            with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
                readiness = dashboard.build_context_lifecycle_readiness(
                    {
                        "ACTIVE_OPENCLAW_ENV": "primary",
                        "OPENCLAW_HOME": str(home),
                        "OPENCLAW_CODE": str(code),
                        "GATEWAY_PORT": 18789,
                        "OPENCLAW_OFFICIAL_STATE": str(home.parent / ".openclaw-official"),
                        "OPENCLAW_OFFICIAL_CODE": str(code.parent / "official-code"),
                        "OPENCLAW_OFFICIAL_PORT": 19021,
                    }
                )
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(len(readiness["checks"]), 5)

    def test_build_context_lifecycle_readiness_reports_degraded_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openclaw"
            code = Path(tmp) / "code"
            home.mkdir(parents=True)
            code.mkdir(parents=True)
            (home / "openclaw.json").write_text(
                json.dumps(
                    {
                        "session": {
                            "memoryFlush": {"enabled": True, "maxTurns": 40},
                            "contextPruning": {"enabled": True, "tokenBudget": 120000},
                            "dailyReset": {"enabled": True, "hour": 4},
                            "idleReset": {"enabled": True, "seconds": 3600},
                            "sessionMaintenance": {"enabled": True, "intervalSeconds": 7200},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
                readiness = dashboard.build_context_lifecycle_readiness(
                    {
                        "ACTIVE_OPENCLAW_ENV": "primary",
                        "OPENCLAW_HOME": str(home),
                        "OPENCLAW_CODE": str(code),
                        "GATEWAY_PORT": 18789,
                        "OPENCLAW_OFFICIAL_STATE": str(home.parent / ".openclaw-official"),
                        "OPENCLAW_OFFICIAL_CODE": str(code.parent / "official-code"),
                        "OPENCLAW_OFFICIAL_PORT": 19021,
                    }
                )
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "degraded")
        self.assertTrue(any("低于基线" in item["detail"] or "高于基线" in item["detail"] for item in readiness["checks"]))

    def test_build_context_lifecycle_readiness_reports_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openclaw"
            code = Path(tmp) / "code"
            home.mkdir(parents=True)
            code.mkdir(parents=True)
            (home / "openclaw.json").write_text(
                json.dumps(
                    {
                        "session": {
                            "memoryFlush": {"enabled": True, "maxTurns": 120},
                            "contextPruning": {"enabled": True, "tokenBudget": 180000},
                            "dailyReset": {"enabled": True, "hour": 4},
                            "idleReset": {"enabled": True, "seconds": 21600},
                            "sessionMaintenance": {"enabled": True, "intervalSeconds": 1800},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
                readiness = dashboard.build_context_lifecycle_readiness(
                    {
                        "ACTIVE_OPENCLAW_ENV": "primary",
                        "OPENCLAW_HOME": str(home),
                        "OPENCLAW_CODE": str(code),
                        "GATEWAY_PORT": 18789,
                        "OPENCLAW_OFFICIAL_STATE": str(home.parent / ".openclaw-official"),
                        "OPENCLAW_OFFICIAL_CODE": str(code.parent / "official-code"),
                        "OPENCLAW_OFFICIAL_PORT": 19021,
                    }
                )
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["status"], "ready")

    @mock.patch("dashboard_backend.check_gateway_health_for_env")
    @mock.patch("dashboard_backend.env_has_control_ui_assets")
    @mock.patch("dashboard_backend.read_git_target_head")
    @mock.patch("dashboard_backend.read_git_head")
    @mock.patch("dashboard_backend.get_listener_pid")
    def test_list_openclaw_environments_only_active_running_env_gets_dashboard_link(
        self,
        listener_pid,
        read_git_head,
        read_git_target_head,
        control_ui_ready,
        health,
    ):
        listener_pid.side_effect = [1111, 2222]
        read_git_head.side_effect = ["abc123", "def456"]
        read_git_target_head.return_value = "def456"
        control_ui_ready.side_effect = [True, True, True, True]
        health.side_effect = [True, True]
        config = {
            "ACTIVE_OPENCLAW_ENV": "primary",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
        }

        with mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            envs = dashboard.list_openclaw_environments(config)
        primary = next(item for item in envs if item["id"] == "primary")
        official = next(item for item in envs if item["id"] == "official")

        self.assertTrue(primary["active"])
        self.assertTrue(primary["running"])
        self.assertEqual(primary["dashboard_open_link"], "/open-dashboard/primary")
        self.assertFalse(official["active"])
        self.assertTrue(official["running"])
        self.assertEqual(official["dashboard_open_link"], "")

    @mock.patch("dashboard_backend.check_gateway_health_for_env")
    @mock.patch("dashboard_backend.env_has_control_ui_assets")
    @mock.patch("dashboard_backend.read_git_target_head")
    @mock.patch("dashboard_backend.read_git_head")
    @mock.patch("dashboard_backend.get_listener_pid")
    def test_list_openclaw_environments_hides_dashboard_link_when_control_ui_missing(
        self,
        listener_pid,
        read_git_head,
        read_git_target_head,
        control_ui_ready,
        health,
    ):
        listener_pid.side_effect = [1111, 2222]
        read_git_head.side_effect = ["abc123", "def456"]
        read_git_target_head.return_value = "def456"
        control_ui_ready.side_effect = [True, False]
        health.side_effect = [True, True]
        config = {
            "ACTIVE_OPENCLAW_ENV": "official",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
        }

        envs = dashboard.list_openclaw_environments(config)
        official = next(item for item in envs if item["id"] == "official")

        self.assertTrue(official["active"])
        self.assertTrue(official["running"])
        self.assertFalse(official["control_ui_ready"])
        self.assertEqual(official["dashboard_open_link"], "")

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
            store.record_task_event(
                "task-1",
                "pipeline_receipt",
                {"receipt": {"agent": "dev", "phase": "implementation", "action": "started", "ack_id": "ack-dev"}},
            )
            store.upsert_task_contract(
                "task-1",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": ["dev:completed", "test:completed"],
                },
            )
            store.reconcile_task_control_action(
                store.get_task("task-1"),
                {
                    "control_state": "dev_running",
                    "next_action": "await_dev_completion",
                    "approved_summary": "等待开发回执",
                    "required_receipts": ["dev:completed", "test:completed"],
                    "next_actor": "dev",
                    "claim_level": "execution_verified",
                    "contract": {"id": "delivery_pipeline"},
                    "phase_statuses": [
                        {"agent": "pm", "label": "产品", "state": "completed"},
                        {"agent": "dev", "label": "开发", "state": "running"},
                    ],
                },
            )

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
            self.assertEqual(payload["current"]["control"]["truth_level"], "derived")
            self.assertEqual(payload["current"]["control"]["claim_level"], "phase_verified")
            self.assertEqual(payload["current"]["control"]["next_actor"], "dev")
            self.assertEqual(payload["current"]["control"]["native_state"]["status"], "running")
            self.assertEqual(payload["current"]["control"]["derived_state"]["contract_id"], "delivery_pipeline")
            self.assertEqual(payload["control_queue"][0]["action_type"], "await_dev_completion")
            self.assertEqual(payload["session_resolution"]["active_task_id"], "task-1")
            self.assertEqual(len(payload["current"]["timeline"]), 3)

    def test_get_active_agent_activity_reads_recent_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = {
                "AGENT_ACTIVITY_LOOKBACK_SECONDS": 1800,
                "AGENT_ACTIVITY_SCAN_LIMIT": 12,
            }
            openclaw_json = {
                "agents": {
                    "list": [
                        {"id": "pm", "identity": {"name": "产品经理", "emoji": "📋"}},
                        {"id": "dev", "identity": {"name": "开发工程师", "emoji": "💻"}},
                    ]
                }
            }
            (home / "openclaw.json").write_text(json.dumps(openclaw_json, ensure_ascii=False), encoding="utf-8")

            pm_dir = home / "agents" / "pm" / "sessions"
            pm_dir.mkdir(parents=True, exist_ok=True)
            pm_file = pm_dir / "session-a.jsonl"
            pm_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "[Subagent Task]: A股实时数据方案与回测系统"}],
                                },
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "type": "message",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "toolCall",
                                            "name": "sessions_spawn",
                                            "arguments": {"agentId": "dev", "label": "A股实现"},
                                        }
                                    ],
                                },
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            dev_dir = home / "agents" / "dev" / "sessions"
            dev_dir.mkdir(parents=True, exist_ok=True)
            dev_file = dev_dir / "session-b.jsonl"
            dev_file.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "ANNOUNCE_SKIP"}],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            now = time.time()
            os_times = (now, now)
            import os

            os.utime(pm_file, os_times)
            os.utime(dev_file, os_times)

            payload = dashboard.get_active_agent_activity({"home": home}, config)

            self.assertEqual(payload["summary"]["active_agents"], 2)
            self.assertEqual(len(payload["agents"]), 2)
            agent_ids = {item["agent_id"] for item in payload["agents"]}
            self.assertEqual(agent_ids, {"pm", "dev"})

    def test_build_environment_promotion_summary_requires_healthy_official(self):
        environments = [
            {"id": "primary", "git_head": "aaa111", "running": False, "healthy": False},
            {"id": "official", "git_head": "bbb222", "running": True, "healthy": True},
        ]
        task_registry = {"summary": {"blocked": 0}, "current": {"control": {"control_state": "completed_verified"}}}
        summary = dashboard.build_environment_promotion_summary(environments, task_registry)
        self.assertTrue(summary["safe_to_promote"])
        self.assertIn("bbb222", " ".join(summary["reasons"]))

    def test_get_learning_center_payload_exposes_summary_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            store.upsert_learning(
                learning_key="lk-1",
                env_id="primary",
                task_id="task-1",
                category="control_plane",
                title="缺少回执",
                detail="task missing ack",
                evidence={"task_id": "task-1"},
                status="pending",
            )
            store.record_reflection_run("scheduled", {"promoted": 1, "reviewed": 2})
            with mock.patch.object(dashboard, "STORE", store), \
                mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "primary"}), \
                mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary", "home": base / ".openclaw"}):
                payload = dashboard.get_learning_center_payload(limit=10)
            self.assertEqual(payload["summary"]["pending"], 1)
            self.assertEqual(payload["reflections"][0]["summary"]["promoted"], 1)
            self.assertEqual(payload["suggestions"][0]["title"], "缺少回执")
            self.assertEqual(payload["source_mode"], "legacy_store")

    def test_get_learning_center_payload_prefers_openclaw_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            learnings_dir = home / ".learnings"
            learnings_dir.mkdir(parents=True)
            now = int(time.time())
            (learnings_dir / "pending.jsonl").write_text(
                json.dumps(
                    {
                        "learning_id": "l1",
                        "status": "pending",
                        "summary": "artifact pending",
                        "detail": "pending detail",
                        "category": "delivery_failure",
                        "source_task_id": "task-1",
                        "source_session_id": "session-1",
                        "occurrences": 2,
                        "created_at": now - 120,
                        "updated_at": now - 60,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (learnings_dir / "promoted.jsonl").write_text(
                json.dumps(
                    {
                        "learning_id": "l2",
                        "status": "promoted",
                        "summary": "artifact promoted",
                        "decision_reason": "stable pattern",
                        "category": "delivery_failure",
                        "source_task_ids": ["task-1", "task-2"],
                        "occurrences": 3,
                        "promoted_by_run_id": "refl-1",
                        "injection_target": {"type": "Skills", "path": "Skills/finalization.md"},
                        "created_at": now - 100,
                        "updated_at": now - 20,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (learnings_dir / "discarded.jsonl").write_text("", encoding="utf-8")
            (learnings_dir / "reflection-runs.jsonl").write_text(
                json.dumps(
                    {
                        "run_type": "daily-reflection",
                        "status": "succeeded",
                        "decisions": {"promoted": 1, "discarded": 0, "kept": 1},
                        "created_at": now - 10,
                        "finished_at": now - 10,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "primary"}), \
                mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary", "home": home}):
                payload = dashboard.get_learning_center_payload(limit=10)
            self.assertEqual(payload["source_mode"], "openclaw_artifact")
            self.assertEqual(payload["summary"]["pending"], 1)
            self.assertEqual(payload["summary"]["promoted"], 1)
            self.assertEqual(payload["learnings"][0]["title"], "artifact promoted")
            self.assertEqual(payload["learnings"][0]["promoted_target"], "Skills")
            self.assertEqual(payload["reflections"][0]["summary"]["promoted"], 1)

    def test_get_health_acceptance_payload_summarizes_acceptance_recovery_and_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = MonitorStateStore(base)
            now = int(time.time())
            store.upsert_task(
                {
                    "task_id": "task-ok",
                    "session_key": "session-ok",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "completed",
                    "current_stage": "testing:completed",
                    "question": "实现验收看板",
                    "last_user_message": "实现验收看板",
                    "started_at": now - 120,
                    "last_progress_at": now - 30,
                    "created_at": now - 120,
                    "updated_at": now - 10,
                    "completed_at": now - 5,
                    "latest_receipt": {"agent": "test", "phase": "testing", "action": "completed"},
                }
            )
            store.upsert_task_contract(
                "task-ok",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:completed",
                        "dev:completed",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event("task-ok", "dispatch_started", {"question": "实现验收看板"})
            store.record_task_event("task-ok", "pipeline_receipt", {"receipt": {"agent": "pm", "phase": "planning", "action": "completed"}})
            store.record_task_event("task-ok", "pipeline_receipt", {"receipt": {"agent": "dev", "phase": "implementation", "action": "completed"}})
            store.record_task_event("task-ok", "pipeline_receipt", {"receipt": {"agent": "test", "phase": "testing", "action": "completed"}})
            store.record_task_event("task-ok", "visible_completion", {"text": "已完成"})
            store.record_task_event("task-ok", "recovery_started", {"recovery_kind": "pipeline_detached", "rebind_target": "dev"})
            store.record_task_event("task-ok", "recovery_succeeded", {"recovery_kind": "pipeline_detached", "rebind_target": "dev"})

            store.upsert_task(
                {
                    "task_id": "task-risk",
                    "session_key": "session-risk",
                    "env_id": "primary",
                    "channel": "feishu_dm",
                    "status": "no_reply",
                    "current_stage": "implementation:completed",
                    "question": "做一个会失联的任务",
                    "last_user_message": "做一个会失联的任务",
                    "started_at": now - 3600,
                    "last_progress_at": now - 3600,
                    "created_at": now - 3600,
                    "updated_at": now - 1800,
                }
            )
            store.upsert_task_contract(
                "task-risk",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:started",
                        "pm:completed",
                        "dev:started",
                        "dev:completed",
                        "test:started",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event("task-risk", "dispatch_started", {"question": "做一个会失联的任务"})

            store.upsert_watcher_task(
                {
                    "watcher_task_id": "watch-1",
                    "env_id": "primary",
                    "source_agent": "main",
                    "target_agent": "dev",
                    "intent": "THREADED_EXECUTION",
                    "current_state": "completed",
                    "completed_at": now - 20,
                    "delivered_at": 0,
                    "payload": {"request_id": "watch-1"},
                }
            )
            store.upsert_learning(
                learning_key="learn-1",
                env_id="primary",
                task_id="task-risk",
                category="control_plane",
                title="回执缺失需要重点观察",
                detail="当任务缺少结构化回执时需要尽快进入恢复流程",
                evidence={"task_id": "task-risk"},
                status="promoted",
            )
            store.upsert_learning(
                learning_key="learn-1",
                env_id="primary",
                task_id="task-risk",
                category="control_plane",
                title="回执缺失需要重点观察",
                detail="当任务缺少结构化回执时需要尽快进入恢复流程",
                evidence={"task_id": "task-risk"},
                status="promoted",
            )
            store.record_reflection_run("scheduled", {"promoted": 1, "reviewed": 1})

            with mock.patch.object(dashboard, "STORE", store), \
                mock.patch.object(dashboard, "load_config", return_value={"TASK_SILENT_TIMEOUT_SECONDS": 900}), \
                mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary"}), \
                mock.patch.object(dashboard, "get_learning_center_payload", return_value={"source_mode": "legacy_store", "learnings": []}), \
                mock.patch.object(dashboard, "build_context_lifecycle_readiness", return_value={"status": "ready", "checks": [{"name": "SOUL.md", "ok": True, "detail": "ok"}]}), \
                mock.patch.object(dashboard, "build_learning_supervision_snapshot", return_value={"artifact_status": "ready", "repeat_error_trend": "down"}):
                payload = dashboard.get_health_acceptance_payload()

            self.assertEqual(payload["env"], "primary")
            self.assertGreater(payload["acceptance"]["chain_integrity_rate"], 0)
            self.assertEqual(payload["acceptance"]["completed_not_delivered_count"], 1)
            self.assertEqual(payload["recovery"]["recovery_started_count"], 1)
            self.assertEqual(payload["recovery"]["recovery_success_rate"], 100.0)
            self.assertEqual(payload["learning"]["learning_reuse_count"], 1)
            self.assertEqual(payload["learning_supervision"]["artifact_status"], "ready")
            self.assertTrue(payload["baseline"]["ready"])
            self.assertTrue(payload["high_risk_tasks"])
            self.assertEqual(payload["assistant_profile"]["generality"]["level"], "OpenClaw-first")

    def test_get_health_acceptance_payload_warns_when_learning_artifacts_missing(self):
        with mock.patch.object(dashboard, "load_config", return_value={"TASK_SILENT_TIMEOUT_SECONDS": 900}), \
            mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
            mock.patch.object(dashboard, "env_spec", return_value={"id": "primary"}), \
            mock.patch.object(dashboard.STORE, "summarize_tasks", return_value={"completed": 0, "no_reply": 0}), \
            mock.patch.object(dashboard.STORE, "summarize_watcher_tasks", return_value={"total": 0, "undelivered": 0}), \
            mock.patch.object(dashboard.STORE, "list_tasks", return_value=[]), \
            mock.patch.object(dashboard.STORE, "list_learnings", return_value=[]), \
            mock.patch.object(dashboard.STORE, "list_reflection_runs", return_value=[]), \
            mock.patch.object(dashboard, "get_learning_center_payload", return_value={"source_mode": "legacy_store", "learnings": []}), \
            mock.patch.object(dashboard, "build_context_lifecycle_readiness", return_value={"status": "ready", "checks": []}), \
            mock.patch.object(dashboard, "build_learning_supervision_snapshot", return_value={"artifact_status": "missing", "repeat_error_trend": "insufficient_data"}), \
            mock.patch.object(dashboard, "build_self_check_supervision_snapshot", return_value={"self_check_artifact_status": "ready", "self_check_status": "succeeded"}), \
            mock.patch.object(dashboard, "build_main_closure_supervision_snapshot", return_value={"main_closure_artifact_status": "ready", "delivery_failed_count": 0, "adoption_pending_count": 0}):
            payload = dashboard.get_health_acceptance_payload()
        self.assertEqual(payload["status"], "warning")
        self.assertIn("过渡态", payload["headline"])

    def test_build_learning_supervision_snapshot_reads_openclaw_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            learnings_dir = home / ".learnings"
            learnings_dir.mkdir(parents=True)
            now = int(time.time())
            (home / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (learnings_dir / "pending.jsonl").write_text(
                json.dumps(
                    {
                        "learning_id": "l1",
                        "status": "pending",
                        "summary": "pending item",
                        "source_task_id": "task-1",
                        "source_session_id": "session-1",
                        "occurrences": 1,
                        "created_at": now - 200,
                        "updated_at": now - 120,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (learnings_dir / "promoted.jsonl").write_text(
                json.dumps(
                    {
                        "learning_id": "l2",
                        "status": "promoted",
                        "summary": "promoted item",
                        "promoted_by_run_id": "refl-1",
                        "injection_target": {"type": "Skills", "path": "Skills/delivery.md"},
                        "created_at": now - 180,
                        "updated_at": now - 60,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (learnings_dir / "discarded.jsonl").write_text(
                json.dumps(
                    {
                        "learning_id": "l3",
                        "status": "discarded",
                        "created_at": now - 170,
                        "updated_at": now - 80,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (learnings_dir / "reflection-runs.jsonl").write_text(
                json.dumps(
                    {
                        "run_type": "daily-reflection",
                        "status": "succeeded",
                        "created_at": now - 30,
                        "finished_at": now - 30,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (learnings_dir / "reuse-evidence.jsonl").write_text(
                json.dumps(
                    {
                        "learning_id": "l2",
                        "created_at": now - 20,
                        "updated_at": now - 20,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary", "home": home}):
                payload = dashboard.build_learning_supervision_snapshot({"ACTIVE_OPENCLAW_ENV": "primary"})
            self.assertEqual(payload["artifact_status"], "ready")
            self.assertEqual(payload["daily_reflection_status"], "succeeded")
            self.assertEqual(payload["reuse_evidence_count"], 1)
            self.assertEqual(payload["recent_promoted_items"][0]["learning_id"], "l2")

    def test_build_self_check_supervision_snapshot_reads_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            self_check_dir = home / "shared-context" / "self-check"
            self_check_dir.mkdir(parents=True)
            now = int(time.time())
            (self_check_dir / "self-check-runtime-status.json").write_text(
                json.dumps(
                    {
                        "env_id": "primary",
                        "self_check_artifact_status": "ready",
                        "last_self_check_at": now - 20,
                        "self_check_status": "succeeded",
                        "last_self_recovery_at": now - 10,
                        "last_self_recovery_result": "delivery_retry_succeeded",
                        "delivery_retry_count": 2,
                        "completed_not_delivered_count": 1,
                        "stale_subagent_count": 0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (self_check_dir / "self-check-events.json").write_text(
                json.dumps(
                    {
                        "events": [
                            {"event_type": "self_check_detected_stall", "task_id": "task-1", "created_at": now - 20},
                            {"event_type": "self_check_recovery_succeeded", "task_id": "task-1", "created_at": now - 10},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary", "home": home}):
                payload = dashboard.build_self_check_supervision_snapshot({"ACTIVE_OPENCLAW_ENV": "primary"})
            self.assertEqual(payload["self_check_artifact_status"], "ready")
            self.assertEqual(payload["self_check_status"], "succeeded")
            self.assertEqual(payload["delivery_retry_count"], 2)
            self.assertIsNotNone(payload["self_check_freshness"])
            self.assertEqual(payload["recent_event_types"][0], "self_check_recovery_succeeded")
            self.assertEqual(len(payload["events"]), 2)

    def test_build_self_check_supervision_snapshot_marks_invalid_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".openclaw"
            self_check_dir = home / "shared-context" / "self-check"
            self_check_dir.mkdir(parents=True)
            (self_check_dir / "self-check-runtime-status.json").write_text(json.dumps({"env_id": "primary"}), encoding="utf-8")
            (self_check_dir / "self-check-events.json").write_text(json.dumps({"oops": True}), encoding="utf-8")
            with mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary", "home": home}):
                payload = dashboard.build_self_check_supervision_snapshot({"ACTIVE_OPENCLAW_ENV": "primary"})
            self.assertEqual(payload["self_check_artifact_status"], "invalid")

    def test_api_health_acceptance_exposes_aggregated_payload(self):
        with mock.patch.object(dashboard, "get_health_acceptance_payload", return_value={"status": "healthy", "env": "primary"}):
            with dashboard.app.test_client() as client:
                response = client.get("/api/health-acceptance")
        payload = response.get_json()
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["env"], "primary")

    def test_api_shared_state_exposes_normalized_objects(self):
        with mock.patch.object(dashboard, "build_shared_state_snapshot", return_value={"runtime_health": {}, "learning_backlog": {}}):
            with dashboard.app.test_client() as client:
                response = client.get("/api/shared-state")
        payload = response.get_json()
        self.assertIn("runtime_health", payload)
        self.assertIn("learning_backlog", payload)

    def test_build_bootstrap_status_reports_created_structure_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".openclaw"
            home.mkdir()
            (home / "openclaw.json").write_text("{}", encoding="utf-8")
            cfg = {
                "ACTIVE_OPENCLAW_ENV": "primary",
                "OPENCLAW_HOME": str(home),
                "OPENCLAW_CODE": str(Path(tmp) / "code"),
                "GATEWAY_PORT": 18789,
                "OPENCLAW_OFFICIAL_STATE": str(Path(tmp) / ".openclaw-official"),
                "OPENCLAW_OFFICIAL_CODE": str(Path(tmp) / "code-official"),
                "OPENCLAW_OFFICIAL_PORT": 19021,
            }
            with mock.patch.object(dashboard, "load_config", return_value=cfg), \
                mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
                payload = dashboard.build_bootstrap_status(cfg)
            self.assertEqual(payload["env_id"], "primary")
            self.assertEqual(payload["write_mode"], "check_only")
            self.assertIn("config_merge", payload)
            self.assertEqual(payload["config_merge"]["mode"], "merge_missing")

    def test_build_shared_state_snapshot_includes_bootstrap_and_watcher(self):
        cfg = {
            "ACTIVE_OPENCLAW_ENV": "primary",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
        }
        with mock.patch.object(dashboard, "load_config", return_value=cfg), \
            mock.patch.object(dashboard, "list_openclaw_environments", return_value=[]), \
            mock.patch.object(dashboard, "get_task_registry_payload", return_value={}), \
            mock.patch.object(dashboard, "get_control_plane_overview", return_value={}), \
            mock.patch.object(dashboard, "get_learning_center_payload", return_value={}), \
            mock.patch.object(dashboard, "build_learning_supervision_snapshot", return_value={"generated_at": 1, "env_id": "primary", "memory_freshness": 120, "reuse_evidence_count": 2, "reuse_evidence_7d": 1}), \
            mock.patch.object(dashboard, "build_self_check_supervision_snapshot", return_value={"generated_at": 1, "env_id": "primary", "self_check_status": "succeeded", "events": [], "delivery_retry_count": 1, "completed_not_delivered_count": 0, "stale_subagent_count": 0}), \
            mock.patch.object(dashboard, "build_main_closure_supervision_snapshot", return_value={"generated_at": 1, "env_id": "primary", "foreground_root_task_id": "rt-1", "events": [], "delivery_failed_count": 0, "adoption_pending_count": 0}), \
            mock.patch.object(dashboard, "get_system_metrics", return_value={}), \
            mock.patch.object(dashboard, "get_recent_anomalies", return_value=[]), \
            mock.patch.object(dashboard, "build_bootstrap_status", return_value={"env_id": "primary", "config_merge": {"applied": ["session.memoryFlush"], "preserved": []}, "context_readiness": {"status": "degraded"}}), \
            mock.patch.object(dashboard.STORE, "summarize_watcher_tasks", return_value={"total": 0}):
            payload = dashboard.build_shared_state_snapshot(cfg)
        self.assertIn("bootstrap_status", payload)
        self.assertIn("config_drift", payload)
        self.assertIn("watcher_summary", payload)
        self.assertIn("learning_runtime_status", payload)
        self.assertIn("reflection_freshness", payload)
        self.assertIn("memory_freshness", payload)
        self.assertIn("reuse_evidence_summary", payload)
        self.assertIn("self_check_runtime_status", payload)
        self.assertIn("self_check_events", payload)
        self.assertIn("main_closure_runtime_status", payload)
        self.assertIn("main_closure_events", payload)
        self.assertIn("restart_runtime_status", payload)
        self.assertIn("restart_events", payload)

    def test_build_main_closure_supervision_snapshot_reads_artifacts(self):
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
                        "adoption_pending_count": 2,
                        "finalization_pending_count": 1,
                        "delivery_failed_count": 1,
                        "late_result_count": 1,
                        "binding_source_counts": {"followup_default": 2},
                        "roots": [{"root_task_id": "rt-1", "status": "active"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (closure_dir / "main-closure-events.json").write_text(
                json.dumps(
                    {
                        "events": [
                            {"event_type": "receipt_adopted", "created_at": now - 20},
                            {"event_type": "final_delivery_failed", "created_at": now - 10},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
                mock.patch.object(dashboard, "env_spec", return_value={"id": "primary", "home": home}):
                payload = dashboard.build_main_closure_supervision_snapshot({"ACTIVE_OPENCLAW_ENV": "primary"})
            self.assertEqual(payload["main_closure_artifact_status"], "ready")
            self.assertEqual(payload["foreground_root_task_id"], "rt-1")
            self.assertEqual(payload["delivery_failed_count"], 1)
            self.assertEqual(payload["recent_event_types"][0], "final_delivery_failed")

    def test_get_health_acceptance_payload_flags_main_closure_delivery_failure(self):
        with mock.patch.object(dashboard, "load_config", return_value={"TASK_SILENT_TIMEOUT_SECONDS": 900}), \
            mock.patch.object(dashboard, "active_env_id", return_value="primary"), \
            mock.patch.object(dashboard, "env_spec", return_value={"id": "primary"}), \
            mock.patch.object(dashboard.STORE, "summarize_tasks", return_value={"completed": 0, "no_reply": 0}), \
            mock.patch.object(dashboard.STORE, "summarize_watcher_tasks", return_value={"total": 0, "undelivered": 0}), \
            mock.patch.object(dashboard.STORE, "list_tasks", return_value=[]), \
            mock.patch.object(dashboard.STORE, "list_learnings", return_value=[]), \
            mock.patch.object(dashboard.STORE, "list_reflection_runs", return_value=[]), \
            mock.patch.object(dashboard, "get_learning_center_payload", return_value={"source_mode": "legacy_store", "learnings": []}), \
            mock.patch.object(dashboard, "build_context_lifecycle_readiness", return_value={"status": "ready", "checks": []}), \
            mock.patch.object(dashboard, "build_learning_supervision_snapshot", return_value={"artifact_status": "ready", "repeat_error_trend": "flat"}), \
            mock.patch.object(dashboard, "build_self_check_supervision_snapshot", return_value={"self_check_artifact_status": "ready", "self_check_status": "succeeded"}), \
            mock.patch.object(dashboard, "build_main_closure_supervision_snapshot", return_value={"main_closure_artifact_status": "ready", "delivery_failed_count": 1, "adoption_pending_count": 0}):
            payload = dashboard.get_health_acceptance_payload()
        self.assertEqual(payload["status"], "critical")
        self.assertIn("未成功送达", payload["headline"])
        self.assertEqual(payload["main_closure"]["delivery_failed_count"], 1)

    def test_snapshot_env_id_detects_official_suffix(self):
        self.assertEqual(dashboard.snapshot_env_id("20260311-before-config-change-official"), "official")
        self.assertEqual(dashboard.snapshot_env_id("20260311-before-config-change-primary"), "primary")

    def test_restore_snapshot_and_restart_skips_restart_for_inactive_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            official_home = base / ".openclaw-official"
            snapshot_root = base / "snapshots"
            target = snapshot_root / "20260311-test-official"
            target.mkdir(parents=True)
            (target / "manifest.json").write_text("{}", encoding="utf-8")
            cfg = {
                "ACTIVE_OPENCLAW_ENV": "primary",
                "OPENCLAW_HOME": str(base / ".openclaw"),
                "OPENCLAW_CODE": str(base / "code-primary"),
                "GATEWAY_PORT": 18789,
                "OPENCLAW_OFFICIAL_STATE": str(official_home),
                "OPENCLAW_OFFICIAL_CODE": str(base / "code-official"),
                "OPENCLAW_OFFICIAL_PORT": 19021,
            }
            with mock.patch.object(dashboard, "BASE_DIR", base), \
                mock.patch.object(dashboard, "load_config", return_value=cfg), \
                mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}), \
                mock.patch.object(dashboard, "restart_active_openclaw_environment") as restart_env, \
                mock.patch("dashboard_backend.SnapshotManager.restore_snapshot") as restore_snapshot:
                ok, message = dashboard.restore_snapshot_and_restart("20260311-test-official")
        self.assertTrue(ok)
        self.assertIn("未切换当前活动环境", message)
        restore_snapshot.assert_called_once()
        restart_env.assert_not_called()

    def test_get_control_plane_overview_reports_recoverable_tasks(self):
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
                    "question": "帮我做一个系统",
                    "last_user_message": "帮我做一个系统",
                    "started_at": 1,
                    "last_progress_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                    "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "started"},
                }
            )
            store.upsert_task_contract(
                "task-1",
                {
                    "id": "delivery_pipeline",
                    "required_receipts": [
                        "pm:started",
                        "pm:completed",
                        "dev:started",
                        "dev:completed",
                        "test:started",
                        "test:completed",
                    ],
                },
            )
            store.record_task_event(
                "task-1",
                "pipeline_receipt",
                {"receipt": {"agent": "pm", "phase": "planning", "action": "completed"}},
            )
            store.record_task_event(
                "task-1",
                "pipeline_receipt",
                {"receipt": {"agent": "dev", "phase": "implementation", "action": "started"}},
            )
            store.reconcile_task_control_action(store.get_task("task-1"), store.derive_task_control_state("task-1"))
            with mock.patch.object(dashboard, "STORE", store):
                payload = dashboard.get_control_plane_overview("primary")
            self.assertEqual(payload["tasks"]["recoverable"], 1)
            self.assertEqual(payload["tasks"]["next_actor_counts"]["dev"], 1)

    @mock.patch("dashboard_backend.wait_for_env_listener")
    @mock.patch("dashboard_backend.run_script")
    @mock.patch("dashboard_backend.save_config")
    @mock.patch("dashboard_backend.get_listener_pid", return_value=None)
    @mock.patch("dashboard_backend.enforce_single_active_listener")
    @mock.patch("dashboard_backend.restore_environment_after_failed_switch")
    def test_switch_openclaw_environment_rolls_back_when_primary_does_not_start(
        self,
        restore_previous,
        single_active,
        _get_listener_pid,
        save_config,
        run_script,
        wait_for_env_listener,
    ):
        restore_previous.return_value = (True, "restored")
        single_active.return_value = (True, "ok")
        cfg = {"ACTIVE_OPENCLAW_ENV": "official"}

        def fake_save_config(key, value):
            cfg[key] = value
            return True

        save_config.side_effect = fake_save_config
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "started", ""),
        ]
        wait_for_env_listener.return_value = False

        with mock.patch.object(dashboard, "load_config", side_effect=lambda: dict(cfg)), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "official"}), \
            mock.patch.object(dashboard, "STORE") as store:
            ok, message = dashboard.switch_openclaw_environment("primary")

        self.assertFalse(ok)
        self.assertIn("Gateway 未成功启动", message)
        self.assertEqual(save_config.call_args_list[0].args, ("ACTIVE_OPENCLAW_ENV", "primary"))
        self.assertEqual(save_config.call_args_list[1].args, ("ACTIVE_OPENCLAW_ENV", "official"))
        restore_previous.assert_called_once_with("official")
        self.assertEqual(store.save_runtime_value.call_args_list[0].args[1]["env_id"], "primary")
        self.assertIn("official", [call.args[1]["env_id"] for call in store.save_runtime_value.call_args_list])

    @mock.patch("dashboard_backend.wait_for_env_listener")
    @mock.patch("dashboard_backend.run_script")
    @mock.patch("dashboard_backend.save_config")
    @mock.patch("dashboard_backend.enforce_single_active_listener")
    def test_switch_openclaw_environment_succeeds_when_primary_listener_is_ready(
        self,
        single_active,
        save_config,
        run_script,
        wait_for_env_listener,
    ):
        single_active.return_value = (True, "ok")
        cfg = {"ACTIVE_OPENCLAW_ENV": "official"}

        def fake_save_config(key, value):
            cfg[key] = value
            return True

        save_config.side_effect = fake_save_config
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "started", ""),
        ]
        wait_for_env_listener.return_value = True

        with mock.patch.object(dashboard, "load_config", side_effect=lambda: dict(cfg)), \
            mock.patch.object(dashboard, "active_binding", side_effect=lambda current_cfg: {
                "active_env": current_cfg.get("ACTIVE_OPENCLAW_ENV", "primary"),
                "expected": {
                    "gateway_port": 18789 if current_cfg.get("ACTIVE_OPENCLAW_ENV") != "official" else 19001,
                    "gateway_label": "ai.openclaw.gateway" if current_cfg.get("ACTIVE_OPENCLAW_ENV") != "official" else "ai.openclaw.gateway.official",
                    "config_path": "/tmp/openclaw.json",
                },
            }), \
            mock.patch.object(dashboard, "get_env_specs", return_value={
                "primary": {
                    "id": "primary",
                    "name": "当前主用版",
                    "port": 18789,
                    "gateway_label": "ai.openclaw.gateway",
                    "config_path": Path("/tmp/openclaw.json"),
                },
                "official": {
                    "id": "official",
                    "name": "官方验证版",
                    "port": 19001,
                    "gateway_label": "ai.openclaw.gateway.official",
                    "config_path": Path("/tmp/openclaw-official.json"),
                },
            }), \
            mock.patch.object(dashboard, "get_listener_pid", side_effect=[None, None, 1111, None]), \
            mock.patch.object(dashboard, "STORE") as store:
            ok, message = dashboard.switch_openclaw_environment("primary")

        self.assertTrue(ok)
        self.assertEqual(message, "started")
        self.assertEqual(save_config.call_count, 1)
        self.assertEqual(store.save_runtime_value.call_args.args[1]["env_id"], "primary")

    @mock.patch("dashboard_backend.wait_for_env_listener")
    @mock.patch("dashboard_backend.run_script")
    @mock.patch("dashboard_backend.get_listener_pid")
    @mock.patch("dashboard_backend.enforce_single_active_listener")
    def test_restart_active_openclaw_environment_restarts_official_only(
        self,
        single_active,
        get_listener_pid,
        run_script,
        wait_for_env_listener,
    ):
        single_active.return_value = (True, "ok")
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "official started", ""),
        ]
        get_listener_pid.side_effect = [2222, 3333]
        wait_for_env_listener.return_value = True

        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "official"}), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "official"}), \
            mock.patch.object(dashboard, "write_active_binding") as write_active_binding:
            with mock.patch.object(dashboard, "STORE") as store:
                ok, message, old_pid, new_pid, env_id = dashboard.restart_active_openclaw_environment()

        self.assertTrue(ok)
        self.assertEqual(message, "official started")
        self.assertEqual(old_pid, "2222")
        self.assertEqual(new_pid, "3333")
        self.assertEqual(env_id, "official")
        self.assertEqual(store.append_runtime_event.call_count, 4)
        write_active_binding.assert_called_once()
        self.assertEqual(write_active_binding.call_args.args[2], "official")
        self.assertEqual(write_active_binding.call_args.kwargs["switch_state"], "committed")
        self.assertEqual(
            [call.args[0] for call in run_script.call_args_list],
            [
                [str(dashboard.OFFICIAL_MANAGER), "stop"],
                [str(dashboard.DESKTOP_RUNTIME), "stop", "gateway"],
                [str(dashboard.OFFICIAL_MANAGER), "start"],
            ],
        )
        wait_for_env_listener.assert_called_once_with("official")

    @mock.patch("dashboard_backend.wait_for_env_listener")
    @mock.patch("dashboard_backend.run_script")
    @mock.patch("dashboard_backend.get_listener_pid")
    @mock.patch("dashboard_backend.enforce_single_active_listener")
    def test_restart_active_openclaw_environment_restarts_primary_only(
        self,
        single_active,
        get_listener_pid,
        run_script,
        wait_for_env_listener,
    ):
        single_active.return_value = (True, "ok")
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "primary started", ""),
        ]
        get_listener_pid.side_effect = [1111, 4444]
        wait_for_env_listener.return_value = True

        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "primary"}), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}), \
            mock.patch.object(dashboard, "write_active_binding") as write_active_binding:
            with mock.patch.object(dashboard, "STORE") as store:
                ok, message, old_pid, new_pid, env_id = dashboard.restart_active_openclaw_environment()

        self.assertTrue(ok)
        self.assertEqual(message, "primary started")
        self.assertEqual(old_pid, "1111")
        self.assertEqual(new_pid, "4444")
        self.assertEqual(env_id, "primary")
        self.assertEqual(store.append_runtime_event.call_count, 4)
        write_active_binding.assert_called_once()
        self.assertEqual(write_active_binding.call_args.args[2], "primary")
        self.assertEqual(write_active_binding.call_args.kwargs["switch_state"], "committed")
        self.assertEqual(
            [call.args[0] for call in run_script.call_args_list],
            [
                [str(dashboard.OFFICIAL_MANAGER), "stop"],
                [str(dashboard.DESKTOP_RUNTIME), "stop", "gateway"],
                [str(dashboard.DESKTOP_RUNTIME), "start", "gateway"],
            ],
        )
        wait_for_env_listener.assert_called_once_with("primary")

    @mock.patch("dashboard_backend.record_change")
    @mock.patch("dashboard_backend.PromotionController")
    @mock.patch("dashboard_backend.get_task_registry_payload")
    @mock.patch("dashboard_backend.list_openclaw_environments")
    @mock.patch("dashboard_backend.load_config")
    def test_execute_official_promotion_records_success(
        self,
        load_config,
        list_openclaw_environments,
        get_task_registry_payload,
        promotion_controller,
        record_change,
    ):
        load_config.return_value = {"ACTIVE_OPENCLAW_ENV": "official"}
        list_openclaw_environments.return_value = [{"id": "primary"}, {"id": "official"}]
        get_task_registry_payload.return_value = {"summary": {"blocked": 0}}
        controller = promotion_controller.return_value
        controller.run.return_value = {
            "status": "promoted",
            "preflight": {"primary_git_head": "aaa111", "official_git_head": "bbb222"},
            "backups": {"primary": "snap-a", "official": "snap-b"},
        }

        result = dashboard.execute_official_promotion()

        self.assertEqual(result["status"], "promoted")
        record_change.assert_called_once()
        self.assertIn("官方验证版晋升为当前主用版", record_change.call_args.args[1])

    @mock.patch("dashboard_backend.execute_official_promotion")
    def test_api_promote_environment_returns_preflight_failure_message(self, execute_official_promotion):
        execute_official_promotion.return_value = {
            "status": "failed_preflight",
            "preflight": {
                "checks": [
                    {"name": "official_running", "ok": False, "detail": "官方验证环境未运行"},
                    {"name": "blocked_tasks", "ok": True, "detail": "没有阻塞任务"},
                ]
            },
        }

        with dashboard.app.test_client() as client:
            response = client.post("/api/environments/promote", json={"source_env": "official", "target_env": "primary"})

        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["status"], "failed_preflight")
        self.assertIn("官方验证环境未运行", payload["message"])

    @mock.patch("dashboard_backend.get_listener_pid")
    @mock.patch("dashboard_backend.run_script")
    def test_enforce_single_active_listener_stops_inactive_env(self, run_script, get_listener_pid):
        get_listener_pid.side_effect = [1111, 2222, None]
        with mock.patch.object(
            dashboard,
            "load_config",
            return_value={
                "ACTIVE_OPENCLAW_ENV": "primary",
                "OPENCLAW_HOME": "/tmp/openclaw-main",
                "OPENCLAW_CODE": "/tmp/openclaw-code",
                "GATEWAY_PORT": 18789,
                "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
                "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
                "OPENCLAW_OFFICIAL_PORT": 19001,
            },
        ), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            ok, message = dashboard.enforce_single_active_listener("primary")
        self.assertTrue(ok)
        self.assertIn("listener", message)
        self.assertEqual(run_script.call_args.args[0], [str(dashboard.OFFICIAL_MANAGER), "stop"])

    def test_active_binding_prefers_runtime_store_binding(self):
        cfg = {"ACTIVE_OPENCLAW_ENV": "primary"}
        with mock.patch.object(dashboard, "load_config", return_value=cfg), \
            mock.patch("dashboard_backend.read_active_binding", return_value={"active_env": "primary", "switch_state": "committed", "binding_version": 1, "updated_at": 1, "expected": {}}), \
            mock.patch.object(dashboard.STORE, "load_runtime_value", return_value={"env_id": "official", "switch_state": "committed", "updated_at": 2}):
            binding = dashboard.active_binding(cfg)
        self.assertEqual(binding["active_env"], "official")

    @mock.patch("dashboard_backend.get_listener_pid")
    @mock.patch("dashboard_backend.run_script")
    @mock.patch("dashboard_backend.terminate_listener_pid")
    def test_enforce_single_active_listener_kills_unbound_env_when_stop_leaves_listener_alive(self, terminate_listener_pid, run_script, get_listener_pid):
        get_listener_pid.side_effect = [1111, 2222, 2222]
        terminate_listener_pid.return_value = (True, "killed")
        with mock.patch.object(dashboard, "load_config", return_value={
            "ACTIVE_OPENCLAW_ENV": "primary",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
        }), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            ok, message = dashboard.enforce_single_active_listener("primary")
        self.assertTrue(ok)
        self.assertIn("未绑定环境", message)
        terminate_listener_pid.assert_called_once_with(2222, "官方验证版")

    def test_enforce_single_active_listener_rejects_unbound_target_env(self):
        with mock.patch.object(dashboard, "load_config", return_value={
            "ACTIVE_OPENCLAW_ENV": "primary",
            "OPENCLAW_HOME": "/tmp/openclaw-main",
            "OPENCLAW_CODE": "/tmp/openclaw-code",
            "GATEWAY_PORT": 18789,
            "OPENCLAW_OFFICIAL_STATE": "/tmp/openclaw-official",
            "OPENCLAW_OFFICIAL_CODE": "/tmp/openclaw-official-code",
            "OPENCLAW_OFFICIAL_PORT": 19001,
        }), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            ok, message = dashboard.enforce_single_active_listener("official")
        self.assertFalse(ok)
        self.assertIn("拒绝操作未绑定环境", message)

    def test_manage_official_environment_blocks_start_when_not_active(self):
        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "primary"}), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary"}):
            ok, message = dashboard.manage_official_environment("start")
        self.assertFalse(ok)
        self.assertIn("切换到 official", message)

    def test_build_shared_state_snapshot_includes_context_and_policy(self):
        with mock.patch.object(dashboard, "list_openclaw_environments", return_value=[]), \
            mock.patch.object(dashboard, "get_task_registry_payload", return_value={}), \
            mock.patch.object(dashboard, "get_control_plane_overview", return_value={}), \
            mock.patch.object(dashboard, "get_learning_center_payload", return_value={}), \
            mock.patch.object(dashboard, "get_system_metrics", return_value={}), \
            mock.patch.object(dashboard, "get_recent_anomalies", return_value=[]), \
            mock.patch.object(dashboard, "build_context_lifecycle_readiness", return_value={"ready": False}), \
            mock.patch.object(dashboard, "active_binding", return_value={"active_env": "primary", "switch_state": "committed", "updated_at": 123}), \
            mock.patch.object(dashboard.STORE, "load_runtime_value", side_effect=lambda key, default=None: [{"status": "verified"}] if key == "binding_audit_events" else default), \
            mock.patch.object(dashboard, "env_spec", return_value={"id": "primary"}):
            payload = dashboard.build_shared_state_snapshot({"REFLECTION_INTERVAL_SECONDS": 3600, "LEARNING_PROMOTION_THRESHOLD": 3})
        self.assertIn("context_lifecycle", payload)
        self.assertIn("learning_promotion_policy", payload)
        self.assertIn("binding_audit", payload)
        self.assertEqual(payload["binding_audit"]["active_env"], "primary")


if __name__ == "__main__":
    unittest.main()
