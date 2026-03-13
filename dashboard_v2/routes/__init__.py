"""
路由初始化
"""
from .health import bp as health_bp
from .metrics import bp as metrics_bp
from .events import bp as events_bp
from .tasks import bp as tasks_bp
from .environments import bp as environments_bp
from .agents import bp as agents_bp
from .learnings import bp as learnings_bp

__all__ = [
    'health_bp',
    'metrics_bp',
    'events_bp',
    'tasks_bp',
    'environments_bp',
    'agents_bp',
    'learnings_bp'
]