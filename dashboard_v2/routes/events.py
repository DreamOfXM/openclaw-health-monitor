"""
事件时间线API路由
"""
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector

bp = Blueprint('events', __name__, url_prefix='/api/v2/events')


@bp.route('/', methods=['GET'])
def get_events():
    """获取事件时间线"""
    try:
        limit = request.args.get('limit', 20, type=int)
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        
        collector = get_collector()
        events = collector.get_events(limit=limit, force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': {
                'events': events,
                'count': len(events)
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500