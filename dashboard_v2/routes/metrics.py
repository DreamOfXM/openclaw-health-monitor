"""
系统指标API路由
"""
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector

bp = Blueprint('metrics', __name__, url_prefix='/api/v2/metrics')


@bp.route('/', methods=['GET'])
def get_metrics():
    """获取系统指标"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        data = collector.get_metrics(force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': data
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/cpu', methods=['GET'])
def get_cpu():
    """获取CPU指标"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        metrics = collector.get_metrics(force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': {
                'percent': metrics.get('cpu_percent', 0),
                'timestamp': metrics.get('timestamp')
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/memory', methods=['GET'])
def get_memory():
    """获取内存指标"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        metrics = collector.get_metrics(force_refresh=force_refresh)
        
        return jsonify({
            'success': True,
            'data': {
                'percent': metrics.get('memory_percent', 0),
                'used_gb': metrics.get('memory_used_gb', 0),
                'total_gb': metrics.get('memory_total_gb', 0),
                'timestamp': metrics.get('timestamp')
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500