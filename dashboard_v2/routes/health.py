"""
健康评分API路由
"""
from flask import Blueprint, jsonify, request
from services.health_score import calculate_health_score, get_calculator
from services.data_collector import get_collector

bp = Blueprint('health', __name__, url_prefix='/api/v2/health')


@bp.route('/score', methods=['GET'])
def get_score():
    """获取健康评分"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        
        collector = get_collector()
        health_data = collector.get_health_score_data(force_refresh=force_refresh)
        
        result = calculate_health_score(
            environment_data=health_data.get('environment', {}),
            metrics_data=health_data.get('metrics', {}),
            task_data=health_data.get('tasks', {}),
            learning_data=health_data.get('learning', {}),
            error_data=health_data.get('errors', {})
        )
        
        next_action = get_calculator().get_next_action(result)
        
        return jsonify({
            'success': True,
            'data': {
                'score': result.score,
                'status': result.status,
                'status_emoji': result.status_emoji,
                'status_color': result.status_color,
                'deductions': result.deductions,
                'next_action': next_action,
                'last_updated': result.last_updated.isoformat()
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/score/details', methods=['GET'])
def get_score_details():
    """获取健康评分详情"""
    try:
        collector = get_collector()
        health_data = collector.get_health_score_data()
        
        result = calculate_health_score(
            environment_data=health_data.get('environment', {}),
            metrics_data=health_data.get('metrics', {}),
            task_data=health_data.get('tasks', {}),
            learning_data=health_data.get('learning', {}),
            error_data=health_data.get('errors', {})
        )
        
        return jsonify({
            'success': True,
            'data': {
                'score': result.score,
                'status': result.status,
                'deductions': result.deductions,
                'raw_data': health_data
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/next-action', methods=['GET'])
def get_next_action():
    """获取下一步行动建议"""
    try:
        collector = get_collector()
        health_data = collector.get_health_score_data()
        
        result = calculate_health_score(
            environment_data=health_data.get('environment', {}),
            metrics_data=health_data.get('metrics', {}),
            task_data=health_data.get('tasks', {}),
            learning_data=health_data.get('learning', {}),
            error_data=health_data.get('errors', {})
        )
        
        next_action = get_calculator().get_next_action(result)
        
        return jsonify({
            'success': True,
            'data': next_action
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500