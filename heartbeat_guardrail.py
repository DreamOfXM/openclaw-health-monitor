#!/usr/bin/env python3
"""
心跳检测 + Guardrail + 异步状态链

整合到健康助手，复用已有的 MonitorStateStore 和 Guardian 能力。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

# 复用健康助手已有的状态存储
from state_store import MonitorStateStore


class HeartbeatPhase(str, Enum):
    """心跳阶段"""
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    CALCULATION = "calculation"
    VERIFICATION = "verification"
    RISK_ASSESSMENT = "risk_assessment"
    IDLE = "idle"


class TaskState(str, Enum):
    """三态协议：任务状态"""
    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 执行中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 已失败
    BLOCKED = "blocked"       # 已阻塞
    TIMEOUT = "timeout"       # 已超时


class GuardrailAction(str, Enum):
    """Guardrail 动作"""
    ALLOW = "allow"           # 允许继续
    RETRY = "retry"           # 重试
    DOWNGRADE = "downgrade"   # 降级
    BLOCK = "block"           # 阻塞
    ESCALATE = "escalate"     # 升级通知


@dataclass
class HeartbeatConfig:
    """心跳配置"""
    intervals: dict[HeartbeatPhase, int] = field(default_factory=lambda: {
        HeartbeatPhase.PLANNING: 30,
        HeartbeatPhase.IMPLEMENTATION: 45,
        HeartbeatPhase.TESTING: 60,
        HeartbeatPhase.CALCULATION: 30,
        HeartbeatPhase.VERIFICATION: 30,
        HeartbeatPhase.RISK_ASSESSMENT: 30,
        HeartbeatPhase.IDLE: 300,
    })
    timeout_multipliers: dict[HeartbeatPhase, float] = field(default_factory=lambda: {
        HeartbeatPhase.PLANNING: 3.0,
        HeartbeatPhase.IMPLEMENTATION: 3.0,
        HeartbeatPhase.TESTING: 3.0,
        HeartbeatPhase.CALCULATION: 3.0,
        HeartbeatPhase.VERIFICATION: 3.0,
        HeartbeatPhase.RISK_ASSESSMENT: 3.0,
        HeartbeatPhase.IDLE: 2.0,
    })
    max_retries: int = 2
    retry_delays: list[int] = field(default_factory=lambda: [5, 15])


@dataclass
class Heartbeat:
    """心跳记录"""
    task_id: str
    session_key: str
    phase: HeartbeatPhase
    progress: int  # 0-100
    timestamp_ms: int
    message: Optional[str] = None
    error_code: Optional[str] = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_key": self.session_key,
            "phase": self.phase.value,
            "progress": self.progress,
            "timestamp_ms": self.timestamp_ms,
            "message": self.message,
            "error_code": self.error_code,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Heartbeat":
        return cls(
            task_id=data["task_id"],
            session_key=data["session_key"],
            phase=HeartbeatPhase(data["phase"]),
            progress=data["progress"],
            timestamp_ms=data["timestamp_ms"],
            message=data.get("message"),
            error_code=data.get("error_code"),
        )
    
    def format(self) -> str:
        """格式化为协议字符串"""
        parts = [
            f"task_id={self.task_id}",
            f"session={self.session_key}",
            f"phase={self.phase.value}",
            f"progress={self.progress}",
            f"timestamp={self.timestamp_ms}",
        ]
        if self.message:
            parts.append(f"message={self.message}")
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        return "HEARTBEAT: " + " | ".join(parts)


@dataclass
class GuardrailRule:
    """Guardrail 规则"""
    name: str
    condition: Callable[[dict[str, Any]], bool]
    action: GuardrailAction
    message: str
    max_attempts: int = 3
    

@dataclass
class StateTransition:
    """状态转换"""
    from_state: TaskState
    to_state: TaskState
    trigger: str
    guardrails: list[GuardrailRule] = field(default_factory=list)
    on_success: Optional[Callable] = None
    on_failure: Optional[Callable] = None


class HeartbeatMonitor:
    """心跳监控器 - 复用 MonitorStateStore"""
    
    def __init__(self, store: MonitorStateStore, config: HeartbeatConfig | None = None):
        self.store = store
        self.config = config or HeartbeatConfig()
        self._init_db()
    
    def _init_db(self):
        """初始化心跳表"""
        with self.store._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    message TEXT,
                    error_code TEXT,
                    created_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_heartbeats_task 
                ON heartbeats(task_id, timestamp_ms DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_heartbeats_session 
                ON heartbeats(session_key, timestamp_ms DESC)
            """)
    
    def record_heartbeat(self, heartbeat: Heartbeat) -> None:
        """记录心跳"""
        with self.store._connection() as conn:
            conn.execute("""
                INSERT INTO heartbeats 
                (task_id, session_key, phase, progress, timestamp_ms, message, error_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                heartbeat.task_id,
                heartbeat.session_key,
                heartbeat.phase.value,
                heartbeat.progress,
                heartbeat.timestamp_ms,
                heartbeat.message,
                heartbeat.error_code,
                int(time.time() * 1000),
            ))
    
    def get_last_heartbeat(self, task_id: str) -> Heartbeat | None:
        """获取最后一次心跳"""
        with self.store._connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM heartbeats 
                WHERE task_id = ? 
                ORDER BY timestamp_ms DESC 
                LIMIT 1
            """, (task_id,))
            row = cursor.fetchone()
            if row:
                return Heartbeat.from_dict({
                    "task_id": row[1],
                    "session_key": row[2],
                    "phase": row[3],
                    "progress": row[4],
                    "timestamp_ms": row[5],
                    "message": row[6],
                    "error_code": row[7],
                })
        return None
    
    def check_timeout(self, task_id: str) -> bool:
        """检查是否超时"""
        heartbeat = self.get_last_heartbeat(task_id)
        if not heartbeat:
            return True  # 没有心跳，视为超时
        
        phase = heartbeat.phase
        interval = self.config.intervals.get(phase, 30)
        multiplier = self.config.timeout_multipliers.get(phase, 3.0)
        timeout_ms = int(interval * multiplier * 1000)
        
        current_ms = int(time.time() * 1000)
        return current_ms - heartbeat.timestamp_ms > timeout_ms
    
    def get_timeout_tasks(self) -> list[dict[str, Any]]:
        """获取所有超时的任务"""
        timeout_tasks = []
        
        # 获取所有活跃任务
        active_tasks = self.store.list_active_tasks(limit=50)
        
        for task in active_tasks:
            task_id = task.get("task_id")
            if task_id and self.check_timeout(task_id):
                heartbeat = self.get_last_heartbeat(task_id)
                timeout_tasks.append({
                    "task_id": task_id,
                    "session_key": task.get("session_key"),
                    "phase": heartbeat.phase.value if heartbeat else "unknown",
                    "last_heartbeat_ms": heartbeat.timestamp_ms if heartbeat else 0,
                    "timeout_seconds": self._get_timeout_seconds(heartbeat.phase if heartbeat else HeartbeatPhase.IDLE),
                })
        
        return timeout_tasks
    
    def _get_timeout_seconds(self, phase: HeartbeatPhase) -> int:
        """获取超时秒数"""
        interval = self.config.intervals.get(phase, 30)
        multiplier = self.config.timeout_multipliers.get(phase, 3.0)
        return int(interval * multiplier)


