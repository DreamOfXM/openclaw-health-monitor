#!/usr/bin/env python3
"""
Dead Letter Queue (DLQ) - 投递失败兜底

核心机制：
1. 投递失败的任务进入 DLQ
2. 保留完整上下文，不丢失已完成的工作成果
3. 支持重试和人工干预
4. 定期清理过期条目

设计原则：
- 不丢失任何已完成的工作
- 失败原因可追溯
- 支持自动重试和人工恢复
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field


@dataclass
class DeadLetterEntry:
    """死信队列条目"""
    entry_id: str
    original_task_id: str
    source_agent: str
    target_agent: str
    action: str
    failure_reason: str
    failure_count: int
    last_failure_at: int
    created_at: int
    payload: dict[str, Any] = field(default_factory=dict)
    recovery_attempts: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"  # pending, recovered, abandoned
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "original_task_id": self.original_task_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "action": self.action,
            "failure_reason": self.failure_reason,
            "failure_count": self.failure_count,
            "last_failure_at": self.last_failure_at,
            "created_at": self.created_at,
            "payload": self.payload,
            "recovery_attempts": self.recovery_attempts,
            "status": self.status,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeadLetterEntry":
        return cls(
            entry_id=data["entry_id"],
            original_task_id=data["original_task_id"],
            source_agent=data["source_agent"],
            target_agent=data["target_agent"],
            action=data["action"],
            failure_reason=data["failure_reason"],
            failure_count=data["failure_count"],
            last_failure_at=data["last_failure_at"],
            created_at=data["created_at"],
            payload=data.get("payload", {}),
            recovery_attempts=data.get("recovery_attempts", []),
            status=data.get("status", "pending"),
        )


class DeadLetterQueue:
    """死信队列管理器"""
    
    MAX_RETRIES = 3
    RETRY_INTERVALS = [60, 300, 900]  # 1分钟, 5分钟, 15分钟
    EXPIRY_DAYS = 7
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.dlq_dir = base_dir / "dead-letter-queue"
        self.dlq_dir.mkdir(parents=True, exist_ok=True)
        self.dlq_file = self.dlq_dir / "dlq.jsonl"
        self.recovered_file = self.dlq_dir / "recovered.jsonl"
    
    def add_entry(
        self,
        task_id: str,
        source_agent: str,
        target_agent: str,
        action: str,
        failure_reason: str,
        payload: dict[str, Any],
    ) -> DeadLetterEntry:
        """添加死信条目"""
        entry_id = f"dlq-{int(time.time())}-{task_id[:8]}"
        
        entry = DeadLetterEntry(
            entry_id=entry_id,
            original_task_id=task_id,
            source_agent=source_agent,
            target_agent=target_agent,
            action=action,
            failure_reason=failure_reason,
            failure_count=1,
            last_failure_at=int(time.time()),
            created_at=int(time.time()),
            payload=payload,
        )
        
        self._write_entry(entry)
        return entry
    
    def get_pending_entries(self) -> list[DeadLetterEntry]:
        """获取待处理的死信条目"""
        entries = []
        
        if not self.dlq_file.exists():
            return entries
        
        for line in self.dlq_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                entry = DeadLetterEntry.from_dict(json.loads(line))
                if entry.status == "pending":
                    entries.append(entry)
            except Exception:
                continue
        
        return entries
    
    def get_retryable_entries(self) -> list[DeadLetterEntry]:
        """获取可重试的死信条目"""
        now = int(time.time())
        retryable = []
        
        for entry in self.get_pending_entries():
            if entry.failure_count >= self.MAX_RETRIES:
                continue
            
            # 检查重试间隔
            retry_index = min(entry.failure_count - 1, len(self.RETRY_INTERVALS) - 1)
            retry_interval = self.RETRY_INTERVALS[retry_index]
            
            if now - entry.last_failure_at >= retry_interval:
                retryable.append(entry)
        
        return retryable
    
    def record_recovery_attempt(
        self,
        entry_id: str,
        success: bool,
        details: dict[str, Any],
    ) -> bool:
        """记录恢复尝试"""
        entry = self._get_entry(entry_id)
        if not entry:
            return False
        
        attempt = {
            "attempted_at": int(time.time()),
            "success": success,
            "details": details,
        }
        entry.recovery_attempts.append(attempt)
        
        if success:
            entry.status = "recovered"
            self._archive_entry(entry)
            return True
        else:
            entry.failure_count += 1
            entry.last_failure_at = int(time.time())
            
            if entry.failure_count >= self.MAX_RETRIES:
                entry.status = "abandoned"
                self._archive_entry(entry)
            else:
                self._update_entry(entry)
        
        return False
    
    def cleanup_expired(self) -> int:
        """清理过期条目"""
        now = int(time.time())
        expiry_seconds = self.EXPIRY_DAYS * 86400
        cleaned = 0
        
        if not self.dlq_file.exists():
            return cleaned
        
        remaining = []
        for line in self.dlq_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                entry = DeadLetterEntry.from_dict(json.loads(line))
                if now - entry.created_at > expiry_seconds:
                    cleaned += 1
                else:
                    remaining.append(line)
            except Exception:
                continue
        
        self.dlq_file.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        return cleaned
    
    def get_statistics(self) -> dict[str, Any]:
        """获取 DLQ 统计"""
        entries = self.get_pending_entries()
        
        by_reason: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        
        for entry in entries:
            by_reason[entry.failure_reason] = by_reason.get(entry.failure_reason, 0) + 1
            by_agent[entry.source_agent] = by_agent.get(entry.source_agent, 0) + 1
        
        return {
            "total_pending": len(entries),
            "by_reason": by_reason,
            "by_agent": by_agent,
            "retryable": len(self.get_retryable_entries()),
        }
    
    def _write_entry(self, entry: DeadLetterEntry) -> None:
        """写入条目"""
        with open(self.dlq_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    
    def _update_entry(self, entry: DeadLetterEntry) -> None:
        """更新条目"""
        if not self.dlq_file.exists():
            return
        
        lines = []
        for line in self.dlq_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                existing = DeadLetterEntry.from_dict(json.loads(line))
                if existing.entry_id == entry.entry_id:
                    lines.append(json.dumps(entry.to_dict(), ensure_ascii=False))
                else:
                    lines.append(line)
            except Exception:
                lines.append(line)
        
        self.dlq_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    def _archive_entry(self, entry: DeadLetterEntry) -> None:
        """归档条目"""
        with open(self.recovered_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        
        # 从 DLQ 中移除
        if not self.dlq_file.exists():
            return
        
        lines = []
        for line in self.dlq_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                existing = DeadLetterEntry.from_dict(json.loads(line))
                if existing.entry_id != entry.entry_id:
                    lines.append(line)
            except Exception:
                lines.append(line)
        
        self.dlq_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    def _get_entry(self, entry_id: str) -> Optional[DeadLetterEntry]:
        """获取条目"""
        if not self.dlq_file.exists():
            return None
        
        for line in self.dlq_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                entry = DeadLetterEntry.from_dict(json.loads(line))
                if entry.entry_id == entry_id:
                    return entry
            except Exception:
                continue
        
        return None