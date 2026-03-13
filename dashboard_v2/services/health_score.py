"""
健康评分计算服务
基于DataDog的Service Health Score设计理念
"""
from typing import Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
import math


@dataclass
class HealthScoreConfig:
    """健康评分配置"""
    # 基础分
    base_score: int = 100
    
    # 扣分权重
    gateway_unhealthy_weight: int = 20
    blocked_task_weight: int = 10
    memory_pressure_weight: int = 10
    cpu_pressure_weight: int = 10
    error_category_weight: int = 5
    learning_stagnant_weight: int = 5
    
    # 阈值
    memory_pressure_threshold: float = 80.0  # %
    cpu_pressure_threshold: float = 90.0  # %
    learning_stagnant_threshold: int = 24  # hours


@dataclass
class HealthScoreResult:
    """健康评分结果"""
    score: int
    status: str  # 'excellent', 'good', 'warning', 'critical'
    status_emoji: str
    status_color: str
    deductions: list
    last_updated: datetime


class HealthScoreCalculator:
    """健康评分计算器"""
    
    def __init__(self, config: Optional[HealthScoreConfig] = None):
        self.config = config or HealthScoreConfig()
    
    def calculate(
        self,
        environment_data: Dict[str, Any],
        metrics_data: Dict[str, Any],
        task_data: Dict[str, Any],
        learning_data: Dict[str, Any],
        error_data: Dict[str, Any]
    ) -> HealthScoreResult:
        """
        计算系统健康评分
        
        算法：
        1. 从基础分100开始
        2. 根据各项指标扣减分数
        3. 确保最低0分
        4. 根据分数区间确定状态
        """
        score = self.config.base_score
        deductions = []
        
        # 1. 检查Gateway健康状态
        gateway_healthy = environment_data.get('gateway_healthy', False)
        if not gateway_healthy:
            score -= self.config.gateway_unhealthy_weight
            deductions.append({
                'category': 'gateway',
                'reason': 'Gateway不健康',
                'weight': self.config.gateway_unhealthy_weight,
                'severity': 'critical'
            })
        
        # 2. 检查阻塞任务
        blocked_tasks = task_data.get('blocked_count', 0)
        if blocked_tasks > 0:
            deduction = min(blocked_tasks * self.config.blocked_task_weight, 30)
            score -= deduction
            deductions.append({
                'category': 'tasks',
                'reason': f'{blocked_tasks}个阻塞任务',
                'weight': deduction,
                'severity': 'warning' if blocked_tasks < 3 else 'critical'
            })
        
        # 3. 检查内存压力
        memory_percent = metrics_data.get('memory_percent', 0)
        if memory_percent > self.config.memory_pressure_threshold:
            deduction = self.config.memory_pressure_weight
            score -= deduction
            deductions.append({
                'category': 'memory',
                'reason': f'内存使用{memory_percent:.1f}%',
                'weight': deduction,
                'severity': 'warning'
            })
        
        # 4. 检查CPU压力
        cpu_percent = metrics_data.get('cpu_percent', 0)
        if cpu_percent > self.config.cpu_pressure_threshold:
            deduction = self.config.cpu_pressure_weight
            score -= deduction
            deductions.append({
                'category': 'cpu',
                'reason': f'CPU使用{cpu_percent:.1f}%',
                'weight': deduction,
                'severity': 'warning'
            })
        
        # 5. 检查错误分类
        error_categories = error_data.get('categories', [])
        if error_categories:
            deduction = min(len(error_categories) * self.config.error_category_weight, 20)
            score -= deduction
            deductions.append({
                'category': 'errors',
                'reason': f'{len(error_categories)}类错误',
                'weight': deduction,
                'severity': 'warning'
            })
        
        # 6. 检查学习滞后
        learning_fresh = learning_data.get('is_fresh', True)
        if not learning_fresh:
            score -= self.config.learning_stagnant_weight
            deductions.append({
                'category': 'learning',
                'reason': '学习系统滞后',
                'weight': self.config.learning_stagnant_weight,
                'severity': 'info'
            })
        
        # 确保分数不低于0
        score = max(0, score)
        critical_deductions = [item for item in deductions if item.get('severity') == 'critical']
        if score < 10 and len(critical_deductions) >= 2:
            score = 0
        
        # 确定状态
        status, emoji, color = self._get_status(score)
        
        return HealthScoreResult(
            score=score,
            status=status,
            status_emoji=emoji,
            status_color=color,
            deductions=deductions,
            last_updated=datetime.now()
        )
    
    def _get_status(self, score: int) -> tuple:
        """根据分数确定状态"""
        if score >= 90:
            return ('excellent', '✅', '#22c55e')  # 绿色
        elif score >= 70:
            return ('good', '🟡', '#eab308')  # 黄色
        elif score >= 50:
            return ('warning', '🟠', '#f97316')  # 橙色
        else:
            return ('critical', '🔴', '#ef4444')  # 红色
    
    def get_next_action(self, result: HealthScoreResult) -> Dict[str, Any]:
        """
        根据健康评分确定下一步行动建议
        优先级：立即处理 > 今天处理 > 本周关注 > 保持现状
        """
        if result.score < 50:
            # 找出最严重的问题
            critical = [d for d in result.deductions if d['severity'] == 'critical']
            if critical:
                return {
                    'priority': 'immediate',
                    'priority_label': '立即处理',
                    'message': critical[0]['reason'],
                    'action': '查看详情',
                    'action_type': 'view',
                    'color': '#ef4444'
                }
        
        if result.score < 70:
            warnings = [d for d in result.deductions if d['severity'] == 'warning']
            if warnings:
                return {
                    'priority': 'today',
                    'priority_label': '今天处理',
                    'message': warnings[0]['reason'],
                    'action': '查看详情',
                    'action_type': 'view',
                    'color': '#f97316'
                }
        
        if result.score < 90:
            infos = [d for d in result.deductions if d['severity'] == 'info']
            if infos:
                return {
                    'priority': 'week',
                    'priority_label': '本周关注',
                    'message': infos[0]['reason'],
                    'action': '查看详情',
                    'action_type': 'view',
                    'color': '#3b82f6'
                }
        
        return {
            'priority': 'none',
            'priority_label': '保持现状',
            'message': '系统运行正常',
            'action': None,
            'action_type': None,
            'color': '#22c55e'
        }


# 全局计算器实例
_calculator = None

def get_calculator() -> HealthScoreCalculator:
    """获取健康评分计算器单例"""
    global _calculator
    if _calculator is None:
        _calculator = HealthScoreCalculator()
    return _calculator

def calculate_health_score(
    environment_data: Dict[str, Any],
    metrics_data: Dict[str, Any],
    task_data: Dict[str, Any],
    learning_data: Dict[str, Any],
    error_data: Dict[str, Any]
) -> HealthScoreResult:
    """便捷函数：计算健康评分"""
    calculator = get_calculator()
    return calculator.calculate(
        environment_data=environment_data,
        metrics_data=metrics_data,
        task_data=task_data,
        learning_data=learning_data,
        error_data=error_data
    )
