"""
心跳监控 + Guardrail API 路由
"""
from flask import Blueprint, jsonify, request
import sys
import json
import re
from datetime import datetime
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from heartbeat_guardrail import TaskWatcher, HeartbeatPhase
from state_store import MonitorStateStore
import dashboard_backend as dashboard

bp = Blueprint('heartbeat', __name__, url_prefix='/api/v2/heartbeat')

# 全局状态
_store = None
_watcher = None

def get_watcher():
    """获取 TaskWatcher 单例"""
    global _store, _watcher
    if _store is None:
        _store = MonitorStateStore(Path(__file__).parent.parent.parent)
    if _watcher is None:
        _watcher = TaskWatcher(_store)
    return _watcher


def _heartbeat_file_effectively_empty(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True
    lines = [line.strip() for line in text.splitlines()]
    content = "\n".join(line for line in lines if line and not line.startswith("#")).strip()
    return not content


def _load_openclaw_heartbeat_status() -> dict:
    config = dashboard.load_config()
    active_env = dashboard.active_env_id(config)
    spec = dashboard.env_spec(active_env, config)
    openclaw_config_path = Path(spec["home"]) / "openclaw.json"
    payload = {}
    if openclaw_config_path.exists():
        try:
            payload = json.loads(openclaw_config_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    heartbeat_cfg = ((payload.get("agents") or {}).get("defaults") or {}).get("heartbeat") or {}
    every = str(heartbeat_cfg.get("every") or "")
    model = str(heartbeat_cfg.get("model") or "")
    target = str(heartbeat_cfg.get("target") or "none")
    agents = ((payload.get("agents") or {}).get("list") or [])
    workspaces = []
    for entry in agents:
        workspace = str((entry or {}).get("workspace") or "").strip()
        if workspace:
            workspaces.append(Path(workspace))
    if not workspaces:
        default_workspace = Path(spec["home"]) / "workspace"
        workspaces.append(default_workspace)

    workspace_items = []
    non_empty_count = 0
    for workspace in workspaces:
        heartbeat_file = workspace / "HEARTBEAT.md"
        empty = _heartbeat_file_effectively_empty(heartbeat_file)
        if not empty:
            non_empty_count += 1
        workspace_items.append(
            {
                "workspace": str(workspace),
                "heartbeat_file": str(heartbeat_file),
                "effective_empty": empty,
            }
        )

    log_path = Path(spec["home"]) / "logs" / "gateway.log"
    last_started_at = ""
    heartbeat_runs = 0
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            lines = []
        for line in reversed(lines):
            if "[heartbeat] started" in line:
                heartbeat_runs += 1
                if not last_started_at:
                    match = re.match(r"^(\S+)", line.strip())
                    if match:
                        last_started_at = match.group(1)
            elif "[heartbeat]" in line:
                heartbeat_runs += 1

    enabled = bool(every)
    if enabled and non_empty_count > 0:
        status = "ready"
        message = "OpenClaw heartbeat 已启用，并且存在有效的 HEARTBEAT.md 指令。"
    elif enabled:
        status = "warning"
        message = "OpenClaw heartbeat 已配置，但当前所有 HEARTBEAT.md 都是空模板，运行时会跳过实际 heartbeat 调用。"
    else:
        status = "disabled"
        message = "OpenClaw heartbeat 当前未配置 cadence。"

    return {
        "env_id": active_env,
        "status": status,
        "enabled": enabled,
        "every": every or "disabled",
        "model": model or "--",
        "target": target,
        "last_started_at": last_started_at,
        "heartbeat_runs": heartbeat_runs,
        "workspaces_total": len(workspace_items),
        "effective_prompt_count": non_empty_count,
        "workspaces": workspace_items,
        "message": message,
        "checked_at": datetime.now().isoformat(),
    }


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
        heartbeats = watcher.get_recent_heartbeats(limit)
        
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


@bp.route('/openclaw', methods=['GET'])
def get_openclaw_heartbeat():
    """获取 OpenClaw 自身 heartbeat 状态"""
    try:
        return jsonify({
            'success': True,
            'data': _load_openclaw_heartbeat_status(),
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
