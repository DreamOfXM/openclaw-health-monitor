"""
WebSocket 连接管理器
支持实时推送健康状态更新
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
from queue import Queue
import logging

logger = logging.getLogger(__name__)


class WebSocketManager:
    """WebSocket连接管理器"""
    
    _instance: Optional[WebSocketManager] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> WebSocketManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self._connections: Dict[str, Set] = defaultdict(set)
        self._message_queues: Dict[str, Queue] = defaultdict(Queue)
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._running = True
        self._broadcast_thread: Optional[threading.Thread] = None
    
    def register_connection(self, channel: str, ws) -> None:
        """注册WebSocket连接"""
        with self._lock:
            self._connections[channel].add(ws)
            logger.debug(f"WebSocket connected to channel: {channel}, total: {len(self._connections[channel])}")
    
    def unregister_connection(self, channel: str, ws) -> None:
        """注销WebSocket连接"""
        with self._lock:
            self._connections[channel].discard(ws)
            logger.debug(f"WebSocket disconnected from channel: {channel}, remaining: {len(self._connections[channel])}")
    
    def subscribe(self, channel: str, callback: Callable[[Dict], None]) -> None:
        """订阅频道消息"""
        self._subscribers[channel].append(callback)
    
    def broadcast(self, channel: str, message: Dict[str, Any]) -> int:
        """广播消息到频道所有连接"""
        sent = 0
        dead_connections = set()
        
        with self._lock:
            connections = list(self._connections.get(channel, set()))
        
        for ws in connections:
            try:
                ws.send(json.dumps(message))
                sent += 1
            except Exception as e:
                logger.debug(f"Failed to send to connection: {e}")
                dead_connections.add(ws)
        
        if dead_connections:
            with self._lock:
                for ws in dead_connections:
                    self._connections[channel].discard(ws)
        
        return sent
    
    def broadcast_all(self, message: Dict[str, Any]) -> int:
        """广播消息到所有连接"""
        total = 0
        for channel in list(self._connections.keys()):
            total += self.broadcast(channel, message)
        return total
    
    def push_health_update(self, data: Dict[str, Any]) -> None:
        """推送健康状态更新"""
        message = {
            "type": "health_update",
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        self.broadcast("health", message)
    
    def push_metrics_update(self, data: Dict[str, Any]) -> None:
        """推送系统指标更新"""
        message = {
            "type": "metrics_update",
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        self.broadcast("metrics", message)
    
    def push_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """推送事件通知"""
        message = {
            "type": "event",
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        self.broadcast("events", message)
    
    def push_alert(self, level: str, title: str, message: str, details: Optional[Dict] = None) -> None:
        """推送告警通知"""
        alert = {
            "type": "alert",
            "level": level,
            "title": title,
            "message": message,
            "details": details or {},
            "timestamp": datetime.now().isoformat()
        }
        self.broadcast_all(alert)
    
    def get_connection_count(self, channel: Optional[str] = None) -> int:
        """获取连接数"""
        if channel:
            return len(self._connections.get(channel, set()))
        return sum(len(conns) for conns in self._connections.values())
    
    def start_background_pusher(self, get_data_func: Callable[[], Dict[str, Any]], channel: str, interval: float = 5.0) -> None:
        """启动后台推送线程"""
        def pusher():
            while self._running:
                try:
                    data = get_data_func()
                    if data:
                        self.broadcast(channel, {
                            "type": f"{channel}_update",
                            "timestamp": datetime.now().isoformat(),
                            "data": data
                        })
                except Exception as e:
                    logger.error(f"Error in background pusher: {e}")
                time.sleep(interval)
        
        thread = threading.Thread(target=pusher, daemon=True, name=f"ws-pusher-{channel}")
        thread.start()
    
    def stop(self) -> None:
        """停止管理器"""
        self._running = False


def get_ws_manager() -> WebSocketManager:
    """获取WebSocket管理器单例"""
    return WebSocketManager()