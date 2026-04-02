#!/usr/bin/env python3
"""
通信 Guardrail - 5 条硬性拦截规则

在系统入口拦截错误路径：
1. 必须有明确的 source 和 target
2. 禁止身份伪造
3. ack_id 重发检查
4. 消息类型检查
5. final 后禁止继续发送

这是自我进化系统的核心通信约束。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
SHARED_CONTEXT = BASE_DIR / "shared-context"
GUARDRAIL_DIR = SHARED_CONTEXT / "guardrail"
VIOLATIONS_FILE = GUARDRAIL_DIR / "violations.jsonl"
ACK_REGISTRY_FILE = GUARDRAIL_DIR / "ack_registry.jsonl"


class CommGuardrail:
    """通信护栏"""
    
    def __init__(self):
        GUARDRAIL_DIR.mkdir(parents=True, exist_ok=True)
        self.violations_file = VIOLATIONS_FILE
        self.ack_registry_file = ACK_REGISTRY_FILE
    
    def _log_violation(self, rule: str, message: dict[str, Any], action: str) -> None:
        """记录违规"""
        entry = {
            "timestamp": int(time.time()),
            "iso_time": datetime.now().isoformat(),
            "rule": rule,
            "action": action,
            "message_summary": {
                "source": message.get("source"),
                "target": message.get("target"),
                "ack_id": message.get("ack_id"),
                "type": message.get("type"),
            },
        }
        with open(self.violations_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def _register_ack(self, ack_id: str, source: str, target: str) -> bool:
        """注册 ack_id，返回是否成功（重复则失败）"""
        now = int(time.time())
        
        # 检查是否已存在
        if self.ack_registry_file.exists():
            with open(self.ack_registry_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("ack_id") == ack_id:
                            return False  # 已存在
                    except json.JSONDecodeError:
                        continue
        
        # 注册新的 ack_id
        entry = {
            "ack_id": ack_id,
            "source": source,
            "target": target,
            "registered_at": now,
        }
        with open(self.ack_registry_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        
        return True
    
    def check_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """检查消息是否符合 5 条规则"""
        result = {
            "action": "allow",
            "rule": None,
            "reason": None,
        }
        
        # 规则 1：必须有明确的 source 和 target
        if not message.get("source") or not message.get("target"):
            result["action"] = "reject"
            result["rule"] = "missing_source_or_target"
            result["reason"] = "消息缺少 source 或 target"
            self._log_violation(result["rule"], message, result["action"])
            return result
        
        # 规则 2：禁止身份伪造（source 必须与实际发送者一致）
        # 这里需要外部传入实际发送者，暂时跳过
        
        # 规则 3：ack_id 重发检查
        ack_id = message.get("ack_id")
        if ack_id:
            if not self._register_ack(ack_id, message.get("source", ""), message.get("target", "")):
                result["action"] = "block"
                result["rule"] = "ack_id_duplicate"
                result["reason"] = f"ack_id={ack_id} 已存在，可能是重复投递"
                self._log_violation(result["rule"], message, result["action"])
                return result
        
        # 规则 4：消息类型检查
        msg_type = message.get("type")
        valid_types = ["request", "confirmed", "final", "progress", "error"]
        if msg_type and msg_type not in valid_types:
            result["action"] = "reject"
            result["rule"] = "invalid_message_type"
            result["reason"] = f"无效的消息类型：{msg_type}"
            self._log_violation(result["rule"], message, result["action"])
            return result
        
        # 规则 5：final 后禁止继续发送
        # 这里需要外部状态，暂时跳过
        
        return result
    
    def get_violations(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取违规记录"""
        if not self.violations_file.exists():
            return []
        
        violations = []
        with open(self.violations_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    violations.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        
        return violations[-limit:]
    
    def get_stats(self) -> dict[str, int]:
        """获取统计信息"""
        violations = self.get_violations(limit=1000)
        
        stats = {
            "total_violations": len(violations),
            "reject": 0,
            "block": 0,
        }
        
        for v in violations:
            action = v.get("action")
            if action in stats:
                stats[action] += 1
        
        return stats


def main():
    """主入口"""
    guardrail = CommGuardrail()
    stats = guardrail.get_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()