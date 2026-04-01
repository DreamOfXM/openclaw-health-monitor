#!/usr/bin/env python3
"""
异步状态链 - 11 态请求生命周期

状态流转：
created → accepted → in_progress → completed → delivered
                    ↓
              failed/timeout → escalated

核心设计：
- completed ≠ delivered
- timeout ≠ failed（支持 ambiguous_success）
- 每次状态变更写 events.jsonl 审计
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum


class RequestState(Enum):
    CREATED = "created"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DELIVERED = "delivered"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ESCALATED = "escalated"
    AMBIGUOUS_SUCCESS = "ambiguous_success"
    CANCELLED = "cancelled"
    RECOVERED = "recovered"


@dataclass
class StateTransition:
    """状态变更事件"""
    request_id: str
    from_state: RequestState
    to_state: RequestState
    reason: str
    actor: str
    timestamp: int
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "reason": self.reason,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateTransition":
        return cls(
            request_id=data["request_id"],
            from_state=RequestState(data["from_state"]),
            to_state=RequestState(data["to_state"]),
            reason=data["reason"],
            actor=data["actor"],
            timestamp=data["timestamp"],
            metadata=data.get("metadata", {}),
        )


@dataclass
class AsyncRequest:
    """异步请求"""
    request_id: str
    source_agent: str
    target_agent: str
    intent: str
    current_state: RequestState
    created_at: int
    updated_at: int
    completed_at: Optional[int] = None
    delivered_at: Optional[int] = None
    deadline: Optional[int] = None
    payload: dict[str, Any] = field(default_factory=dict)
    state_history: list[StateTransition] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "intent": self.intent,
            "current_state": self.current_state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "delivered_at": self.delivered_at,
            "deadline": self.deadline,
            "payload": self.payload,
            "state_history": [t.to_dict() for t in self.state_history],
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AsyncRequest":
        return cls(
            request_id=data["request_id"],
            source_agent=data["source_agent"],
            target_agent=data["target_agent"],
            intent=data["intent"],
            current_state=RequestState(data["current_state"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            completed_at=data.get("completed_at"),
            delivered_at=data.get("delivered_at"),
            deadline=data.get("deadline"),
            payload=data.get("payload", {}),
            state_history=[StateTransition.from_dict(t) for t in data.get("state_history", [])],
        )


class AsyncStateChain:
    """异步状态链管理器"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.state_dir = base_dir / "async-state-chain"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.requests_file = self.state_dir / "requests.jsonl"
        self.events_file = self.state_dir / "events.jsonl"
        self.pending_deliveries_dir = self.state_dir / "pending-deliveries"
        self.pending_deliveries_dir.mkdir(parents=True, exist_ok=True)
    
    def create_request(
        self,
        source_agent: str,
        target_agent: str,
        intent: str,
        payload: dict[str, Any],
        deadline_seconds: int = 3600,
    ) -> AsyncRequest:
        """创建异步请求"""
        request_id = f"req-{int(time.time())}-{source_agent[:4]}-{target_agent[:4]}"
        now = int(time.time())
        
        request = AsyncRequest(
            request_id=request_id,
            source_agent=source_agent,
            target_agent=target_agent,
            intent=intent,
            current_state=RequestState.CREATED,
            created_at=now,
            updated_at=now,
            deadline=now + deadline_seconds,
            payload=payload,
        )
        
        # 记录初始状态变更
        self._record_transition(
            request_id,
            RequestState.CREATED,
            RequestState.CREATED,
            "request_created",
            "system",
            now,
        )
        
        self._write_request(request)
        return request
    
    def accept(self, request_id: str, actor: str) -> bool:
        """接受请求"""
        request = self._get_request(request_id)
        if not request or request.current_state != RequestState.CREATED:
            return False
        
        self._transition(request, RequestState.ACCEPTED, "request_accepted", actor)
        return True
    
    def start_work(self, request_id: str, actor: str) -> bool:
        """开始工作"""
        request = self._get_request(request_id)
        if not request or request.current_state != RequestState.ACCEPTED:
            return False
        
        self._transition(request, RequestState.IN_PROGRESS, "work_started", actor)
        return True
    
    def complete(
        self,
        request_id: str,
        actor: str,
        result: dict[str, Any],
    ) -> bool:
        """完成工作"""
        request = self._get_request(request_id)
        if not request or request.current_state not in (
            RequestState.IN_PROGRESS,
            RequestState.ACCEPTED,
        ):
            return False
        
        request.completed_at = int(time.time())
        request.payload["result"] = result
        self._transition(request, RequestState.COMPLETED, "work_completed", actor)
        
        # 写入待投递队列
        self._mark_pending_delivery(request)
        return True
    
    def deliver(self, request_id: str, actor: str) -> bool:
        """确认送达"""
        request = self._get_request(request_id)
        if not request or request.current_state != RequestState.COMPLETED:
            return False
        
        request.delivered_at = int(time.time())
        self._transition(request, RequestState.DELIVERED, "delivery_confirmed", actor)
        
        # 从待投递队列移除
        self._clear_pending_delivery(request)
        return True
    
    def fail(
        self,
        request_id: str,
        actor: str,
        reason: str,
    ) -> bool:
        """标记失败"""
        request = self._get_request(request_id)
        if not request or request.current_state not in (
            RequestState.CREATED,
            RequestState.ACCEPTED,
            RequestState.IN_PROGRESS,
        ):
            return False
        
        self._transition(request, RequestState.FAILED, reason, actor)
        return True
    
    def timeout(self, request_id: str) -> bool:
        """标记超时"""
        request = self._get_request(request_id)
        if not request or request.current_state in (
            RequestState.COMPLETED,
            RequestState.DELIVERED,
            RequestState.FAILED,
            RequestState.ESCALATED,
        ):
            return False
        
        self._transition(
            request,
            RequestState.TIMEOUT,
            "request_timeout",
            "system",
            {"deadline": request.deadline},
        )
        return True
    
    def escalate(
        self,
        request_id: str,
        actor: str,
        reason: str,
    ) -> bool:
        """升级处理"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        self._transition(request, RequestState.ESCALATED, reason, actor)
        return True
    
    def mark_ambiguous_success(
        self,
        request_id: str,
        actor: str,
        reason: str,
    ) -> bool:
        """标记模糊成功（超时但可能已成功）"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        self._transition(
            request,
            RequestState.AMBIGUOUS_SUCCESS,
            reason,
            actor,
        )
        return True
    
    def get_pending_deliveries(self) -> list[AsyncRequest]:
        """获取待投递的完成结果"""
        requests = []
        
        if not self.pending_deliveries_dir.exists():
            return requests
        
        for file in self.pending_deliveries_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                request = AsyncRequest.from_dict(data)
                requests.append(request)
            except Exception:
                continue
        
        return requests
    
    def get_statistics(self) -> dict[str, Any]:
        """获取状态链统计"""
        by_state: dict[str, int] = {}
        
        if self.requests_file.exists():
            for line in self.requests_file.read_text(encoding="utf-8").strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    state = data.get("current_state", "unknown")
                    by_state[state] = by_state.get(state, 0) + 1
                except Exception:
                    continue
        
        return {
            "by_state": by_state,
            "pending_deliveries": len(self.get_pending_deliveries()),
        }
    
    def _transition(
        self,
        request: AsyncRequest,
        to_state: RequestState,
        reason: str,
        actor: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """状态流转"""
        from_state = request.current_state
        now = int(time.time())
        
        request.current_state = to_state
        request.updated_at = now
        request.state_history.append(
            StateTransition(
                request_id=request.request_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                actor=actor,
                timestamp=now,
                metadata=metadata or {},
            )
        )
        
        self._write_request(request)
        self._record_transition(
            request.request_id,
            from_state,
            to_state,
            reason,
            actor,
            now,
            metadata,
        )
    
    def _write_request(self, request: AsyncRequest) -> None:
        """写入请求"""
        if not self.requests_file.exists():
            self.requests_file.write_text("", encoding="utf-8")
        
        lines = []
        found = False
        for line in self.requests_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("request_id") == request.request_id:
                    lines.append(json.dumps(request.to_dict(), ensure_ascii=False))
                    found = True
                else:
                    lines.append(line)
            except Exception:
                lines.append(line)
        
        if not found:
            lines.append(json.dumps(request.to_dict(), ensure_ascii=False))
        
        self.requests_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    def _get_request(self, request_id: str) -> Optional[AsyncRequest]:
        """获取请求"""
        if not self.requests_file.exists():
            return None
        
        for line in self.requests_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("request_id") == request_id:
                    return AsyncRequest.from_dict(data)
            except Exception:
                continue
        
        return None
    
    def _record_transition(
        self,
        request_id: str,
        from_state: RequestState,
        to_state: RequestState,
        reason: str,
        actor: str,
        timestamp: int,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """记录状态变更事件"""
        event = StateTransition(
            request_id=request_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            actor=actor,
            timestamp=timestamp,
            metadata=metadata or {},
        )
        
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
    
    def _mark_pending_delivery(self, request: AsyncRequest) -> None:
        """标记待投递"""
        file = self.pending_deliveries_dir / f"{request.request_id}.json"
        file.write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    
    def _clear_pending_delivery(self, request: AsyncRequest) -> None:
        """清除待投递标记"""
        file = self.pending_deliveries_dir / f"{request.request_id}.json"
        if file.exists():
            file.unlink()