class GuardrailEngine:
    """Guardrail 引擎 - 异步状态链"""
    
    def __init__(self, store: MonitorStateStore):
        self.store = store
        self.rules: list[GuardrailRule] = []
        self.transitions: dict[tuple[TaskState, str], StateTransition] = {}
        self._init_default_rules()
        self._init_default_transitions()
    
    def _init_default_rules(self):
        """初始化默认规则"""
        # 规则1：认证失败 -> 切换模型重试
        self.add_rule(GuardrailRule(
            name="auth_failure",
            condition=lambda ctx: ctx.get("error_code") in ["AUTH_401", "AUTH_403"],
            action=GuardrailAction.DOWNGRADE,
            message="认证失败，切换模型重试",
            max_attempts=2,
        ))
        
        # 规则2：模型错误 -> 切换模型
        self.add_rule(GuardrailRule(
            name="model_error",
            condition=lambda ctx: ctx.get("error_code") == "MODEL_ERROR",
            action=GuardrailAction.DOWNGRADE,
            message="模型错误，切换模型",
            max_attempts=2,
        ))
        
        # 规则3：超时 -> 重试
        self.add_rule(GuardrailRule(
            name="timeout",
            condition=lambda ctx: ctx.get("error_code") == "TIMEOUT",
            action=GuardrailAction.RETRY,
            message="任务超时，重试",
            max_attempts=2,
        ))
        
        # 规则4：派发失败 -> 重试
        self.add_rule(GuardrailRule(
            name="spawn_failed",
            condition=lambda ctx: ctx.get("error_code") == "SPAWN_FAILED",
            action=GuardrailAction.RETRY,
            message="派发失败，重试",
            max_attempts=3,
        ))
        
        # 规则5：会话不存在 -> 重新创建
        self.add_rule(GuardrailRule(
            name="session_not_found",
            condition=lambda ctx: ctx.get("error_code") == "SESSION_NOT_FOUND",
            action=GuardrailAction.RETRY,
            message="会话不存在，重新创建",
            max_attempts=1,
        ))
    
    def _init_default_transitions(self):
        """初始化默认状态转换"""
        # PENDING -> RUNNING
        self.add_transition(StateTransition(
            from_state=TaskState.PENDING,
            to_state=TaskState.RUNNING,
            trigger="start",
        ))
        
        # RUNNING -> COMPLETED
        self.add_transition(StateTransition(
            from_state=TaskState.RUNNING,
            to_state=TaskState.COMPLETED,
            trigger="complete",
        ))
        
        # RUNNING -> FAILED
        self.add_transition(StateTransition(
            from_state=TaskState.RUNNING,
            to_state=TaskState.FAILED,
            trigger="fail",
        ))
        
        # RUNNING -> BLOCKED
        self.add_transition(StateTransition(
            from_state=TaskState.RUNNING,
            to_state=TaskState.BLOCKED,
            trigger="block",
        ))
        
        # RUNNING -> TIMEOUT
        self.add_transition(StateTransition(
            from_state=TaskState.RUNNING,
            to_state=TaskState.TIMEOUT,
            trigger="timeout",
        ))
        
        # BLOCKED -> RUNNING (恢复)
        self.add_transition(StateTransition(
            from_state=TaskState.BLOCKED,
            to_state=TaskState.RUNNING,
            trigger="resume",
        ))
        
        # TIMEOUT -> RUNNING (重试)
        self.add_transition(StateTransition(
            from_state=TaskState.TIMEOUT,
            to_state=TaskState.RUNNING,
            trigger="retry",
        ))
    
    def add_rule(self, rule: GuardrailRule):
        """添加规则"""
        self.rules.append(rule)
    
    def add_transition(self, transition: StateTransition):
        """添加状态转换"""
        key = (transition.from_state, transition.trigger)
        self.transitions[key] = transition
    
    def evaluate(self, context: dict[str, Any]) -> GuardrailAction:
        """评估规则，返回动作"""
        for rule in self.rules:
            if rule.condition(context):
                attempts = context.get("attempts", 0)
                if attempts >= rule.max_attempts:
                    return GuardrailAction.BLOCK
                return rule.action
        return GuardrailAction.ALLOW
    
    def can_transition(self, from_state: TaskState, trigger: str) -> bool:
        """检查是否可以转换"""
        key = (from_state, trigger)
        return key in self.transitions
    
    def get_transition(self, from_state: TaskState, trigger: str) -> StateTransition | None:
        """获取状态转换"""
        key = (from_state, trigger)
        return self.transitions.get(key)
    
    def execute_transition(
        self, 
        task_id: str, 
        trigger: str, 
        context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """执行状态转换"""
        # 获取当前任务状态
        task = self.store.get_task(task_id)
        if not task:
            return {"success": False, "error": "task_not_found"}
        
        current_state = TaskState(task.get("status", "pending"))
        
        # 检查是否可以转换
        transition = self.get_transition(current_state, trigger)
        if not transition:
            return {
                "success": False, 
                "error": f"invalid_transition",
                "from_state": current_state.value,
                "trigger": trigger,
            }
        
        # 评估 Guardrail
        ctx = context or {}
        ctx["task_id"] = task_id
        ctx["from_state"] = current_state.value
        ctx["to_state"] = transition.to_state.value
        
        action = self.evaluate(ctx)
        
        if action == GuardrailAction.BLOCK:
            return {
                "success": False,
                "action": "blocked",
                "message": "Guardrail blocked the transition",
            }
        
        # 执行转换
        self.store.update_task_fields(task_id, status=transition.to_state.value)
        
        # 记录事件
        self.store.record_task_event(
            task_id,
            f"state_transition:{current_state.value}->{transition.to_state.value}",
            {"trigger": trigger, "action": action.value, **ctx}
        )
        
        # 执行回调
        if transition.on_success:
            try:
                transition.on_success(task_id, ctx)
            except Exception as e:
                pass
        
        return {
            "success": True,
            "from_state": current_state.value,
            "to_state": transition.to_state.value,
            "action": action.value,
        }


class TaskWatcher:
    """任务监控器 - 整合心跳和 Guardrail"""
    
    def __init__(self, store: MonitorStateStore, config: HeartbeatConfig | None = None):
        self.store = store
        self.heartbeat_monitor = HeartbeatMonitor(store, config)
        self.guardrail_engine = GuardrailEngine(store)
    
    def check_all_tasks(self) -> dict[str, Any]:
        """检查所有任务状态"""
        result = {
            "total_tasks": 0,
            "active_tasks": 0,
            "timeout_tasks": [],
            "blocked_tasks": [],
            "health_status": "healthy",
        }
        
        # 获取活跃任务
        active_tasks = self.store.list_active_tasks(limit=50)
        result["active_tasks"] = len(active_tasks)
        result["total_tasks"] = len(self.store.list_tasks(limit=100))
        
        # 检查超时
        timeout_tasks = self.heartbeat_monitor.get_timeout_tasks()
        result["timeout_tasks"] = timeout_tasks
        
        # 检查阻塞
        blocked_tasks = self.store.list_tasks(statuses=["blocked"], limit=20)
        result["blocked_tasks"] = blocked_tasks
        
        # 计算健康状态
        if timeout_tasks or blocked_tasks:
            result["health_status"] = "degraded"
        else:
            result["health_status"] = "healthy"
        
        return result
    
    def recover_timeout_task(self, task_id: str) -> dict[str, Any]:
        """恢复超时任务"""
        task = self.store.get_task(task_id)
        if not task:
            return {"success": False, "error": "task_not_found"}
        
        # 获取重试次数
        attempts = task.get("retry_count", 0)
        
        # 评估恢复策略
        context = {
            "task_id": task_id,
            "attempts": attempts,
            "error_code": "TIMEOUT",
        }
        
        action = self.guardrail_engine.evaluate(context)
        
        if action == GuardrailAction.RETRY:
            # 重试
            self.guardrail_engine.execute_transition(task_id, "retry", context)
            self.store.update_task_fields(task_id, retry_count=attempts + 1)
            return {"success": True, "action": "retry", "attempts": attempts + 1}
        
        elif action == GuardrailAction.DOWNGRADE:
            # 降级
            self.guardrail_engine.execute_transition(task_id, "retry", context)
            self.store.update_task_fields(task_id, retry_count=attempts + 1, downgraded=True)
            return {"success": True, "action": "downgrade", "attempts": attempts + 1}
        
        else:
            # 阻塞
            self.guardrail_engine.execute_transition(task_id, "block", context)
            return {"success": False, "action": "block", "message": "Max retries exceeded"}
    
    def get_observability_report(self) -> dict[str, Any]:
        """生成可观测性报告"""
        return {
            "timestamp": int(time.time() * 1000),
            "tasks": self.check_all_tasks(),
            "control_plane": self.store.summarize_control_plane(),
            "heartbeats": {
                "recent": self._get_recent_heartbeats(10),
            },
        }
    
    def _get_recent_heartbeats(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近的心跳"""
        with self.store._connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM heartbeats 
                ORDER BY timestamp_ms DESC 
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [
                {
                    "task_id": row[1],
                    "session_key": row[2],
                    "phase": row[3],
                    "progress": row[4],
                    "timestamp_ms": row[5],
                }
                for row in rows
            ]


# 便捷函数
def create_watcher(db_path: Path | None = None) -> TaskWatcher:
    """创建任务监控器"""
    if db_path is None:
        db_path = Path(__file__).parent / "data" / "monitor.db"
    store = MonitorStateStore(db_path.parent)
    return TaskWatcher(store)


if __name__ == "__main__":
    # 测试
    watcher = create_watcher()
    report = watcher.get_observability_report()
    print(json.dumps(report, indent=2, ensure_ascii=False))