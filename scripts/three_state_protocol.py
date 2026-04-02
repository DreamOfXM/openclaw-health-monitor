#!/usr/bin/env python3
"""
三态协议执行器

实现完整的三态协议：
1. request: @对方 + ack_id + 期望动作 + 截止时间
2. confirmed: @发起方 + 相同 ack_id + 版本号/生效时间
3. final: @对方 + 相同 ack_id + 终态收敛（全线程仅 1 条）
4. 静默：final 后 → NO_REPLY（禁止礼貌性回复）

这是自我进化系统的核心通信协议。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
SHARED_CONTEXT = BASE_DIR / "shared-context"
PROTOCOL_DIR = SHARED_CONTEXT / "three-state-protocol"
REQUESTS_FILE = PROTOCOL_DIR / "requests.jsonl"
CONFIRMED_FILE = PROTOCOL_DIR / "confirmed.jsonl"
FINAL_FILE = PROTOCOL_DIR / "final.jsonl"


class ThreeStateProtocol:
    """三态协议执行器"""
    
    def __init__(self):
        PROTOCOL_DIR.mkdir(parents=True, exist_ok=True)
        self.requests_file = REQUESTS_FILE
        self.confirmed_file = CONFIRMED_FILE
        self.final_file = FINAL_FILE
    
    def _log(self, file: Path, entry: dict[str, Any]) -> None:
        """写入日志"""
        with open(file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def request(
        self,
        ack_id: str,
        target_agent: str,
        action: str,
        deadline: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送 request"""
        now = int(time.time())
        entry = {
            "type": "request",
            "ack_id": ack_id,
            "target_agent": target_agent,
            "action": action,
            "deadline": deadline,
            "metadata": metadata or {},
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
        }
        self._log(self.requests_file, entry)
        return entry
    
    def confirmed(
        self,
        ack_id: str,
        source_agent: str,
        version: str = "v1",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送 confirmed"""
        now = int(time.time())
        entry = {
            "type": "confirmed",
            "ack_id": ack_id,
            "source_agent": source_agent,
            "version": version,
            "metadata": metadata or {},
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
        }
        self._log(self.confirmed_file, entry)
        return entry
    
    def final(
        self,
        ack_id: str,
        source_agent: str,
        result: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送 final"""
        now = int(time.time())
        entry = {
            "type": "final",
            "ack_id": ack_id,
            "source_agent": source_agent,
            "result": result,
            "metadata": metadata or {},
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
        }
        self._log(self.final_file, entry)
        return entry
    
    def get_request(self, ack_id: str) -> dict[str, Any] | None:
        """获取 request"""
        if not self.requests_file.exists():
            return None
        
        with open(self.requests_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("ack_id") == ack_id:
                        return entry
                except json.JSONDecodeError:
                    continue
        
        return None
    
    def is_confirmed(self, ack_id: str) -> bool:
        """检查是否已 confirmed"""
        if not self.confirmed_file.exists():
            return False
        
        with open(self.confirmed_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("ack_id") == ack_id:
                        return True
                except json.JSONDecodeError:
                    continue
        
        return False
    
    def is_final(self, ack_id: str) -> bool:
        """检查是否已 final"""
        if not self.final_file.exists():
            return False
        
        with open(self.final_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("ack_id") == ack_id:
                        return True
                except json.JSONDecodeError:
                    continue
        
        return False
    
    def get_state(self, ack_id: str) -> str:
        """获取协议状态"""
        if self.is_final(ack_id):
            return "final"
        elif self.is_confirmed(ack_id):
            return "confirmed"
        elif self.get_request(ack_id):
            return "request"
        else:
            return "unknown"
    
    def should_silence(self, ack_id: str) -> bool:
        """检查是否应该静默（final 后禁止回复）"""
        return self.is_final(ack_id)


def main():
    """主入口"""
    protocol = ThreeStateProtocol()
    
    # 测试
    ack_id = "test-001"
    protocol.request(ack_id, "builder", "implement_feature", int(time.time()) + 3600)
    print(f"Request sent: {ack_id}")
    
    protocol.confirmed(ack_id, "main")
    print(f"Confirmed: {ack_id}")
    
    protocol.final(ack_id, "builder", {"status": "completed"})
    print(f"Final: {ack_id}")
    
    print(f"State: {protocol.get_state(ack_id)}")
    print(f"Should silence: {protocol.should_silence(ack_id)}")


if __name__ == "__main__":
    main()