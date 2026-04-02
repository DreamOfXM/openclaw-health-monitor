#!/usr/bin/env python3
"""
Dead Letter Queue - 死信队列

投递失败兜底，不丢失已完成的工作成果。

这是自我进化系统的核心容错机制。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
SHARED_CONTEXT = BASE_DIR / "shared-context"
DLQ_DIR = SHARED_CONTEXT / "dead-letter-queue"
DLQ_FILE = DLQ_DIR / "dlq.jsonl"
RETRY_FILE = DLQ_DIR / "retry.jsonl"


class DeadLetterQueue:
    """死信队列"""
    
    def __init__(self):
        DLQ_DIR.mkdir(parents=True, exist_ok=True)
        self.dlq_file = DLQ_FILE
        self.retry_file = RETRY_FILE
    
    def _log(self, file: Path, entry: dict[str, Any]) -> None:
        """写入日志"""
        with open(file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def add(
        self,
        request_id: str,
        reason: str,
        original_message: dict[str, Any],
        attempts: int = 0,
    ) -> dict[str, Any]:
        """添加到死信队列"""
        now = int(time.time())
        entry = {
            "request_id": request_id,
            "reason": reason,
            "original_message": original_message,
            "attempts": attempts,
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
            "status": "pending",
        }
        self._log(self.dlq_file, entry)
        return entry
    
    def get_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取待处理项"""
        if not self.dlq_file.exists():
            return []
        
        items = []
        with open(self.dlq_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("status") == "pending":
                        items.append(entry)
                except json.JSONDecodeError:
                    continue
        
        return items[-limit:]
    
    def mark_retry(self, request_id: str) -> None:
        """标记为重试"""
        now = int(time.time())
        
        # 读取所有条目
        entries = []
        if self.dlq_file.exists():
            with open(self.dlq_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        
        # 更新状态
        for entry in entries:
            if entry.get("request_id") == request_id:
                entry["status"] = "retry"
                entry["retry_at"] = now
                break
        
        # 写回文件
        with open(self.dlq_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        
        # 记录重试
        self._log(self.retry_file, {
            "request_id": request_id,
            "retry_at": now,
            "iso_time": datetime.now().isoformat(),
        })
    
    def mark_resolved(self, request_id: str) -> None:
        """标记为已解决"""
        now = int(time.time())
        
        # 读取所有条目
        entries = []
        if self.dlq_file.exists():
            with open(self.dlq_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        
        # 更新状态
        for entry in entries:
            if entry.get("request_id") == request_id:
                entry["status"] = "resolved"
                entry["resolved_at"] = now
                break
        
        # 写回文件
        with open(self.dlq_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def get_stats(self) -> dict[str, int]:
        """获取统计信息"""
        if not self.dlq_file.exists():
            return {
                "total": 0,
                "pending": 0,
                "retry": 0,
                "resolved": 0,
            }
        
        stats = {
            "total": 0,
            "pending": 0,
            "retry": 0,
            "resolved": 0,
        }
        
        with open(self.dlq_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    stats["total"] += 1
                    status = entry.get("status", "pending")
                    if status in stats:
                        stats[status] += 1
                except json.JSONDecodeError:
                    continue
        
        return stats


def main():
    """主入口"""
    dlq = DeadLetterQueue()
    
    # 测试
    dlq.add("test-001", "投递失败", {"message": "测试消息"}, attempts=3)
    dlq.add("test-002", "超时", {"message": "测试消息2"}, attempts=5)
    
    print("Pending items:", len(dlq.get_pending()))
    
    dlq.mark_retry("test-001")
    dlq.mark_resolved("test-002")
    
    print("Stats:", dlq.get_stats())


if __name__ == "__main__":
    main()