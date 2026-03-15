"""
环境API路由
"""
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector

bp = Blueprint('environments', __name__, url_prefix='/api/v2/environments')


@bp.route('/', methods=['GET'])
def get_environments():
    """获取环境列表"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        collector = get_collector()
        data = collector.get_environment(force_refresh=force_refresh)
        
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
def get_active_environment():
    """获取当前激活的环境"""
    try:
        collector = get_collector()
        data = collector.get_environment()
        
        return jsonify({
            'success': True,
            'data': {
                'active': data.get('active_environment', 'unknown'),
                'healthy': data.get('gateway_healthy', False)
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/switch', methods=['POST'])
def switch_environment():
    """兼容旧接口；单环境模式下只允许 primary。"""
    try:
        data = request.get_json() or {}
        target_env = data.get('environment')
        
        if not target_env:
            return jsonify({
                'success': False,
                'error': '未指定目标环境'
            }), 400
        
        collector = get_collector()
        result = collector.switch_environment(target_env)
        return jsonify({
            'success': bool(result.get('success')),
            'data': result
        }), (200 if result.get('success') else 500)
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/restart', methods=['POST'])
def restart_environment():
    """重启当前唯一运行环境（高危操作）"""
    try:
        collector = get_collector()
        result = collector.restart_environment()
        success = bool(result.get('success'))
        message = str(result.get('message') or '重启流程已结束')
        return jsonify({
            'success': bool(success),
            'data': {
                **result,
                'message': message,
            }
        }), (200 if success else 500)
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/snapshots', methods=['GET'])
def get_snapshots():
    """获取快照列表"""
    try:
        force_refresh = request.args.get('refresh', 'false').lower() == 'true'
        limit = max(1, min(request.args.get('limit', 20, type=int), 100))
        offset = max(0, request.args.get('offset', 0, type=int))
        collector = get_collector()
        data = collector.get_snapshots(limit=limit, offset=offset, force_refresh=force_refresh)
        return jsonify({
            'success': True,
            'data': data
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/snapshots', methods=['POST'])
def create_snapshot():
    """创建快照"""
    try:
        data = request.get_json() or {}
        label = str(data.get('label') or '').strip()
        if not label:
            return jsonify({
                'success': False,
                'error': '未指定快照标签'
            }), 400
        collector = get_collector()
        result = collector.create_snapshot(label)
        return jsonify({
            'success': True,
            'data': result
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@bp.route('/snapshots/restore', methods=['POST'])
def restore_snapshot():
    """恢复快照"""
    try:
        data = request.get_json() or {}
        name = str(data.get('name') or '').strip()
        if not name:
            return jsonify({
                'success': False,
                'error': '未指定快照名称'
            }), 400
        collector = get_collector()
        result = collector.restore_snapshot(name)
        return jsonify({
            'success': bool(result.get('success')),
            'data': result
        }), (200 if result.get('success') else 500)
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
