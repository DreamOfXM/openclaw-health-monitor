"""
数据收集器测试
"""
import unittest
import time
import sys
import os
import pathlib
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.data_collector import DataCollector, DataCache, get_collector


class TestDataCache(unittest.TestCase):
    """测试数据缓存"""
    
    def setUp(self):
        self.cache = DataCache()
    
    def test_set_and_get(self):
        """测试设置和获取缓存"""
        self.cache.set('test_key', {'data': 'value'}, ttl=60)
        result = self.cache.get('test_key')
        self.assertEqual(result, {'data': 'value'})
    
    def test_cache_expiration(self):
        """测试缓存过期"""
        self.cache.set('expiring_key', {'data': 'value'}, ttl=0)
        time.sleep(0.1)  # 等待过期
        result = self.cache.get('expiring_key')
        self.assertIsNone(result)
    
    def test_cache_invalidate(self):
        """测试使缓存失效"""
        self.cache.set('key1', 'value1', ttl=60)
        self.cache.invalidate('key1')
        result = self.cache.get('key1')
        self.assertIsNone(result)
    
    def test_cache_invalidate_all(self):
        """测试使所有缓存失效"""
        self.cache.set('key1', 'value1', ttl=60)
        self.cache.set('key2', 'value2', ttl=60)
        self.cache.invalidate_all()
        self.assertIsNone(self.cache.get('key1'))
        self.assertIsNone(self.cache.get('key2'))
    
    def test_get_nonexistent_key(self):
        """测试获取不存在的key"""
        result = self.cache.get('nonexistent')
        self.assertIsNone(result)


