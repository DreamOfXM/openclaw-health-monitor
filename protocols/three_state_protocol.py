#!/usr/bin/env python3
"""
三态协议 - request → confirmed → final → 静默

核心规则：
1. request: @对方 + ack_id + 期望动作 + 截止时间
2. confirmed: @发起方 + 相同 ack_id + 版本号/生效时间
3. final: @对方 + 相同 ack_id + 终态收敛（全线程仅 1 条）
→ 静默: final 后 → NO_REPLY（禁止礼貌性回复）

设计原则：
- 同一线程只允许一个 ack_id
- 超时不得重试
- 第二次超时降级到 shared-context/ 文件投递
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum


class ProtocolState(Enum):
    REQUEST = "request"
    CONFIRMED = "confirmed"
    FINAL = "final"
    SILENT = "silent"
    TIMEOUT = "timeout"
    ESCALATED = "escalated"


@dataclass
class ProtocolMessage:
    """三态协议消息"""
    ack_id: str
    state: ProtocolState
    source_agent: str
    target_agent: str
    action: str
    deadline: Optional[int] = None
    version: int = 1
    created_at: int = field(default_factory=lambda: int(time.time()))
    payload: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "ack_id": self.ack_id,
            "state": self.state.value,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "action": self.action,
            "deadline": self.deadline,
            "version": self.version,
            "created_at": self.created_at,
            "payload": self.payload,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProtocolMessage":
        return cls(
            ack_id=data["ack_id"],
            state=ProtocolState(data["state"]),
            source_agent=data["source_agent"],
            target_agent=data["target_agent"],
            action=data["action"],
            deadline=data.get("deadline"),
            version=data.get("version", 1),
            created_at=data.get("created_at", int(time.time())),
            payload=data.get("payload", {}),
        )


class ThreeStateProtocol:
    """三态协议管理器"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.protocols_dir = base_dir / "protocols"
        self.protocols_dir.mkdir(parents=True, exist_ok=True)
        self.active_file = self.protocols_dir / "active_protocols.jsonl"
        self.history_file = self.protocols_dir / "protocol_history.jsonl"
    
    def generate_ack_id(self, source: str, target: str, action: str) -> str:
        """生成唯一的 ack_id"""
        payload = f"{source}|{target}|{action}|{time.time()}"
        return f"ack-{hashlib.sha1(payload.encode()).hexdigest()[:12]}"
    
    def create_request(
        self,
        source_agent: str,
        target_agent: str,
        action: str,
        deadline_seconds: int = 3600,
        payload: Optional[dict[str, Any]] = None,
    ) -> ProtocolMessage:
        """创建 request 消息"""
        ack_id = self.generate_ack_id(source_agent, target_agent, action)
        deadline = int(time.time()) + deadline_seconds
        
        message = ProtocolMessage(
            ack_id=ack_id,
            state=ProtocolState.REQUEST,
            source_agent=source_agent,
            target_agent=target_agent,
            action=action,
            deadline=deadline,
            payload=payload or {},
        )
        
        self._record_protocol(message)
        return message
    
    def confirm(self, ack_id: str, target_agent: str) -> Optional[ProtocolMessage]:
        """确认请求"""
        request = self._get_active_protocol(ack_id)
        if not request:
            return None
        
        if request.state != ProtocolState.REQUEST:
            return None
        
        confirmed = ProtocolMessage(
            ack_id=ack_id,
            state=ProtocolState.CONFIRMED,
            source_agent=request.target_agent,
            target_agent=request.source_agent,
            action=request.action,
            deadline=request.deadline,
            version=request.version + 1,
            payload={"confirmed_at": int(time.time())},
        )
        
        self._update_protocol(confirmed)
        return confirmed
    
    def finalize(
        self,
        ack_id: str,
        source_agent: str,
        result: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Optional[ProtocolMessage]:
        """终态收敛"""
        protocol = self._get_active_protocol(ack_id)
        if not protocol:
            return None
        
        if protocol.state not in (ProtocolState.REQUEST, ProtocolState.CONFIRMED):
            return None
        
        final = ProtocolMessage(
            ack_id=ack_id,
            state=ProtocolState.FINAL,
            source_agent=source_agent,
            target_agent=protocol.source_agent if source_agent == protocol.target_agent else protocol.target_agent,
            action=protocol.action,
            version=protocol.version + 1,
            payload={
                "result": result,
                "finalized_at": int(time.time()),
                **(payload or {}),
            },
        )
        
        self._update_protocol(final)
        self._archive_protocol(final)
        return final
    
    def check_timeout(self) -> list[dict[str, Any]]:
        """检查超时的协议"""
        now = int(time.time())
        timeouts = []
        
        if not self.active_file.exists():
            return timeouts
        
        for line in self.active_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                protocol = ProtocolMessage.from_dict(json.loads(line))
                if protocol.state in (ProtocolState.REQUEST, ProtocolState.CONFIRMED):
                    if protocol.deadline and now > protocol.deadline:
                        timeouts.append({
                            "ack_id": protocol.ack_id,
                            "state": protocol.state.value,
                            "source_agent": protocol.source_agent,
                            "target_agent": protocol.target_agent,
                            "action": protocol.action,
                            "timeout_seconds": now - protocol.deadline,
                        })
            except Exception:
                continue
        
        return timeouts
    
    def escalate_to_file(self, ack_id: str) -> bool:
        """超时降级到文件投递"""
        protocol = self._get_active_protocol(ack_id)
        if not protocol:
            return False
        
        escalated = ProtocolMessage(
            ack_id=ack_id,
            state=ProtocolState.ESCALATED,
            source_agent=protocol.source_agent,
            target_agent=protocol.target_agent,
            action=protocol.action,
            version=protocol.version + 1,
            payload={
                "escalated_at": int(time.time()),
                "escalation_reason": "timeout",
            },
        )
        
        self._update_protocol(escalated)
        
        # 写入 shared-context 文件
        shared_context = Path.home() / ".openclaw" / "shared-context"
        shared_context.mkdir(parents=True, exist_ok=True)
        escalation_file = shared_context / f"escalation-{ack_id}.json"
        escalation_file.write_text(
            json.dumps(escalated.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        return True
    
    def _record_protocol(self, message: ProtocolMessage) -> None:
        """记录新协议"""
        with open(self.active_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
    
    def _update_protocol(self, message: ProtocolMessage) -> None:
        """更新协议状态"""
        if not self.active_file.exists():
            return
        
        lines = []
        for line in self.active_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                protocol = ProtocolMessage.from_dict(json.loads(line))
                if protocol.ack_id == message.ack_id:
                    lines.append(json.dumps(message.to_dict(), ensure_ascii=False))
                else:
                    lines.append(line)
            except Exception:
                lines.append(line)
        
        self.active_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    def _archive_protocol(self, message: ProtocolMessage) -> None:
        """归档协议"""
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        
        # 从活跃列表中移除
        if not self.active_file.exists():
            return
        
        lines = []
        for line in self.active_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                protocol = ProtocolMessage.from_dict(json.loads(line))
                if protocol.ack_id != message.ack_id:
                    lines.append(line)
            except Exception:
                lines.append(line)
        
        self.active_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    def _get_active_protocol(self, ack_id: str) -> Optional[ProtocolMessage]:
        """获取活跃协议"""
        if not self.active_file.exists():
            return None
        
        for line in self.active_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                protocol = ProtocolMessage.from_dict(json.loads(line))
                if protocol.ack_id == ack_id:
                    return protocol
            except Exception:
                continue
        
        return None


def should_silence(ack_id: str, protocols_dir: Path) -> bool:
    """检查是否应该静默（final 后禁止礼貌性回复）"""
    history_file = protocols_dir / "protocol_history.jsonl"
    if not history_file.exists():
        return False
    
    for line in history_file.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            protocol = json.loads(line)
            if protocol.get("ack_id") == ack_id and protocol.get("state") == "final":
                return True
        except Exception:
            continue
    
    return False