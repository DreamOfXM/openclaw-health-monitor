import unittest
from unittest import mock
from pathlib import Path
import tempfile
import json
import time

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

    @mock.patch("dashboard.check_gateway_health_for_env")
    @mock.patch("dashboard.env_has_control_ui_assets")
    @mock.patch("dashboard.read_git_head")
    @mock.patch("dashboard.get_listener_pid")
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

        envs = dashboard.list_openclaw_environments(config)

        self.assertEqual(len(envs), 2)
        self.assertEqual(envs[0]["id"], "primary")
        self.assertTrue(envs[0]["active"])
        self.assertTrue(envs[0]["running"])
        self.assertTrue(envs[0]["healthy"])
        self.assertTrue(envs[0]["control_ui_ready"])
        self.assertEqual(envs[0]["listener_pid"], 1111)
        self.assertEqual(envs[1]["id"], "official")
        self.assertFalse(envs[1]["active"])
        self.assertFalse(envs[1]["running"])

    def test_detect_environment_inconsistencies_reports_dual_listener(self):
        envs = [
            {"id": "primary", "active": True, "running": True},
            {"id": "official", "active": False, "running": True},
        ]
        issues = dashboard.detect_environment_inconsistencies(envs, "primary")
        codes = {item["code"] for item in issues}
        self.assertIn("dual_listener", codes)
        self.assertIn("official_running_while_primary_active", codes)

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
        self.assertEqual(len(readiness["checks"]), 4)

    @mock.patch("dashboard.check_gateway_health_for_env")
    @mock.patch("dashboard.env_has_control_ui_assets")
    @mock.patch("dashboard.read_git_target_head")
    @mock.patch("dashboard.read_git_head")
    @mock.patch("dashboard.get_listener_pid")
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

        envs = dashboard.list_openclaw_environments(config)
        primary = next(item for item in envs if item["id"] == "primary")
        official = next(item for item in envs if item["id"] == "official")

        self.assertTrue(primary["active"])
        self.assertTrue(primary["running"])
        self.assertEqual(primary["dashboard_open_link"], "/open-dashboard/primary")
        self.assertFalse(official["active"])
        self.assertTrue(official["running"])
        self.assertEqual(official["dashboard_open_link"], "")

    @mock.patch("dashboard.check_gateway_health_for_env")
    @mock.patch("dashboard.env_has_control_ui_assets")
    @mock.patch("dashboard.read_git_target_head")
    @mock.patch("dashboard.read_git_head")
    @mock.patch("dashboard.get_listener_pid")
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
            self.assertEqual(payload["current"]["control"]["claim_level"], "execution_verified")
            self.assertEqual(payload["current"]["control"]["next_actor"], "dev")
            self.assertEqual(payload["control_queue"][0]["action_type"], "await_dev_completion")
            self.assertEqual(payload["session_resolution"]["active_task_id"], "task-1")
            self.assertEqual(len(payload["current"]["timeline"]), 2)

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
            with mock.patch.object(dashboard, "STORE", store):
                payload = dashboard.get_learning_center_payload(limit=10)
            self.assertEqual(payload["summary"]["pending"], 1)
            self.assertEqual(payload["reflections"][0]["summary"]["promoted"], 1)
            self.assertEqual(payload["suggestions"][0]["title"], "缺少回执")

    def test_api_shared_state_exposes_normalized_objects(self):
        with mock.patch.object(dashboard, "build_shared_state_snapshot", return_value={"runtime_health": {}, "learning_backlog": {}}):
            with dashboard.app.test_client() as client:
                response = client.get("/api/shared-state")
        payload = response.get_json()
        self.assertIn("runtime_health", payload)
        self.assertIn("learning_backlog", payload)

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

    @mock.patch("dashboard.wait_for_env_listener")
    @mock.patch("dashboard.run_script")
    @mock.patch("dashboard.save_config")
    def test_switch_openclaw_environment_rolls_back_when_primary_does_not_start(
        self,
        save_config,
        run_script,
        wait_for_env_listener,
    ):
        save_config.return_value = True
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "started", ""),
        ]
        wait_for_env_listener.return_value = False

        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "official"}), \
            mock.patch.object(dashboard, "STORE") as store:
            ok, message = dashboard.switch_openclaw_environment("primary")

        self.assertFalse(ok)
        self.assertIn("Gateway 未成功启动", message)
        self.assertEqual(save_config.call_args_list[0].args, ("ACTIVE_OPENCLAW_ENV", "primary"))
        self.assertEqual(save_config.call_args_list[1].args, ("ACTIVE_OPENCLAW_ENV", "official"))
        self.assertEqual(store.save_runtime_value.call_args_list[0].args[1]["env_id"], "primary")
        self.assertEqual(store.save_runtime_value.call_args_list[1].args[1]["env_id"], "official")

    @mock.patch("dashboard.wait_for_env_listener")
    @mock.patch("dashboard.run_script")
    @mock.patch("dashboard.save_config")
    def test_switch_openclaw_environment_succeeds_when_primary_listener_is_ready(
        self,
        save_config,
        run_script,
        wait_for_env_listener,
    ):
        save_config.return_value = True
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "started", ""),
        ]
        wait_for_env_listener.return_value = True

        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "official"}), \
            mock.patch.object(dashboard, "STORE") as store:
            ok, message = dashboard.switch_openclaw_environment("primary")

        self.assertTrue(ok)
        self.assertEqual(message, "started")
        self.assertEqual(save_config.call_count, 1)
        self.assertEqual(store.save_runtime_value.call_args.args[1]["env_id"], "primary")

    @mock.patch("dashboard.wait_for_env_listener")
    @mock.patch("dashboard.run_script")
    @mock.patch("dashboard.get_listener_pid")
    def test_restart_active_openclaw_environment_restarts_official_only(
        self,
        get_listener_pid,
        run_script,
        wait_for_env_listener,
    ):
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "official started", ""),
        ]
        get_listener_pid.side_effect = [2222, 3333]
        wait_for_env_listener.return_value = True

        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "official"}):
            ok, message, old_pid, new_pid, env_id = dashboard.restart_active_openclaw_environment()

        self.assertTrue(ok)
        self.assertEqual(message, "official started")
        self.assertEqual(old_pid, "2222")
        self.assertEqual(new_pid, "3333")
        self.assertEqual(env_id, "official")
        self.assertEqual(
            [call.args[0] for call in run_script.call_args_list],
            [
                [str(dashboard.OFFICIAL_MANAGER), "stop"],
                [str(dashboard.DESKTOP_RUNTIME), "stop", "gateway"],
                [str(dashboard.OFFICIAL_MANAGER), "start"],
            ],
        )
        wait_for_env_listener.assert_called_once_with("official")

    @mock.patch("dashboard.wait_for_env_listener")
    @mock.patch("dashboard.run_script")
    @mock.patch("dashboard.get_listener_pid")
    def test_restart_active_openclaw_environment_restarts_primary_only(
        self,
        get_listener_pid,
        run_script,
        wait_for_env_listener,
    ):
        run_script.side_effect = [
            (0, "", ""),
            (0, "", ""),
            (0, "primary started", ""),
        ]
        get_listener_pid.side_effect = [1111, 4444]
        wait_for_env_listener.return_value = True

        with mock.patch.object(dashboard, "load_config", return_value={"ACTIVE_OPENCLAW_ENV": "primary"}):
            ok, message, old_pid, new_pid, env_id = dashboard.restart_active_openclaw_environment()

        self.assertTrue(ok)
        self.assertEqual(message, "primary started")
        self.assertEqual(old_pid, "1111")
        self.assertEqual(new_pid, "4444")
        self.assertEqual(env_id, "primary")
        self.assertEqual(
            [call.args[0] for call in run_script.call_args_list],
            [
                [str(dashboard.OFFICIAL_MANAGER), "stop"],
                [str(dashboard.DESKTOP_RUNTIME), "stop", "gateway"],
                [str(dashboard.DESKTOP_RUNTIME), "start", "gateway"],
            ],
        )
        wait_for_env_listener.assert_called_once_with("primary")

    @mock.patch("dashboard.record_change")
    @mock.patch("dashboard.PromotionController")
    @mock.patch("dashboard.get_task_registry_payload")
    @mock.patch("dashboard.list_openclaw_environments")
    @mock.patch("dashboard.load_config")
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

    @mock.patch("dashboard.execute_official_promotion")
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


if __name__ == "__main__":
    unittest.main()
