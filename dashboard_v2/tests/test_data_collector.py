"""
数据收集器测试
"""
import unittest
import time
import sys
import os
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
        fake_legacy.load_config.return_value = {"ACTIVE_OPENCLAW_ENV": "official"}
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
        fake_legacy.load_config.return_value = {"ACTIVE_OPENCLAW_ENV": "official"}
        fake_legacy.STORE.load_runtime_value.side_effect = lambda key, default=None: {
            "active_openclaw_env": {
                "env_id": "primary",
                "switch_state": "committed",
                "updated_at": 123,
            },
            "binding_audit_events": [{"source": "db"}],
        }.get(key, default)
        fake_legacy.env_spec.side_effect = lambda env_id, _cfg=None: {"id": env_id}
        fake_legacy.get_task_registry_payload.return_value = {}
        fake_legacy.list_openclaw_environments.return_value = [
            {"id": "primary", "running": True, "healthy": True},
            {"id": "official", "running": False, "healthy": False},
        ]
        fake_legacy.build_bootstrap_status.return_value = {}
        fake_legacy.build_context_lifecycle_readiness.return_value = {"status": "ready"}
        fake_legacy.check_gateway_health_for_env.return_value = True
        fake_legacy.build_environment_promotion_summary.return_value = {}

        with mock.patch("services.data_collector._legacy_dashboard", return_value=fake_legacy):
            env = self.collector.get_environment(force_refresh=True)

        self.assertEqual(env["active_environment"], "primary")
        self.assertEqual(env["binding_audit"]["active_env"], "primary")
        self.assertEqual(env["binding_audit"]["source"], "runtime_db")
        self.assertEqual(env["binding_audit"]["recent_events"], [{"source": "db"}])

    def test_get_environment_exposes_binding_audit(self):
        fake_legacy = mock.Mock()
        fake_legacy.list_openclaw_environments.return_value = []
        fake_legacy.check_gateway_health_for_env.return_value = True
        fake_legacy.build_bootstrap_status.return_value = {}
        fake_legacy.build_context_lifecycle_readiness.return_value = {"status": "ready"}
        fake_legacy.build_environment_promotion_summary.return_value = {}
        context = {
            "legacy": fake_legacy,
            "config": {},
            "active_env": "primary",
            "binding": {"active_env": "primary", "switch_state": "committed", "updated_at": 1},
            "selected_env": {"id": "primary"},
            "task_registry": {},
        }
        with mock.patch.object(self.collector, "_load_runtime_context", return_value=context), \
            mock.patch.object(self.collector, "_shared_state", side_effect=[{}, {}, {}, []]):
            env = self.collector._fetch_environment_data()
        self.assertEqual(env["binding_audit"]["active_env"], "primary")

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
                    "state_label": "待机",
                    "detail": "暂无最近会话",
                    "task_hint": "",
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
                    "state_label": "命令完成",
                    "detail": "最近会话有更新",
                    "task_hint": "修复同步链路",
                    "sessions": 2,
                    "recent_sessions": [{"session_file": "a.jsonl", "updated_at": int(time.time()) - 60, "updated_label": "03-14 00:01:00", "state_label": "命令完成", "detail": "最近会话有更新", "task_hint": "修复同步链路"}],
                    "is_active": False,
                    "activity_source": "session",
                    "activity_excerpt": "最近会话有更新",
                },
            }), \
            mock.patch.object(self.collector, "_load_agent_log_activity", return_value={
                "main": {
                    "updated_at": int(time.time()) - 30,
                    "updated_label": "03-14 00:01:30",
                    "state_label": "日志活跃",
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
        self.assertEqual(data["agents"][0]["activity_source"], "gateway_log")
        self.assertEqual(data["agents"][1]["id"], "dev")
        self.assertEqual(data["agents"][1]["sessions"], 2)
    
    def test_get_learnings(self):
        """测试获取学习数据"""
        learnings = self.collector.get_learnings()
        self.assertIn('is_fresh', learnings)
        self.assertIn('items', learnings)
        self.assertIn('reflections', learnings)
        self.assertIn('timestamp', learnings)

    def test_get_snapshots(self):
        """测试获取快照数据"""
        snapshots = self.collector.get_snapshots(force_refresh=True)
        self.assertIn('count', snapshots)
        self.assertIn('snapshots', snapshots)
    
    def test_get_events(self):
        """测试获取事件数据"""
        events = self.collector.get_events(limit=10)
        self.assertIsInstance(events, list)
    
    def test_get_health_score_data(self):
        """测试获取健康评分数据"""
        data = self.collector.get_health_score_data()
        self.assertIn('environment', data)
        self.assertIn('metrics', data)
        self.assertIn('tasks', data)
        self.assertIn('learning', data)
        self.assertIn('errors', data)
    
    def test_invalidate_cache(self):
        """测试使缓存失效"""
        # 先缓存一些数据
        self.collector.get_metrics()
        self.collector.get_environment()
        
        # 使单个缓存失效
        self.collector.invalidate_cache('metrics')
        
        # 使所有缓存失效
        self.collector.invalidate_cache()


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
