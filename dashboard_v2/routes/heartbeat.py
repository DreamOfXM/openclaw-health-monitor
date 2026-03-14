"""
心跳监控 + Guardrail API 路由
"""
from flask import Blueprint, jsonify, request
import sys
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from heartbeat_guardrail import TaskWatcher, HeartbeatPhase
from state_store import MonitorStateStore

bp = Blueprint('heartbeat', __name__, url_prefix='/api/v2/heartbeat')

# 全局状态
_store = None
_watcher = None

def get_watcher():
    """获取 TaskWatcher 单例"""
    global _store, _watcher
    if _store is None:
        _store = MonitorStateStore(Path(__file__).parent.parent.parent / "data")
    if _watcher is None:
        _watcher = TaskWatcher(_store)
    return _watcher


@bp.route('/status', methods=['GET'])
def get_status():
    """获取心跳监控状态"""
    try:
        watcher = get_watcher()
        result = watcher.check_all_tasks()
        
        return jsonify({
            'success': True,
            'data': result
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/report', methods=['GET'])
def get_report():
    """获取可观测性报告"""
    try:
        watcher = get_watcher()
        report = watcher.get_observability_report()
        
        return jsonify({
            'success': True,
            'data': report
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/heartbeats', methods=['GET'])
def get_heartbeats():
    """获取最近的心跳记录"""
    try:
        limit = request.args.get('limit', 20, type=int)
        watcher = get_watcher()
        heartbeats = watcher.heartbeat_monitor._get_recent_heartbeats(limit)
        
        return jsonify({
            'success': True,
            'data': {
                'count': len(heartbeats),
                'heartbeats': heartbeats
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/timeout-tasks', methods=['GET'])
def get_timeout_tasks():
    """获取超时任务"""
    try:
        watcher = get_watcher()
        timeout_tasks = watcher.heartbeat_monitor.get_timeout_tasks()
        
        return jsonify({
            'success': True,
            'data': {
                'count': len(timeout_tasks),
                'tasks': timeout_tasks
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/recover/<task_id>', methods=['POST'])
def recover_task(task_id):
    """恢复超时任务"""
    try:
        watcher = get_watcher()
        result = watcher.recover_timeout_task(task_id)
        
        return jsonify({
            'success': result.get('success', False),
            'data': result
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/guardrail/rules', methods=['GET'])
def get_guardrail_rules():
    """获取 Guardrail 规则"""
    try:
        watcher = get_watcher()
        rules = [
            {
                'name': rule.name,
                'action': rule.action.value,
                'message': rule.message,
                'max_attempts': rule.max_attempts,
            }
            for rule in watcher.guardrail_engine.rules
        ]
        
        return jsonify({
            'success': True,
            'data': {
                'count': len(rules),
                'rules': rules
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/guardrail/transitions', methods=['GET'])
def get_guardrail_transitions():
    """获取状态转换规则"""
    try:
        watcher = get_watcher()
        transitions = [
            {
                'from_state': trans.from_state.value,
                'to_state': trans.to_state.value,
                'trigger': trans.trigger,
            }
            for trans in watcher.guardrail_engine.transitions.values()
        ]
        
        return jsonify({
            'success': True,
            'data': {
                'count': len(transitions),
                'transitions': transitions
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/phases', methods=['GET'])
def get_phases():
    """获取心跳阶段配置"""
    try:
        watcher = get_watcher()
        config = watcher.heartbeat_monitor.config
        
        phases = []
        for phase in HeartbeatPhase:
            interval = config.intervals.get(phase, 30)
            multiplier = config.timeout_multipliers.get(phase, 3.0)
            timeout = int(interval * multiplier)
            
            phases.append({
                'phase': phase.value,
                'interval_seconds': interval,
                'timeout_seconds': timeout,
            })
        
        return jsonify({
            'success': True,
            'data': {
                'phases': phases,
                'max_retries': config.max_retries,
                'retry_delays': config.retry_delays,
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500