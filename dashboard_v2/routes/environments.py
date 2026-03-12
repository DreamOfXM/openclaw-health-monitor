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
                'active': data.get('active_environment', 'primary'),
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
    """切换环境（高危操作）"""
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


@bp.route('/promote', methods=['POST'])
def promote_environment():
    """执行版本晋升（高危操作）"""
    try:
        data = request.get_json() or {}
        confirmation = data.get('confirmation')
        
        if confirmation != 'PROMOTE':
            return jsonify({
                'success': False,
                'error': '确认码错误，操作取消'
            }), 403
        
        collector = get_collector()
        result = collector.promote_environment()
        status = str(result.get('status') or '')
        success = status == 'promoted'
        message = str(result.get('error') or result.get('message') or status or '晋升流程已结束')
        return jsonify({
            'success': success,
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
        collector = get_collector()
        data = collector.get_snapshots(force_refresh=force_refresh)
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
