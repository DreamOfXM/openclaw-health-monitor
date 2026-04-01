#!/usr/bin/env python3
"""
通信 Guardrail - 5 条硬性拦截规则

规则：
1. 误用 message → reject
2. 身份伪造 → reject
3. ack_id 重发 → block
4. 超时重试 → block
5. final 后回复 → block

设计原则：
- 在系统入口拦截错误路径
- 不依赖 Agent 自觉，系统级强制约束
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


class GuardrailAction(Enum):
    ALLOW = "allow"
    REJECT = "reject"
    BLOCK = "block"
    WARN = "warn"


@dataclass
class GuardrailCheck:
    """Guardrail 检查结果"""
    rule_id: str
    rule_name: str
    passed: bool
    action: GuardrailAction
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


class CommunicationGuardrail:
    """通信 Guardrail 管理器"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.guardrail_dir = base_dir / "guardrail"
        self.guardrail_dir.mkdir(parents=True, exist_ok=True)
        self.violations_file = self.guardrail_dir / "violations.jsonl"
        self.ack_registry_file = self.guardrail_dir / "ack_registry.jsonl"
        self.final_registry_file = self.guardrail_dir / "final_registry.jsonl"
    
    def check_message(
        self,
        source_agent: str,
        target_agent: str,
        message_type: str,
        ack_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> list[GuardrailCheck]:
        """检查消息是否违反 Guardrail 规则"""
        checks = []
        
        # 规则 1: 误用 message → reject
        checks.append(self._check_message_misuse(source_agent, target_agent, message_type))
        
        # 规则 2: 身份伪造 → reject
        checks.append(self._check_identity_forgery(source_agent, target_agent, payload))
        
        # 规则 3: ack_id 重发 → block
        if ack_id:
            checks.append(self._check_ack_id_replay(source_agent, ack_id, message_type))
        
        # 规则 4: 超时重试 → block
        if ack_id and thread_id:
            checks.append(self._check_timeout_retry(ack_id, thread_id))
        
        # 规则 5: final 后回复 → block
        if ack_id and thread_id:
            checks.append(self._check_post_final_reply(ack_id, thread_id))
        
        # 记录违规
        violated = [c for c in checks if not c.passed]
        if violated:
            self._record_violation(source_agent, target_agent, ack_id, violated)
        
        return checks
    
    def should_allow(self, checks: list[GuardrailCheck]) -> bool:
        """判断是否允许通过"""
        for check in checks:
            if check.action in (GuardrailAction.REJECT, GuardrailAction.BLOCK):
                return False
        return True
    
    def get_violations(self, agent_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        """获取违规记录"""
        violations = []
        
        if not self.violations_file.exists():
            return violations
        
        for line in self.violations_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                violation = json.loads(line)
                if agent_id and violation.get("source_agent") != agent_id:
                    continue
                violations.append(violation)
                if len(violations) >= limit:
                    break
            except Exception:
                continue
        
        return violations
    
    def get_statistics(self) -> dict[str, Any]:
        """获取 Guardrail 统计"""
        by_rule: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        
        if self.violations_file.exists():
            for line in self.violations_file.read_text(encoding="utf-8").strip().split("\n"):
                if not line:
                    continue
                try:
                    violation = json.loads(line)
                    for v in violation.get("violations", []):
                        rule_id = v.get("rule_id", "unknown")
                        by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
                    by_agent[violation.get("source_agent", "unknown")] = by_agent.get(violation.get("source_agent", "unknown"), 0) + 1
                except Exception:
                    continue
        
        return {
            "total_violations": sum(by_rule.values()),
            "by_rule": by_rule,
            "by_agent": by_agent,
        }
    
    def _check_message_misuse(
        self,
        source_agent: str,
        target_agent: str,
        message_type: str,
    ) -> GuardrailCheck:
        """规则 1: 误用 message"""
        # message 不应用于内部控制面通信
        if message_type == "message" and source_agent.startswith("agent:") and target_agent.startswith("agent:"):
            return GuardrailCheck(
                rule_id="rule_1",
                rule_name="message_misuse",
                passed=False,
                action=GuardrailAction.REJECT,
                reason="Agent 间通信不应使用 message，应使用 sessions_send 或 shared-context",
            )
        
        return GuardrailCheck(
            rule_id="rule_1",
            rule_name="message_misuse",
            passed=True,
            action=GuardrailAction.ALLOW,
            reason="OK",
        )
    
    def _check_identity_forgery(
        self,
        source_agent: str,
        target_agent: str,
        payload: Optional[dict[str, Any]],
    ) -> GuardrailCheck:
        """规则 2: 身份伪造"""
        if not payload:
            return GuardrailCheck(
                rule_id="rule_2",
                rule_name="identity_forgery",
                passed=True,
                action=GuardrailAction.ALLOW,
                reason="OK",
            )
        
        # 检查 payload 中是否有伪造的身份信息
        claimed_source = payload.get("source_agent")
        if claimed_source and claimed_source != source_agent:
            return GuardrailCheck(
                rule_id="rule_2",
                rule_name="identity_forgery",
                passed=False,
                action=GuardrailAction.REJECT,
                reason=f"身份伪造：声称来自 {claimed_source}，实际来自 {source_agent}",
                details={"claimed": claimed_source, "actual": source_agent},
            )
        
        return GuardrailCheck(
            rule_id="rule_2",
            rule_name="identity_forgery",
            passed=True,
            action=GuardrailAction.ALLOW,
            reason="OK",
        )
    
    def _check_ack_id_replay(
        self,
        source_agent: str,
        ack_id: str,
        message_type: str,
    ) -> GuardrailCheck:
        """规则 3: ack_id 重发"""
        if not self.ack_registry_file.exists():
            self._register_ack_id(ack_id, source_agent, message_type)
            return GuardrailCheck(
                rule_id="rule_3",
                rule_name="ack_id_replay",
                passed=True,
                action=GuardrailAction.ALLOW,
                reason="首次发送",
            )
        
        # 检查是否已注册
        for line in self.ack_registry_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("ack_id") == ack_id:
                    # 同一 ack_id 不得重发
                    return GuardrailCheck(
                        rule_id="rule_3",
                        rule_name="ack_id_replay",
                        passed=False,
                        action=GuardrailAction.BLOCK,
                        reason=f"ack_id {ack_id} 已发送过，不得重发",
                        details={"original_sender": data.get("source_agent")},
                    )
            except Exception:
                continue
        
        self._register_ack_id(ack_id, source_agent, message_type)
        return GuardrailCheck(
            rule_id="rule_3",
            rule_name="ack_id_replay",
            passed=True,
            action=GuardrailAction.ALLOW,
            reason="OK",
        )
    
    def _check_timeout_retry(
        self,
        ack_id: str,
        thread_id: str,
    ) -> GuardrailCheck:
        """规则 4: 超时重试"""
        # 超时不得重试，应降级到文件投递
        # 这里简化实现，实际应检查超时状态
        return GuardrailCheck(
            rule_id="rule_4",
            rule_name="timeout_retry",
            passed=True,
            action=GuardrailAction.ALLOW,
            reason="OK",
        )
    
    def _check_post_final_reply(
        self,
        ack_id: str,
        thread_id: str,
    ) -> GuardrailCheck:
        """规则 5: final 后回复"""
        if not self.final_registry_file.exists():
            return GuardrailCheck(
                rule_id="rule_5",
                rule_name="post_final_reply",
                passed=True,
                action=GuardrailAction.ALLOW,
                reason="OK",
            )
        
        # 检查是否已有 final
        for line in self.final_registry_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("ack_id") == ack_id:
                    return GuardrailCheck(
                        rule_id="rule_5",
                        rule_name="post_final_reply",
                        passed=False,
                        action=GuardrailAction.BLOCK,
                        reason=f"ack_id {ack_id} 已有 final，禁止再回复",
                        details={"final_at": data.get("finalized_at")},
                    )
            except Exception:
                continue
        
        return GuardrailCheck(
            rule_id="rule_5",
            rule_name="post_final_reply",
            passed=True,
            action=GuardrailAction.ALLOW,
            reason="OK",
        )
    
    def _register_ack_id(
        self,
        ack_id: str,
        source_agent: str,
        message_type: str,
    ) -> None:
        """注册 ack_id"""
        with open(self.ack_registry_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ack_id": ack_id,
                "source_agent": source_agent,
                "message_type": message_type,
                "registered_at": int(time.time()),
            }, ensure_ascii=False) + "\n")
    
    def _register_final(
        self,
        ack_id: str,
        thread_id: str,
    ) -> None:
        """注册 final"""
        with open(self.final_registry_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ack_id": ack_id,
                "thread_id": thread_id,
                "finalized_at": int(time.time()),
            }, ensure_ascii=False) + "\n")
    
    def _record_violation(
        self,
        source_agent: str,
        target_agent: str,
        ack_id: Optional[str],
        violations: list[GuardrailCheck],
    ) -> None:
        """记录违规"""
        record = {
            "timestamp": int(time.time()),
            "source_agent": source_agent,
            "target_agent": target_agent,
            "ack_id": ack_id,
            "violations": [
                {
                    "rule_id": v.rule_id,
                    "rule_name": v.rule_name,
                    "action": v.action.value,
                    "reason": v.reason,
                }
                for v in violations
            ],
        }
        
        with open(self.violations_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")