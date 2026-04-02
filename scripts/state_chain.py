#!/usr/bin/env python3
"""
异步状态链 - 11 状态模型

从 accepted 到 delivered，每次变化写 events.jsonl 审计。

状态：
1. accepted - 请求已接受
2. confirmed - 已确认
3. in_progress - 进行中
4. pending_review - 等待审核
5. approved - 已批准
6. rejected - 已拒绝
7. completed - 已完成
8. failed - 已失败
9. timeout - 已超时
10. delivered - 已送达
11. cancelled - 已取消

这是自我进化系统的核心状态管理。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any
from enum import Enum

BASE_DIR = Path(__file__).parent.parent
SHARED_CONTEXT = BASE_DIR / "shared-context"
STATE_CHAIN_DIR = SHARED_CONTEXT / "state-chain"
EVENTS_FILE = STATE_CHAIN_DIR / "events.jsonl"
STATE_FILE = STATE_CHAIN_DIR / "states.jsonl"


class RequestState(Enum):
    """请求状态"""
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class StateChain:
    """异步状态链"""
    
    # 状态转换规则
    TRANSITIONS = {
        RequestState.ACCEPTED: [RequestState.CONFIRMED, RequestState.REJECTED, RequestState.CANCELLED],
        RequestState.CONFIRMED: [RequestState.IN_PROGRESS, RequestState.CANCELLED],
        RequestState.IN_PROGRESS: [RequestState.PENDING_REVIEW, RequestState.COMPLETED, RequestState.FAILED, RequestState.TIMEOUT],
        RequestState.PENDING_REVIEW: [RequestState.APPROVED, RequestState.REJECTED],
        RequestState.APPROVED: [RequestState.COMPLETED, RequestState.FAILED],
        RequestState.REJECTED: [RequestState.ACCEPTED],  # 可以重新提交
        RequestState.COMPLETED: [RequestState.DELIVERED],
        RequestState.FAILED: [RequestState.ACCEPTED],  # 可以重试
        RequestState.TIMEOUT: [RequestState.ACCEPTED],  # 可以重试
        RequestState.DELIVERED: [],  # 终态
        RequestState.CANCELLED: [],  # 终态
    }
    
    def __init__(self):
        STATE_CHAIN_DIR.mkdir(parents=True, exist_ok=True)
        self.events_file = EVENTS_FILE
        self.state_file = STATE_FILE
    
    def _log_event(self, event: dict[str, Any]) -> None:
        """写入事件日志"""
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    
    def _get_state_entry(self, request_id: str) -> dict[str, Any] | None:
        """获取状态条目"""
        if not self.state_file.exists():
            return None
        
        with open(self.state_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("request_id") == request_id:
                        return entry
                except json.JSONDecodeError:
                    continue
        
        return None
    
    def _update_state_entry(self, request_id: str, state: RequestState, metadata: dict[str, Any] | None = None) -> None:
        """更新状态条目"""
        now = int(time.time())
        
        # 读取所有条目
        entries = []
        if self.state_file.exists():
            with open(self.state_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        
        # 更新或添加条目
        found = False
        for entry in entries:
            if entry.get("request_id") == request_id:
                entry["state"] = state.value
                entry["updated_at"] = now
                entry["metadata"].update(metadata or {})
                found = True
                break
        
        if not found:
            entries.append({
                "request_id": request_id,
                "state": state.value,
                "created_at": now,
                "updated_at": now,
                "metadata": metadata or {},
            })
        
        # 写回文件
        with open(self.state_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def can_transition(self, current_state: RequestState, new_state: RequestState) -> bool:
        """检查是否可以转换状态"""
        return new_state in self.TRANSITIONS.get(current_state, [])
    
    def transition(
        self,
        request_id: str,
        new_state: RequestState,
        actor: str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """转换状态"""
        now = int(time.time())
        
        # 获取当前状态
        entry = self._get_state_entry(request_id)
        current_state = RequestState(entry["state"]) if entry else RequestState.ACCEPTED
        
        # 检查是否可以转换
        if entry and not self.can_transition(current_state, new_state):
            return {
                "success": False,
                "error": f"不能从 {current_state.value} 转换到 {new_state.value}",
            }
        
        # 记录事件
        event = {
            "request_id": request_id,
            "from_state": current_state.value,
            "to_state": new_state.value,
            "actor": actor,
            "reason": reason,
            "timestamp": now,
            "iso_time": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._log_event(event)
        
        # 更新状态
        self._update_state_entry(request_id, new_state, metadata)
        
        return {
            "success": True,
            "event": event,
        }
    
    def get_state(self, request_id: str) -> RequestState | None:
        """获取当前状态"""
        entry = self._get_state_entry(request_id)
        if entry:
            return RequestState(entry["state"])
        return None
    
    def is_terminal(self, state: RequestState) -> bool:
        """检查是否是终态"""
        return len(self.TRANSITIONS.get(state, [])) == 0


def main():
    """主入口"""
    chain = StateChain()
    
    # 测试状态转换
    request_id = "test-request-001"
    
    result = chain.transition(request_id, RequestState.ACCEPTED, "main", "测试请求")
    print(f"Accepted: {result}")
    
    result = chain.transition(request_id, RequestState.CONFIRMED, "builder", "确认接受")
    print(f"Confirmed: {result}")
    
    result = chain.transition(request_id, RequestState.IN_PROGRESS, "builder", "开始执行")
    print(f"In progress: {result}")
    
    result = chain.transition(request_id, RequestState.COMPLETED, "builder", "执行完成")
    print(f"Completed: {result}")
    
    result = chain.transition(request_id, RequestState.DELIVERED, "main", "已送达")
    print(f"Delivered: {result}")
    
    print(f"Current state: {chain.get_state(request_id)}")
    print(f"Is terminal: {chain.is_terminal(chain.get_state(request_id))}")


if __name__ == "__main__":
    main()