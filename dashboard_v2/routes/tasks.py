"""
任务API路由
"""
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector

bp = Blueprint('tasks', __name__, url_prefix='/api/v2/tasks')


@bp.route('/', methods=['GET'])
def get_tasks():
    """获取任务列表"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        data = collector.get_tasks(force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': data
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/<task_id>', methods=['GET'])
def get_task(task_id):
    """获取单个任务详情"""
    try:
        collector = get_collector()
        data = collector.get_tasks(force_refresh=True)
        task = next((item for item in data.get('tasks', []) if item.get('id') == task_id), None)
        if task is None and data.get('current', {}).get('id') == task_id:
            task = data.get('current')
        if task is None:
            return jsonify({
                'success': False,
                'error': '任务不存在'
            }), 404
        return jsonify({
            'success': True,
            'data': task
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/blocked', methods=['GET'])
def get_blocked_tasks():
    """获取阻塞任务列表"""
    try:
        collector = get_collector()
        data = collector.get_tasks()
        
        blocked = [t for t in data.get('tasks', []) if t.get('status') == 'blocked']
        
        return jsonify({
            'success': True,
            'data': {
                'count': len(blocked),
                'tasks': blocked
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
