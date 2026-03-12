"""
健康评分服务测试
"""
import unittest
from datetime import datetime
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.health_score import (
    HealthScoreCalculator,
    HealthScoreConfig,
    calculate_health_score,
    get_calculator
)


class TestHealthScoreCalculator(unittest.TestCase):
    """测试健康评分计算器"""
    
    def setUp(self):
        """测试前准备"""
        self.calculator = HealthScoreCalculator()
        
        # 基础测试数据
        self.base_environment = {'gateway_healthy': True}
        self.base_metrics = {'cpu_percent': 30, 'memory_percent': 50}
        self.base_tasks = {'blocked_count': 0}
        self.base_learning = {'is_fresh': True}
        self.base_errors = {'categories': []}
    
    def test_perfect_health_score(self):
        """测试完美健康评分"""
        result = self.calculator.calculate(
            environment_data=self.base_environment,
            metrics_data=self.base_metrics,
            task_data=self.base_tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        self.assertEqual(result.score, 100)
        self.assertEqual(result.status, 'excellent')
        self.assertEqual(len(result.deductions), 0)
    
    def test_gateway_unhealthy(self):
        """测试Gateway不健康"""
        env = {'gateway_healthy': False}
        
        result = self.calculator.calculate(
            environment_data=env,
            metrics_data=self.base_metrics,
            task_data=self.base_tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        self.assertEqual(result.score, 80)  # 100 - 20
        self.assertEqual(result.status, 'good')
        self.assertEqual(len(result.deductions), 1)
        self.assertEqual(result.deductions[0]['category'], 'gateway')
    
    def test_blocked_tasks(self):
        """测试阻塞任务"""
        tasks = {'blocked_count': 2}
        
        result = self.calculator.calculate(
            environment_data=self.base_environment,
            metrics_data=self.base_metrics,
            task_data=tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        self.assertEqual(result.score, 80)  # 100 - 20
        self.assertEqual(result.status, 'good')
        self.assertEqual(len(result.deductions), 1)
        self.assertEqual(result.deductions[0]['category'], 'tasks')
    
    def test_memory_pressure(self):
        """测试内存压力"""
        metrics = {'cpu_percent': 30, 'memory_percent': 85}
        
        result = self.calculator.calculate(
            environment_data=self.base_environment,
            metrics_data=metrics,
            task_data=self.base_tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        self.assertEqual(result.score, 90)  # 100 - 10
        self.assertEqual(result.status, 'excellent')
    
    def test_cpu_pressure(self):
        """测试CPU压力"""
        metrics = {'cpu_percent': 95, 'memory_percent': 50}
        
        result = self.calculator.calculate(
            environment_data=self.base_environment,
            metrics_data=metrics,
            task_data=self.base_tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        self.assertEqual(result.score, 90)  # 100 - 10
    
    def test_multiple_issues(self):
        """测试多个问题同时存在"""
        env = {'gateway_healthy': False}
        tasks = {'blocked_count': 1}
        metrics = {'cpu_percent': 95, 'memory_percent': 85}
        
        result = self.calculator.calculate(
            environment_data=env,
            metrics_data=metrics,
            task_data=tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        # 100 - 20(gateway) - 10(tasks) - 10(cpu) - 10(memory) = 50
        self.assertEqual(result.score, 50)
        self.assertEqual(result.status, 'warning')
        self.assertEqual(len(result.deductions), 4)
    
    def test_critical_score(self):
        """测试严重状态"""
        env = {'gateway_healthy': False}
        tasks = {'blocked_count': 5}  # 50分扣除，但上限30
        metrics = {'cpu_percent': 95, 'memory_percent': 85}
        
        result = self.calculator.calculate(
            environment_data=env,
            metrics_data=metrics,
            task_data=tasks,
            learning_data=self.base_learning,
            error_data=self.base_errors
        )
        
        self.assertLess(result.score, 50)
        self.assertEqual(result.status, 'critical')
    
    def test_zero_score_floor(self):
        """测试最低分数为0"""
        env = {'gateway_healthy': False}
        tasks = {'blocked_count': 10}
        metrics = {'cpu_percent': 100, 'memory_percent': 100}
        errors = {'categories': ['error1', 'error2', 'error3', 'error4']}
        learning = {'is_fresh': False}
        
        result = self.calculator.calculate(
            environment_data=env,
            metrics_data=metrics,
            task_data=tasks,
            learning_data=learning,
            error_data=errors
        )
        
        self.assertEqual(result.score, 0)
        self.assertEqual(result.status, 'critical')


class TestHealthScoreStatus(unittest.TestCase):
    """测试健康评分状态判断"""
    
    def setUp(self):
        self.calculator = HealthScoreCalculator()
    
    def test_excellent_status(self):
        """测试优秀状态"""
        status, emoji, color = self.calculator._get_status(95)
        self.assertEqual(status, 'excellent')
        self.assertEqual(emoji, '✅')
        self.assertEqual(color, '#22c55e')
    
    def test_good_status(self):
        """测试良好状态"""
        status, emoji, color = self.calculator._get_status(75)
        self.assertEqual(status, 'good')
        self.assertEqual(emoji, '🟡')
        self.assertEqual(color, '#eab308')
    
    def test_warning_status(self):
        """测试警告状态"""
        status, emoji, color = self.calculator._get_status(60)
        self.assertEqual(status, 'warning')
        self.assertEqual(emoji, '🟠')
        self.assertEqual(color, '#f97316')
    
    def test_critical_status(self):
        """测试严重状态"""
        status, emoji, color = self.calculator._get_status(30)
        self.assertEqual(status, 'critical')
        self.assertEqual(emoji, '🔴')
        self.assertEqual(color, '#ef4444')


class TestNextAction(unittest.TestCase):
    """测试下一步行动建议"""
    
    def setUp(self):
        self.calculator = HealthScoreCalculator()
        from services.health_score import HealthScoreResult
        
        self.base_result = HealthScoreResult(
            score=100,
            status='excellent',
            status_emoji='✅',
            status_color='#22c55e',
            deductions=[],
            last_updated=datetime.now()
        )
    
    def test_no_action_needed(self):
        """测试无需行动"""
        action = self.calculator.get_next_action(self.base_result)
        self.assertEqual(action['priority'], 'none')
        self.assertEqual(action['priority_label'], '保持现状')
    
    def test_critical_action(self):
        """测试立即处理"""
        from services.health_score import HealthScoreResult
        from datetime import datetime
        
        result = HealthScoreResult(
            score=40,
            status='critical',
            status_emoji='🔴',
            status_color='#ef4444',
            deductions=[{
                'category': 'gateway',
                'reason': 'Gateway不健康',
                'weight': 20,
                'severity': 'critical'
            }],
            last_updated=datetime.now()
        )
        
        action = self.calculator.get_next_action(result)
        self.assertEqual(action['priority'], 'immediate')
        self.assertEqual(action['priority_label'], '立即处理')


class TestGlobalFunctions(unittest.TestCase):
    """测试全局函数"""
    
    def test_get_calculator_singleton(self):
        """测试计算器单例"""
        calc1 = get_calculator()
        calc2 = get_calculator()
        self.assertIs(calc1, calc2)
    
    def test_calculate_health_score(self):
        """测试便捷函数"""
        result = calculate_health_score(
            environment_data={'gateway_healthy': True},
            metrics_data={'cpu_percent': 30, 'memory_percent': 50},
            task_data={'blocked_count': 0},
            learning_data={'is_fresh': True},
            error_data={'categories': []}
        )
        
        self.assertEqual(result.score, 100)


if __name__ == '__main__':
    unittest.main(verbosity=2)