"""
API路由测试
需要Flask环境
"""
import unittest
import json
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app

# 模拟Flask环境
class MockResponse:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
    
    def get_json(self):
        return json.loads(self.data)


class TestHealthRoutes(unittest.TestCase):
    """测试健康评分路由"""
    
    def test_health_score_calculation(self):
        """测试健康评分计算逻辑"""
        from services.health_score import calculate_health_score
        
        # 测试数据
        result = calculate_health_score(
            environment_data={'gateway_healthy': True},
            metrics_data={'cpu_percent': 30, 'memory_percent': 50},
            task_data={'blocked_count': 0},
            learning_data={'is_fresh': True},
            error_data={'categories': []}
        )
        
        # 验证结果
        self.assertEqual(result.score, 100)
        self.assertEqual(result.status, 'excellent')
        self.assertEqual(len(result.deductions), 0)
    
    def test_health_score_with_issues(self):
        """测试有问题的健康评分"""
        from services.health_score import calculate_health_score
        
        result = calculate_health_score(
            environment_data={'gateway_healthy': False},
            metrics_data={'cpu_percent': 95, 'memory_percent': 85},
            task_data={'blocked_count': 2},
            learning_data={'is_fresh': False},
            error_data={'categories': ['error1']}
        )
        
        # 应该有扣分
        self.assertLess(result.score, 100)
        self.assertGreater(len(result.deductions), 0)
    
    def test_next_action_logic(self):
        """测试下一步行动逻辑"""
        from services.health_score import get_calculator, HealthScoreResult
        from datetime import datetime
        
        calculator = get_calculator()
        
        # 优秀状态
        excellent_result = HealthScoreResult(
            score=95,
            status='excellent',
            status_emoji='✅',
            status_color='#22c55e',
            deductions=[],
            last_updated=datetime.now()
        )
        
        action = calculator.get_next_action(excellent_result)
        self.assertEqual(action['priority'], 'none')


class TestDataCollectorRoutes(unittest.TestCase):
    """测试数据收集路由"""
    
    def test_metrics_data_structure(self):
        """测试指标数据结构"""
        from services.data_collector import get_collector
        
        collector = get_collector()
        metrics = collector.get_metrics()
        
        # 验证返回的数据结构
        self.assertIn('cpu_percent', metrics)
        self.assertIn('memory_percent', metrics)
        self.assertIn('memory_used_gb', metrics)
        self.assertIn('memory_total_gb', metrics)
        self.assertIn('timestamp', metrics)
        
        # 验证数据类型
        self.assertIsInstance(metrics['cpu_percent'], (int, float))
        self.assertIsInstance(metrics['memory_percent'], (int, float))
    
    def test_environment_data_structure(self):
        """测试环境数据结构"""
        from services.data_collector import get_collector
        
        collector = get_collector()
        env = collector.get_environment()
        
        self.assertIn('gateway_healthy', env)
        self.assertIn('active_environment', env)
        self.assertIn('environments', env)
        self.assertIn('timestamp', env)
    
    def test_tasks_data_structure(self):
        """测试任务数据结构"""
        from services.data_collector import get_collector
        
        collector = get_collector()
        tasks = collector.get_tasks()
        
        self.assertIn('blocked_count', tasks)
        self.assertIn('total_count', tasks)
        self.assertIn('tasks', tasks)
        self.assertIn('summary', tasks)
        self.assertIn('timestamp', tasks)
    
    def test_agents_data_structure(self):
        """测试代理数据结构"""
        from services.data_collector import get_collector
        
        collector = get_collector()
        agents = collector.get_agents()
        
        self.assertIn('active_count', agents)
        self.assertIn('agents', agents)
        self.assertIn('timestamp', agents)
    
    def test_learnings_data_structure(self):
        """测试学习数据结构"""
        from services.data_collector import get_collector
        
        collector = get_collector()
        learnings = collector.get_learnings()
        
        self.assertIn('is_fresh', learnings)
        self.assertIn('items', learnings)
        self.assertIn('reflections', learnings)
        self.assertIn('timestamp', learnings)
    
    def test_health_score_data_structure(self):
        """测试健康评分数据结构"""
        from services.data_collector import get_collector
        
        collector = get_collector()
        data = collector.get_health_score_data()
        
        self.assertIn('environment', data)
        self.assertIn('metrics', data)
        self.assertIn('tasks', data)
        self.assertIn('learning', data)
        self.assertIn('errors', data)


