#!/usr/bin/env python3
"""
Completion Bus - 异步完成投递总线

功能：
1. producer-consumer 模式
2. 先 completed 落盘，后 delivered 确认
3. 投递失败进入死信队列

这是自我进化系统的核心组件：
- 解决 completed ≠ delivered 问题
- 不丢失已完成的工作成果
- 支持重试和兜底

使用：
    from completion_bus import CompletionBus
    
    bus = CompletionBus()
    bus.publish_completion(task_id, result)
    bus.consume_deliveries()
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
SHARED_CONTEXT = BASE_DIR / "shared-context"
AGENT_REQUESTS_DIR = SHARED_CONTEXT / "agent-requests"
PENDING_DELIVERIES_DIR = AGENT_REQUESTS_DIR / "pending-deliveries"
REQUESTS_FILE = AGENT_REQUESTS_DIR / "requests.jsonl"
EVENTS_FILE = AGENT_REQUESTS_DIR / "events.jsonl"
CONSUMED_FILE = AGENT_REQUESTS_DIR / "consumed.jsonl"


class CompletionBus:
    """异步完成投递总线"""
    
    def __init__(self):
        AGENT_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_DELIVERIES_DIR.mkdir(parents=True, exist_ok=True)
        self.requests_file = REQUESTS_FILE
        self.events_file = EVENTS_FILE
        self.consumed_file = CONSUMED_FILE
        self.pending_dir = PENDING_DELIVERIES_DIR
    
    def _log_event(self, event_type: str, request_id: str, details: dict[str, Any]) -> None:
        """写入事件日志"""
        entry = {
            "timestamp": int(time.time()),
            "iso_time": datetime.now().isoformat(),
            "event_type": event_type,
            "request_id": request_id,
            "details": details,
        }
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def publish_completion(
        self,
        request_id: str,
        source_agent: str,
        target_agent: str,
        result: dict[str, Any],
        delivery_channel: str = "feishu",
    ) -> dict[str, Any]:
        """发布完成结果"""
        now = int(time.time())
        
        # 创建投递记录
        delivery = {
            "request_id": request_id,
            "source_agent": source_agent,
            "target_agent": target_agent,
            "result": result,
            "delivery_channel": delivery_channel,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "attempts": 0,
            "last_attempt_at": None,
            "delivered_at": None,
        }
        
        # 写入 pending-deliveries
        delivery_file = self.pending_dir / f"{request_id}.json"
        with open(delivery_file, "w", encoding="utf-8") as f:
            json.dump(delivery, f, ensure_ascii=False, indent=2)
        
        # 写入事件日志
        self._log_event("completion_published", request_id, {
            "source_agent": source_agent,
            "target_agent": target_agent,
            "delivery_channel": delivery_channel,
        })
        
        return delivery
    
    def get_pending_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取待投递的完成结果"""
        deliveries = []
        
        for file in sorted(self.pending_dir.glob("*.json"))[:limit]:
            try:
                with open(file, "r", encoding="utf-8") as f:
                    delivery = json.load(f)
                    if delivery.get("status") == "pending":
                        deliveries.append(delivery)
            except (json.JSONDecodeError, IOError):
                continue
        
        return deliveries
    
    def mark_delivered(self, request_id: str) -> None:
        """标记为已投递"""
        now = int(time.time())
        
        delivery_file = self.pending_dir / f"{request_id}.json"
        if not delivery_file.exists():
            return
        
        with open(delivery_file, "r", encoding="utf-8") as f:
            delivery = json.load(f)
        
        delivery["status"] = "delivered"
        delivery["delivered_at"] = now
        delivery["updated_at"] = now
        
        with open(delivery_file, "w", encoding="utf-8") as f:
            json.dump(delivery, f, ensure_ascii=False, indent=2)
        
        # 移动到 consumed
        with open(self.consumed_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(delivery, ensure_ascii=False) + "\n")
        
        # 删除 pending 文件
        delivery_file.unlink()
        
        # 写入事件日志
        self._log_event("delivery_confirmed", request_id, {
            "delivered_at": now,
        })
    
    def mark_failed(self, request_id: str, error: str) -> None:
        """标记为投递失败"""
        now = int(time.time())
        
        delivery_file = self.pending_dir / f"{request_id}.json"
        if not delivery_file.exists():
            return
        
        with open(delivery_file, "r", encoding="utf-8") as f:
            delivery = json.load(f)
        
        delivery["status"] = "failed"
        delivery["last_error"] = error
        delivery["updated_at"] = now
        delivery["attempts"] = delivery.get("attempts", 0) + 1
        delivery["last_attempt_at"] = now
        
        with open(delivery_file, "w", encoding="utf-8") as f:
            json.dump(delivery, f, ensure_ascii=False, indent=2)
        
        # 写入事件日志
        self._log_event("delivery_failed", request_id, {
            "error": error,
            "attempts": delivery["attempts"],
        })
    
    def consume_deliveries(self, max_attempts: int = 3) -> dict[str, int]:
        """消费待投递的完成结果"""
        result = {
            "processed": 0,
            "delivered": 0,
            "failed": 0,
            "dlq": 0,
        }
        
        deliveries = self.get_pending_deliveries()
        
        for delivery in deliveries:
            request_id = delivery.get("request_id")
            if not request_id:
                continue
            
            result["processed"] += 1
            
            # 检查重试次数
            attempts = delivery.get("attempts", 0)
            if attempts >= max_attempts:
                # 进入死信队列
                self.mark_dlq(request_id, "max_attempts_exceeded")
                result["dlq"] += 1
                continue
            
            # 尝试投递（这里需要具体的投递逻辑）
            try:
                # TODO: 实际投递逻辑
                # 例如：调用 feishu API 发送消息
                self.mark_delivered(request_id)
                result["delivered"] += 1
            except Exception as e:
                self.mark_failed(request_id, str(e))
                result["failed"] += 1
        
        return result
    
    def mark_dlq(self, request_id: str, reason: str) -> None:
        """标记为进入死信队列"""
        now = int(time.time())
        
        delivery_file = self.pending_dir / f"{request_id}.json"
        if not delivery_file.exists():
            return
        
        with open(delivery_file, "r", encoding="utf-8") as f:
            delivery = json.load(f)
        
        delivery["status"] = "dlq"
        delivery["dlq_reason"] = reason
        delivery["dlq_at"] = now
        delivery["updated_at"] = now
        
        # 重命名为 .dlq
        dlq_file = self.pending_dir / f"{request_id}.dlq"
        with open(dlq_file, "w", encoding="utf-8") as f:
            json.dump(delivery, f, ensure_ascii=False, indent=2)
        
        delivery_file.unlink()
        
        # 写入事件日志
        self._log_event("delivery_dlq_entered", request_id, {
            "reason": reason,
            "dlq_at": now,
        })


def main():
    """主入口（用于 cron 调用）"""
    bus = CompletionBus()
    result = bus.consume_deliveries()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()