"""
事件时间线API路由
"""
import time
from pathlib import Path
from flask import Blueprint, jsonify, request
from services.data_collector import get_collector
from state_store import MonitorStateStore

bp = Blueprint('events', __name__, url_prefix='/api/v2/events')

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STORE = MonitorStateStore(PROJECT_ROOT)


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


@bp.route('/visible-completion', methods=['POST'])
def visible_completion():
    """
    接收 OpenClaw 发送的 visible_completion 事件。
    
    当 OpenClaw 给用户发送回复后，调用此 API 通知健康监控系统任务已完成。
    """
    try:
        data = request.get_json()
        session_key = data.get('session_key')
        message = data.get('message', '')
        timestamp = data.get('timestamp')
        
        if not session_key:
            return jsonify({'success': False, 'message': '缺少 session_key'}), 400
        
        # 根据 session_key 查找对应的任务
        task = STORE.get_latest_task_for_session(session_key)
        if not task:
            return jsonify({'success': False, 'message': f'未找到 session_key={session_key} 对应的任务'}), 404
        
        task_id = task.get('task_id')
        if not task_id:
            return jsonify({'success': False, 'message': '任务缺少 task_id'}), 500
        
        # 检查任务是否已经终态
        core = STORE.get_core_closure_snapshot_for_task(task_id, allow_legacy_projection=False)
        is_terminal = bool(core.get('is_terminal'))
        
        if is_terminal:
            return jsonify({
                'success': True,
                'message': '任务已处于终态，visible_completion 事件已忽略',
                'task_id': task_id,
                'is_terminal': True
            })
        
        # 记录 visible_completion 事件（审计线索）
        payload = {
            'message': message,
            'timestamp': timestamp or int(time.time()),
            'source': 'api'
        }
        
        inserted = STORE.record_task_event(task_id, 'visible_completion', payload)

        # 同时写入 core terminal+delivery 事件，并立刻重建 workflow 投影
        root_task_id = (core.get('root_task') or {}).get('root_task_id') or task.get('root_task_id') or ''
        workflow_run_id = (core.get('current_workflow_run') or {}).get('workflow_run_id') or task.get('current_workflow_run_id') or ''
        event_ts = int(timestamp or time.time())
        if root_task_id and workflow_run_id:
            STORE.record_core_event({
                'event_id': f'receipt-adopted-completed:{task_id}:{event_ts}',
                'root_task_id': root_task_id,
                'workflow_run_id': workflow_run_id,
                'event_type': 'receipt_adopted_completed',
                'event_ts': event_ts,
                'event_seq': 1,
                'idempotency_key': f'receipt-adopted-completed:{task_id}:{event_ts}',
                'payload': {
                    'reason': 'visible_completion_ack',
                    'message': message,
                    'source': 'api',
                },
            })
            STORE.record_core_event({
                'event_id': f'delivery-confirmed:{task_id}:{event_ts}',
                'root_task_id': root_task_id,
                'workflow_run_id': workflow_run_id,
                'event_type': 'delivery_confirmed',
                'event_ts': event_ts,
                'event_seq': 2,
                'idempotency_key': f'delivery-confirmed:{task_id}:{event_ts}',
                'payload': {
                    'reason': 'channel_ack',
                    'confirmation_level': 'delivery_confirmed',
                    'message': message,
                    'source': 'api',
                },
            })
            STORE.rebuild_workflow_projection(workflow_run_id)
        
        # 更新任务状态
        STORE.update_task_fields(
            task_id,
            status='completed',
            current_stage='已完成',
            updated_at=int(time.time()),
            completed_at=int(time.time())
        )
        
        # 触发状态重新派生
        try:
            STORE.sync_legacy_task_projection(task_id)
        except Exception:
            pass
        
        return jsonify({
            'success': True,
            'message': 'visible_completion 事件已记录',
            'task_id': task_id,
            'inserted': inserted
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500