class TestCacheFunctionality(unittest.TestCase):
    """测试缓存功能"""
    
    def test_cache_ttl(self):
        """测试缓存TTL"""
        from services.data_collector import DataCache
        import time
        
        cache = DataCache()
        
        # 设置短TTL缓存
        cache.set('test', {'data': 'value'}, ttl=0)
        time.sleep(0.1)
        
        # 应该过期
        result = cache.get('test')
        self.assertIsNone(result)
    
    def test_cache_hit(self):
        """测试缓存命中"""
        from services.data_collector import DataCache
        
        cache = DataCache()
        cache.set('test', {'data': 'value'}, ttl=60)
        
        result = cache.get('test')
        self.assertEqual(result, {'data': 'value'})


class TestIntegration(unittest.TestCase):
    """集成测试"""
    
    def test_end_to_end_health_calculation(self):
        """测试端到端健康评分计算"""
        from services.data_collector import get_collector
        from services.health_score import calculate_health_score
        
        # 1. 获取数据
        collector = get_collector()
        data = collector.get_health_score_data()
        
        # 2. 计算健康评分
        result = calculate_health_score(
            environment_data=data['environment'],
            metrics_data=data['metrics'],
            task_data=data['tasks'],
            learning_data=data['learning'],
            error_data=data['errors']
        )
        
        # 3. 验证结果
        self.assertIsNotNone(result.score)
        self.assertIn(result.status, ['excellent', 'good', 'warning', 'critical'])
        self.assertIsInstance(result.deductions, list)


class TestFlaskRoutes(unittest.TestCase):
    """测试 Flask 路由行为"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def test_v2_routes_accept_paths_without_trailing_slash(self):
        """前端调用的无斜杠路径应直接可用"""
        for path in [
            '/api/v2/metrics',
            '/api/v2/events',
            '/api/v2/environments',
            '/api/v2/tasks',
            '/api/v2/agents',
            '/api/v2/learnings',
            '/api/v2/heartbeat/openclaw',
        ]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_openclaw_heartbeat_endpoint_returns_structured_payload(self):
        response = self.client.get('/api/v2/heartbeat/openclaw')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertIn('status', payload['data'])
        self.assertIn('every', payload['data'])
        self.assertIn('effective_prompt_count', payload['data'])

    def test_legacy_status_endpoint_still_available(self):
        """旧脚本依赖的状态接口应继续可用"""
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('gateway_healthy', payload)
        self.assertIn('incident_summary', payload)
        self.assertIn('memory_summary', payload)

    def test_legacy_support_endpoints_still_available(self):
        """保留旧版共享状态接口兼容性"""
        for path in ['/api/task-registry', '/api/shared-state', '/api/context-baseline']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_restart_route_returns_structured_payload(self):
        with mock.patch('routes.environments.get_collector') as get_collector:
            get_collector.return_value.restart_environment.return_value = {
                'success': True,
                'message': 'Primary 已开始重启',
                'environment': 'primary',
            }
            response = self.client.post('/api/v2/environments/restart', json={})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data']['environment'], 'primary')

    def test_emergency_recover_route_returns_structured_payload(self):
        with mock.patch('routes.environments.get_collector') as get_collector:
            get_collector.return_value.emergency_recover.return_value = {
                'success': False,
                'message': '没有可恢复的配置快照',
                'rollback_guidance': {'target': 'v2026.3.11'},
            }
            response = self.client.post('/api/v2/environments/emergency-recover', json={})
        self.assertEqual(response.status_code, 500)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['data']['rollback_guidance']['target'], 'v2026.3.11')

    def test_switch_route_rejects_missing_environment(self):
        with mock.patch('routes.environments.get_collector') as get_collector:
            response = self.client.post('/api/v2/environments/switch', json={})
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
