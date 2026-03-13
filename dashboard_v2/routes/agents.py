"""
代理API路由
"""
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector

bp = Blueprint('agents', __name__, url_prefix='/api/v2/agents')


@bp.route('/', methods=['GET'])
def get_agents():
    """获取代理列表"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        data = collector.get_agents(force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': data
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/active', methods=['GET'])
def get_active_agents():
    """获取活跃代理"""
    try:
        collector = get_collector()
        data = collector.get_agents()
        
        active = [a for a in data.get('agents', []) if a.get('is_active', False)]
        
        return jsonify({
            'success': True,
            'data': {
                'count': len(active),
                'agents': active
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500