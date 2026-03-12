"""Dashboard V2 主应用。"""
import os
import sys
from functools import lru_cache

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

for path in (BASE_DIR, PROJECT_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from flask import Flask, render_template, jsonify
from routes import (
    health_bp,
    metrics_bp,
    events_bp,
    tasks_bp,
    environments_bp,
    agents_bp,
    learnings_bp
)


@lru_cache(maxsize=1)
def _legacy_dashboard():
    import importlib

    return importlib.import_module("dashboard")

def create_app():
    """创建Flask应用"""
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )
    app.url_map.strict_slashes = False
    
    # 注册蓝图
    app.register_blueprint(health_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(environments_bp)
    app.register_blueprint(agents_bp)
    app.register_blueprint(learnings_bp)
    
    # 页面路由
    @app.route('/')
    def index():
        """总览视图（默认）"""
        return render_template('overview.html')
    
    @app.route('/explore')
    def explore():
        """详情视图"""
        return render_template('explore.html')
    
    @app.route('/manage')
    def manage():
        """管理视图"""
        return render_template('manage.html')
    
    @app.route('/api/v2/status')
    def api_status():
        """系统状态检查"""
        return jsonify({
            'status': 'ok',
            'version': '2.0.0',
            'timestamp': __import__('datetime').datetime.now().isoformat()
        })

    @app.route('/api/status')
    def compatibility_status():
        return _legacy_dashboard().api_status()

    @app.route('/api/task-registry')
    def compatibility_task_registry():
        return _legacy_dashboard().api_task_registry()

    @app.route('/api/learnings')
    def compatibility_learnings():
        return _legacy_dashboard().api_learnings()

    @app.route('/api/health-acceptance')
    def compatibility_health_acceptance():
        return _legacy_dashboard().api_health_acceptance()

    @app.route('/api/shared-state')
    def compatibility_shared_state():
        return _legacy_dashboard().api_shared_state()

    @app.route('/api/context-baseline')
    def compatibility_context_baseline():
        return _legacy_dashboard().api_context_baseline()

    @app.route('/api/config', methods=['POST'])
    def compatibility_config():
        return _legacy_dashboard().api_config()
    
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({
            'success': False,
            'error': '接口不存在'
        }), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        return jsonify({
            'success': False,
            'error': '服务器内部错误'
        }), 500
    
    return app


if __name__ == '__main__':
    app = create_app()
    host = os.environ.get('DASHBOARD_HOST', '127.0.0.1')
    port = int(os.environ.get('DASHBOARD_PORT', '8080'))
    debug = os.environ.get('DASHBOARD_DEBUG', '').lower() in {'1', 'true', 'yes'}
    print('Dashboard V2 starting...')
    print(f'Listening on http://{host}:{port}')
    app.run(host=host, port=port, debug=debug)
