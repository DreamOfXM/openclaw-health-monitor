"""
学习API路由
"""
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector

bp = Blueprint('learnings', __name__, url_prefix='/api/v2/learnings')


@bp.route('/', methods=['GET'])
def get_learnings():
    """获取学习数据"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        data = collector.get_learnings(force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': data
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/status', methods=['GET'])
def get_learning_status():
    """获取学习系统状态"""
    try:
        collector = get_collector()
        data = collector.get_learnings()
        
        return jsonify({
            'success': True,
            'data': {
                'is_fresh': data.get('is_fresh', True),
                'last_update': data.get('last_update'),
                'item_count': len(data.get('items', [])),
                'timestamp': data.get('timestamp')
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500