class TestDataCollector(unittest.TestCase):
    """测试数据收集器"""
    
    def setUp(self):
        self.collector = DataCollector()
    
    def test_get_metrics(self):
        """测试获取指标"""
        metrics = self.collector.get_metrics()
        self.assertIn('cpu_percent', metrics)
        self.assertIn('memory_percent', metrics)
        self.assertIn('timestamp', metrics)
    
    def test_get_metrics_caching(self):
        """测试指标缓存"""
        # 第一次获取
        metrics1 = self.collector.get_metrics()
        # 第二次获取（应该从缓存）
        metrics2 = self.collector.get_metrics()
        # 强制刷新
        metrics3 = self.collector.get_metrics(force_refresh=True)
        
        # 缓存的数据应该相同
        self.assertEqual(metrics1['timestamp'], metrics2['timestamp'])
        # 强制刷新后可能不同
        self.assertIsNotNone(metrics3)

    @mock.patch('services.data_collector._legacy_dashboard')
    def test_get_metrics_falls_back_when_runtime_health_is_zero(
        self,
        mock_legacy_dashboard,
    ):
        fake_legacy = mock.Mock()
        fake_legacy.get_system_metrics.return_value = {"cpu": 0, "mem_used": 0, "mem_total": 0}
        fake_legacy.get_gateway_process_for_env.return_value = {}
        fake_legacy.get_guardian_process_info.return_value = {}
        fake_legacy.check_gateway_health_for_env.return_value = True
        mock_legacy_dashboard.return_value = fake_legacy
        fake_psutil = types.SimpleNamespace(
            cpu_percent=lambda interval=None: 12.5,
            virtual_memory=lambda: types.SimpleNamespace(
                percent=48.0,
                used=8 * 1024 ** 3,
                total=16 * 1024 ** 3,
            ),
        )
        with mock.patch.object(self.collector, "_shared_state_fresh", return_value={
            "generated_at": int(time.time()),
            "gateway_healthy": True,
            "metrics": {"cpu": 0.0, "mem_used": 0, "mem_total": 32},
        }), mock.patch.object(self.collector, "_load_runtime_context", return_value={
            "legacy": fake_legacy,
            "selected_env": {"id": "primary"},
        }), mock.patch.object(self.collector, "_shared_state", return_value={"summary": {}}), \
            mock.patch.dict(sys.modules, {"psutil": fake_psutil}):
            metrics = self.collector._fetch_metrics_data()

        self.assertEqual(metrics["cpu_percent"], 12.5)
        self.assertEqual(metrics["memory_percent"], 48.0)
    
    def test_get_environment(self):
        """测试获取环境数据"""
        env = self.collector.get_environment()
        self.assertIn('gateway_healthy', env)
        self.assertIn('active_environment', env)
        self.assertIn('environments', env)
        self.assertIn('context_readiness', env)
        self.assertIn('timestamp', env)

    def test_runtime_context_prefers_active_binding(self):
        """测试运行时上下文优先读取 committed binding，而不是旧配置里的 ACTIVE_OPENCLAW_ENV。"""
        fake_legacy = mock.Mock()
        fake_legacy.load_config.return_value = {"ACTIVE_OPENCLAW_ENV": "primary"}
        fake_legacy.STORE.load_runtime_value.side_effect = lambda key, default=None: {
            "env_id": "primary",
            "switch_state": "committed",
            "updated_at": 123,
        } if key == "active_openclaw_env" else default
        fake_legacy.env_spec.side_effect = lambda env_id, _cfg=None: {"id": env_id}
        fake_legacy.get_task_registry_payload.return_value = {}

        with mock.patch("services.data_collector._legacy_dashboard", return_value=fake_legacy):
            context = self.collector._load_runtime_context()

        self.assertEqual(context["active_env"], "primary")
        self.assertEqual(context["selected_env"]["id"], "primary")
        self.assertEqual(context["binding"]["source"], "runtime_db")

    def test_environment_binding_audit_uses_runtime_binding(self):
        """测试环境绑定展示优先使用 DB/runtime binding，不读取 active-binding.json 作为当前真相。"""
        fake_legacy = mock.Mock()
        fake_legacy.load_config.return_value = {"ACTIVE_OPENCLAW_ENV": "primary"}
        fake_legacy.STORE.load_runtime_value.side_effect = lambda key, default=None: {
            "active_openclaw_env": {
                "env_id": "primary",
                "switch_state": "committed",
                "updated_at": 123,
            },
            "binding_audit_events": [{"source": "db"}],
            "channel_readiness:primary": {"status": "ready", "summary": "ok"},
        }.get(key, default)
        fake_legacy.env_spec.side_effect = lambda env_id, _cfg=None: {"id": env_id}
        fake_legacy.get_task_registry_payload.return_value = {}
        fake_legacy.get_gateway_process_for_env.return_value = {"pid": 1234}
        fake_legacy.env_has_control_ui_assets.return_value = True
        fake_legacy.env_dashboard_url.return_value = "http://127.0.0.1:18791/"
        fake_legacy.env_open_link.return_value = "http://127.0.0.1:18791/"
        fake_legacy.check_gateway_health_for_env.return_value = True

        with mock.patch("services.data_collector._legacy_dashboard", return_value=fake_legacy), \
            mock.patch.object(self.collector, "_shared_state_fresh", return_value={"env_id": "primary", "gateway_running": True, "gateway_healthy": True}), \
            mock.patch.object(
                self.collector,
                "_shared_state",
                side_effect=lambda name, default=None: {
                    "bootstrap-status.json": {},
                    "context-lifecycle-baseline.json": {},
                    "watcher-summary.json": {},
                    "restart-runtime-status.json": {},
                    "openclaw-version.json": {},
                    "openclaw-recovery-profile.json": {},
                    "watchdog-recovery-status.json": {},
                    "watchdog-recovery-hints.json": [],
                }.get(name, default),
            ):
            env = self.collector.get_environment(force_refresh=True)

        self.assertEqual(env["active_environment"], "primary")
        self.assertEqual(env["binding_audit"]["active_env"], "primary")
        self.assertEqual(env["binding_audit"]["source"], "runtime_db")
        self.assertEqual(env["binding_audit"]["recent_events"], [{"source": "db"}])
        self.assertEqual([item["id"] for item in env["environments"]], ["primary"])
        fake_legacy.list_openclaw_environments.assert_not_called()

    def test_get_environment_exposes_binding_audit(self):
        fake_legacy = mock.Mock()
        fake_legacy.get_gateway_process_for_env.return_value = {}
        fake_legacy.env_has_control_ui_assets.return_value = False
        fake_legacy.env_dashboard_url.return_value = ""
        fake_legacy.env_open_link.return_value = ""
        context = {
            "legacy": fake_legacy,
            "config": {},
            "active_env": "primary",
            "binding": {"active_env": "primary", "switch_state": "committed", "updated_at": 1},
            "selected_env": {"id": "primary", "code": "/tmp/code", "home": "/tmp/home", "port": 18789},
            "task_registry": {},
        }
        with mock.patch.object(self.collector, "_load_runtime_context", return_value=context), \
            mock.patch.object(self.collector, "_shared_state_fresh", return_value={"env_id": "primary", "gateway_running": False, "gateway_healthy": False}), \
            mock.patch.object(
                self.collector,
                "_shared_state",
                side_effect=lambda name, default=None: {
                    "bootstrap-status.json": {},
                    "context-lifecycle-baseline.json": {},
                    "watcher-summary.json": {},
                    "restart-runtime-status.json": {},
                    "openclaw-version.json": {},
                    "openclaw-recovery-profile.json": {},
                    "watchdog-recovery-status.json": {},
                    "watchdog-recovery-hints.json": [],
                }.get(name, default),
            ):
            env = self.collector._fetch_environment_data()
        self.assertEqual(env["binding_audit"]["active_env"], "primary")

    def test_get_environment_uses_lightweight_sources_without_environment_probe(self):
        fake_legacy = mock.Mock()
        fake_legacy.get_gateway_process_for_env.return_value = {"pid": 4321}
        fake_legacy.env_has_control_ui_assets.return_value = True
        fake_legacy.env_dashboard_url.return_value = "http://127.0.0.1:18791/"
        fake_legacy.env_open_link.return_value = "http://127.0.0.1:18791/"
        context = {
            "legacy": fake_legacy,
            "config": {},
            "active_env": "primary",
            "binding": {"active_env": "primary", "switch_state": "committed", "updated_at": 1},
            "selected_env": {"id": "primary", "name": "OpenClaw", "description": "当前唯一运行环境", "code": "/tmp/code", "home": "/tmp/home", "port": 18789},
        }
        with mock.patch.object(self.collector, "_load_runtime_context", return_value=context), \
            mock.patch.object(self.collector, "_shared_state_fresh", return_value={"env_id": "primary", "gateway_running": True, "gateway_healthy": True}), \
            mock.patch.object(
                self.collector,
                "_shared_state",
                side_effect=lambda name, default=None: {
                    "bootstrap-status.json": {"env_id": "primary", "context_readiness": {"status": "ready"}, "config_merge": {"applied": []}},
                    "watcher-summary.json": {},
                    "restart-runtime-status.json": {},
                    "openclaw-version.json": {},
                    "openclaw-recovery-profile.json": {},
                    "watchdog-recovery-status.json": {},
                    "watchdog-recovery-hints.json": [],
                }.get(name, default),
            ):
            env = self.collector._fetch_environment_data()

        self.assertTrue(env["gateway_healthy"])
        self.assertEqual(env["active"]["pid"], 4321)
        fake_legacy.list_openclaw_environments.assert_not_called()

    def test_get_environment_probes_live_health_when_runtime_snapshot_is_stale_false(self):
        fake_legacy = mock.Mock()
        fake_legacy.get_gateway_process_for_env.return_value = {"pid": 4321}
        fake_legacy.env_has_control_ui_assets.return_value = True
        fake_legacy.env_dashboard_url.return_value = "http://127.0.0.1:18791/"
        fake_legacy.env_open_link.return_value = "http://127.0.0.1:18791/"
        fake_legacy.check_gateway_health_for_env.return_value = True
        context = {
            "legacy": fake_legacy,
            "config": {},
            "active_env": "primary",
            "binding": {"active_env": "primary", "switch_state": "committed", "updated_at": 1},
            "selected_env": {"id": "primary", "name": "OpenClaw", "description": "当前唯一运行环境", "code": "/tmp/code", "home": "/tmp/home", "port": 18789},
        }
        with mock.patch.object(self.collector, "_load_runtime_context", return_value=context), \
            mock.patch.object(self.collector, "_shared_state_fresh", return_value={"env_id": "primary", "gateway_running": True, "gateway_healthy": False}), \
            mock.patch.object(
                self.collector,
                "_shared_state",
                side_effect=lambda name, default=None: {
                    "bootstrap-status.json": {"env_id": "primary", "context_readiness": {"status": "ready"}, "config_merge": {"applied": []}},
                    "watcher-summary.json": {},
                    "restart-runtime-status.json": {},
                    "openclaw-version.json": {},
                    "openclaw-recovery-profile.json": {},
                    "watchdog-recovery-status.json": {},
                    "watchdog-recovery-hints.json": [],
                }.get(name, default),
            ):
            env = self.collector._fetch_environment_data()

        self.assertTrue(env["gateway_healthy"])
        self.assertTrue(env["active"]["healthy"])
        fake_legacy.check_gateway_health_for_env.assert_called_once()

    def test_restart_environment_delegates_to_legacy_restart(self):
        fake_legacy = mock.Mock()
        fake_legacy.restart_active_openclaw_environment.return_value = (True, "重启成功", "123", "456", "primary")
        with mock.patch("services.data_collector._legacy_dashboard", return_value=fake_legacy):
            result = self.collector.restart_environment()
        self.assertTrue(result["success"])
        self.assertEqual(result["environment"], "primary")

    def test_get_tasks_prefers_shared_state_snapshot(self):
        payload = {
            "summary": {"blocked": 1, "total": 2, "running": 1},
            "current": {"task_id": "task-1", "status": "running", "question": "主任务"},
            "tasks": [
                {"task_id": "task-1", "status": "running", "question": "主任务"},
                {"task_id": "task-2", "status": "background", "question": "后台任务"},
            ],
            "control_queue": [],
            "session_resolution": {"active_task_id": "task-1"},
        }
        with mock.patch.object(self.collector, "_shared_state", return_value=payload), \
            mock.patch("services.data_collector._read_json_file", return_value={}), \
            mock.patch("services.data_collector._legacy_dashboard") as legacy:
            data = self.collector._fetch_task_data()
        legacy.return_value.get_task_registry_payload.assert_not_called()
        self.assertEqual(data["blocked_count"], 1)
        self.assertEqual(data["total_count"], 2)
        self.assertEqual(data["current"]["task_id"], "task-1")
    
    def test_get_tasks(self):
        """测试获取任务数据"""
        tasks = self.collector.get_tasks()
        self.assertIn('blocked_count', tasks)
        self.assertIn('total_count', tasks)
        self.assertIn('summary', tasks)
        self.assertIn('timestamp', tasks)
    
    def test_get_agents(self):
        """测试获取代理数据"""
        agents = self.collector.get_agents()
        self.assertIn('active_count', agents)
        self.assertIn('agents', agents)
        self.assertIn('timestamp', agents)
        self.assertIn('active_agent_id', agents)

    def test_fetch_agents_returns_all_agents_and_marks_log_active(self):
        fake_legacy = mock.Mock()
        fake_legacy.load_agent_catalog.return_value = {
            "main": {"name": "小忆", "emoji": "🌸"},
            "dev": {"name": "开发", "emoji": "🛠️"},
        }
        context = {
            "legacy": fake_legacy,
            "config": {"AGENT_ACTIVITY_ACTIVE_WINDOW_SECONDS": 900},
            "selected_env": {"id": "primary", "home": "/tmp/fake-openclaw"},
        }
        with mock.patch.object(self.collector, "_load_runtime_context", return_value=context), \
            mock.patch.object(self.collector, "_load_agent_sessions", return_value={
                "main": {
                    "agent_id": "main",
                    "display_name": "小忆",
                    "emoji": "🌸",
                    "updated_at": 100,
                    "updated_label": "03-14 00:00:00",
                    "status_code": "idle",
                    "state_label": "待机",
                    "state_reason": "最近没有新的代理动作",
                    "detail": "暂无最近会话",
                    "task_hint": "",
                    "task_title": "",
                    "sessions": 0,
                    "recent_sessions": [],
                    "is_active": False,
                    "activity_source": "session",
                    "activity_excerpt": "",
                },
                "dev": {
                    "agent_id": "dev",
                    "display_name": "开发",
                    "emoji": "🛠️",
                    "updated_at": int(time.time()) - 60,
                    "updated_label": "03-14 00:01:00",
                    "status_code": "completed",
                    "state_label": "命令完成",
                    "state_reason": "当前阶段已完成",
                    "detail": "最近会话有更新",
                    "task_hint": "修复同步链路",
                    "task_title": "修复同步链路",
                    "sessions": 2,
                    "recent_sessions": [{"session_file": "a.jsonl", "updated_at": int(time.time()) - 60, "updated_label": "03-14 00:01:00", "status_code": "completed", "state_label": "命令完成", "state_reason": "当前阶段已完成", "detail": "最近会话有更新", "task_hint": "修复同步链路", "task_title": "修复同步链路", "recent_context": []}],
                    "is_active": False,
                    "activity_source": "session",
                    "activity_excerpt": "最近会话有更新",
                },
            }), \
            mock.patch.object(self.collector, "_load_agent_log_activity", return_value={
                "main": {
                    "updated_at": int(time.time()) - 30,
                    "updated_label": "03-14 00:01:30",
                    "status_code": "processing",
                    "state_label": "正在处理",
                    "state_reason": "最近日志显示代理正在执行或派发任务",
                    "detail": "dispatching to agent",
                    "activity_source": "gateway_log",
                    "activity_excerpt": "dispatching to agent",
                }
            }):
            data = self.collector._fetch_agents_data()

        self.assertEqual(len(data["agents"]), 2)
        self.assertEqual(data["active_agent_id"], "main")
        self.assertEqual(data["agents"][0]["id"], "main")
        self.assertTrue(data["agents"][0]["is_active"])
        self.assertTrue(data["agents"][0]["is_processing"])
        self.assertEqual(data["agents"][0]["activity_source"], "gateway_log")
        self.assertEqual(data["agents"][1]["id"], "dev")
        self.assertEqual(data["agents"][1]["sessions"], 2)
        self.assertFalse(data["agents"][1]["is_processing"])

    def test_summarize_session_entries_prefers_human_task_title_and_explainable_state(self):
        entries = [
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "[粘贴用户原始需求]"}],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "sessions_spawn",
                            "arguments": {"agentId": "dev", "label": "实现 A 股模拟交易闭环"},
                        }
                    ],
                },
            },
        ]
        summary = self.collector._summarize_session_entries(entries, pathlib.Path("/tmp/pm.jsonl"))
        self.assertEqual(summary["status_code"], "waiting_downstream")
        self.assertIn("等待下游", summary["state_label"])
        self.assertIn("等待下游回执", summary["state_reason"])
        self.assertIn("实现 A 股模拟交易闭环", summary["detail"])
        self.assertFalse(summary["task_title"].startswith("[粘贴"))

    def test_summarize_session_entries_marks_exec_as_processing(self):
        entries = [
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "任务标题：修复同步链路"},
                        {"type": "toolCall", "name": "exec", "arguments": {"cmd": "pytest"}},
                    ],
                },
            }
        ]
        summary = self.collector._summarize_session_entries(entries, pathlib.Path("/tmp/dev.jsonl"))
        self.assertEqual(summary["status_code"], "processing")
        self.assertEqual(summary["state_label"], "正在处理")
        self.assertIn("正在执行", summary["detail"])
        self.assertEqual(summary["task_title"], "修复同步链路")
    
    def test_get_learnings(self):
        """测试获取学习数据"""
        learnings = self.collector.get_learnings()
        self.assertIn('is_fresh', learnings)
        self.assertIn('items', learnings)
        self.assertIn('reflections', learnings)
        self.assertIn('timestamp', learnings)

    @mock.patch('services.data_collector._legacy_dashboard')
    def test_get_learnings_hides_internal_control_promotions_from_user_view(self, mock_legacy_dashboard):
        fake_legacy = mock.Mock()
        fake_legacy.get_learning_center_payload.return_value = {
            "summary": {"pending": 0, "reviewed": 0, "promoted": 2, "total": 2},
            "source_mode": "legacy_store",
            "suggestions": [],
            "learnings": [
                {
                    "id": 1,
                    "category": "control_plane",
                    "title": "任务因缺少结构化回执而阻塞",
                    "detail": "task=abc control=received_only action=blocked blocked_reason=missing_pipeline_receipt",
                    "status": "promoted",
                    "occurrences": 10,
                    "updated_at": int(time.time()),
                    "promoted_target": "contract",
                },
                {
                    "id": 2,
                    "category": "workflow",
                    "title": "测试完成后主脑必须统一收口",
                    "detail": "系统学会在测试完成后由主脑统一给出最终结论。",
                    "status": "promoted",
                    "occurrences": 3,
                    "updated_at": int(time.time()),
                    "promoted_target": "rule",
                },
            ],
            "reflections": [],
        }
        mock_legacy_dashboard.return_value = fake_legacy

        data = self.collector._fetch_learning_data()

        self.assertEqual(len(data["promoted"]), 1)
        self.assertEqual(data["promoted"][0]["title"], "测试完成后主脑必须统一收口")
        self.assertEqual(data["internal_summary"]["promoted_count"], 1)
        self.assertEqual(len(data["internal_promoted"]), 1)

    def test_get_snapshots(self):
        """测试获取快照数据"""
        snapshots = self.collector.get_snapshots(force_refresh=True)
        self.assertIn('count', snapshots)
        self.assertIn('snapshots', snapshots)

    def test_fetch_environment_data_includes_version_and_recovery_profile(self):
        with mock.patch.object(self.collector, '_load_runtime_context') as context_mock, \
            mock.patch.object(self.collector, '_shared_state_fresh', return_value={"env_id": "primary", "gateway_running": True, "gateway_healthy": True}), \
            mock.patch.object(
                self.collector,
                '_shared_state',
                side_effect=lambda name, default=None: {
                    "bootstrap-status.json": {},
                    "context-lifecycle-baseline.json": {},
                    "watcher-summary.json": {},
                    "restart-runtime-status.json": {},
                    "openclaw-version.json": {"describe": "v2026.3.11", "branch": "main", "short_commit": "abc123", "upstream_ahead": 2, "upstream_behind": 138},
                    "openclaw-recovery-profile.json": {"known_good": {"describe": "v2026.3.11"}, "rollback_hint": {"target_describe": "v2026.3.11"}},
                    "watchdog-recovery-status.json": {},
                    "watchdog-recovery-hints.json": [],
                }.get(name, default),
            ):
            legacy = mock.Mock()
            legacy.get_gateway_process_for_env.return_value = {"pid": 1234}
            legacy.check_gateway_health_for_env.return_value = True
            legacy.env_has_control_ui_assets.return_value = True
            legacy.env_dashboard_url.return_value = "http://localhost:18789"
            legacy.env_open_link.return_value = "http://localhost:18789"
            context_mock.return_value = {
                "legacy": legacy,
                "active_env": "primary",
                "selected_env": {"id": "primary", "name": "OpenClaw", "port": 18789, "code": "/tmp/code", "home": "/tmp/home"},
                "binding": {},
            }

            data = self.collector._fetch_environment_data()

        self.assertEqual(data["version_info"]["describe"], "v2026.3.11")
        self.assertEqual(data["recovery_profile"]["known_good"]["describe"], "v2026.3.11")
    
    def test_get_events(self):
        """测试获取事件数据"""
        events = self.collector.get_events(limit=10)
        self.assertIsInstance(events, list)

    def test_emergency_recover_returns_known_good_guidance_when_no_snapshot_exists(self):
        fake_legacy = mock.Mock()
        fake_legacy.load_config.return_value = {"ENABLE_SNAPSHOT_RECOVERY": True}
        fake_legacy.SNAPSHOTS.restore_latest_snapshot.return_value = None
        fake_legacy.load_versions.return_value = {"known_good": {"describe": "v2026.3.11", "commit": "abc"}}
        with mock.patch('services.data_collector._legacy_dashboard', return_value=fake_legacy):
            result = self.collector.emergency_recover()

        self.assertFalse(result["success"])
        self.assertEqual(result["rollback_guidance"]["target"], "v2026.3.11")
    
    def test_get_health_score_data(self):
        """测试获取健康评分数据"""
        data = self.collector.get_health_score_data()
        self.assertIn('environment', data)
        self.assertIn('metrics', data)
        self.assertIn('tasks', data)
        self.assertIn('learning', data)
        self.assertIn('errors', data)

    def test_get_health_score_data_flags_main_closure_purity_gate(self):
        with mock.patch.object(self.collector, "_fetch_environment_data", return_value={"gateway_healthy": True}), \
            mock.patch.object(
                self.collector,
                "_shared_state",
                side_effect=[
                    {"summary": {"blocked": 0}},
                    {"reflection_freshness": 0},
                    {"actions": {}, "tasks": {}},
                    {"ok": False, "reasons": ["shadow_state_detected"]},
                ],
            ), \
            mock.patch.object(self.collector, "get_metrics", return_value={"cpu_percent": 1.0}):
            data = self.collector._fetch_health_score_data()

        self.assertIn("main_closure_purity_gate_failed", data["errors"]["categories"])
    
    def test_invalidate_cache(self):
        """测试使缓存失效"""
        # 先缓存一些数据
        self.collector.get_metrics()
        self.collector.get_environment()
        
        # 使单个缓存失效
        self.collector.invalidate_cache('metrics')
        
        # 使所有缓存失效
        self.collector.invalidate_cache()

    def test_get_tasks_normalizes_current_task_facts_shape(self):
        current_facts = {
            "current_task": {
                "task_id": "task-1",
                "question": "集成主链路",
                "status": "running",
                "current_stage": "implementation:started",
                "latest_receipt": {"agent": "dev", "phase": "implementation", "action": "started"},
                "approved_summary": "开发阶段已启动，存在结构化执行证据。",
                "control_state": "dev_running",
                "next_action": "await_dev_receipt",
                "next_actor": "dev",
                "claim_level": "phase_verified",
                "evidence_level": "strong",
                "missing_receipts": ["dev:completed", "test:started", "test:completed"],
            },
            "current_root_task": {
                "root_task_id": "legacy-root:task-1",
                "user_goal_summary": "集成主链路",
                "workflow_state": "delivery_pending",
                "foreground": True,
                "finalization_state": "finalized",
                "final_status": "completed",
                "delivery_state": "delivery_confirmed",
                "delivery_confirmation_level": "delivery_confirmed",
                "open_followup_count": 1,
                "followup_types": ["delivery_retry"],
            },
            "current_workflow_run": {
                "workflow_run_id": "legacy-run:task-1",
                "current_state": "delivery_pending",
                "state_reason": "finalizer_finalized",
            },
            "current_finalizer": {
                "finalization_id": "legacy-finalizer:task-1",
                "decision_state": "finalized",
                "final_status": "completed",
                "delivery_state": "delivery_confirmed",
                "user_visible_summary": "已完成并回传给主人",
            },
            "current_delivery_attempt": {
                "delivery_attempt_id": "legacy-delivery:task-1:1",
                "current_state": "delivery_confirmed",
                "confirmation_level": "delivery_confirmed",
                "channel": "feishu_dm",
                "target": "agent:main:feishu:direct:user-1",
            },
            "current_followups": [
                {
                    "followup_id": "fu-1",
                    "followup_type": "delivery_retry",
                    "trigger_reason": "delivery_failed",
                    "current_state": "open",
                    "suggested_action": "retry_delivery",
                }
            ],
        }
        with mock.patch.object(self.collector, "_shared_state", return_value={"summary": {}}), \
            mock.patch("services.data_collector._read_json_file", return_value=current_facts), \
            mock.patch.object(self.collector, "_load_runtime_context", return_value={"legacy": mock.Mock(), "selected_env": {"id": "primary"}}):
            payload = self.collector._fetch_task_data()

        self.assertEqual(payload["current"]["task_id"], "task-1")
        self.assertEqual(payload["current"]["latest_receipt"]["agent"], "dev")
        self.assertEqual(payload["current"]["control"]["control_state"], "dev_running")
        self.assertEqual(payload["current"]["control"]["next_actor"], "dev")
        self.assertEqual(payload["current"]["root_task"]["root_task_id"], "legacy-root:task-1")
        self.assertEqual(payload["current"]["root_task"]["workflow_state"], "delivery_pending")
        self.assertEqual(payload["current"]["root_task"]["delivery_state"], "delivery_confirmed")
        self.assertEqual(payload["current"]["current_workflow_run"]["workflow_run_id"], "legacy-run:task-1")
        self.assertEqual(payload["current"]["current_finalizer"]["decision_state"], "finalized")
        self.assertEqual(payload["current"]["current_delivery_attempt"]["current_state"], "delivery_confirmed")
        self.assertEqual(payload["current"]["followup_count"], 1)
        self.assertEqual(payload["current"]["current_followups"][0]["followup_type"], "delivery_retry")
        self.assertEqual(payload["current"]["truth_level"], "core_projection")

    def test_normalize_task_prefers_core_workflow_terminal_truth(self):
        task = {
            "task_id": "task-delivered",
            "status": "running",
            "question": "交付完成的任务",
            "current_workflow_run": {"workflow_run_id": "wr-delivered", "current_state": "delivered"},
            "current_root_task": {"root_task_id": "rt-delivered", "workflow_state": "delivered"},
        }

        normalized = self.collector._normalize_task(task)

        self.assertEqual(normalized["status"], "completed")
        self.assertEqual(normalized["current_stage"], "delivered")
        self.assertEqual(normalized["truth_level"], "core_projection")


class TestGlobalCollector(unittest.TestCase):
    """测试全局收集器"""
    
    def test_get_collector_singleton(self):
        """测试单例模式"""
        collector1 = get_collector()
        collector2 = get_collector()
        self.assertIs(collector1, collector2)


class TestRefreshIntervals(unittest.TestCase):
    """测试刷新间隔配置"""
    
    def test_refresh_intervals(self):
        """测试刷新间隔配置存在"""
        collector = DataCollector()
        self.assertIn('health_score', collector.REFRESH_INTERVALS)
        self.assertIn('metrics', collector.REFRESH_INTERVALS)
        self.assertIn('events', collector.REFRESH_INTERVALS)
        self.assertIn('environment', collector.REFRESH_INTERVALS)
        self.assertEqual(collector.REFRESH_INTERVALS['metrics'], 5)
        self.assertEqual(collector.REFRESH_INTERVALS['health_score'], 5)
        self.assertEqual(collector.REFRESH_INTERVALS['events'], 5)
        self.assertEqual(collector.REFRESH_INTERVALS['environment'], 10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
