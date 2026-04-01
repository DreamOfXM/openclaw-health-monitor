#!/usr/bin/env python3
"""
Agent 协作系统 - 集成模块

整合：
1. 三态协议 (three_state_protocol)
2. Dead Letter Queue (dead_letter_queue)
3. 异步状态链 (async_state_chain)
4. 通信 Guardrail (communication_guardrail)
5. 记忆压缩 (memory_compressor)
6. Agent 联动 (agent_linkage)

使用方式：
    from protocols import AgentCollaborationSystem
    
    system = AgentCollaborationSystem(base_dir)
    
    # 创建请求
    request = system.create_request("main", "builder", "implement_feature", {...})
    
    # 确认请求
    system.confirm_request(request.ack_id, "builder")
    
    # 完成请求
    system.complete_request(request.ack_id, "builder", result)
    
    # 确认送达
    system.deliver_request(request.ack_id, "main")
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from protocols.three_state_protocol import ThreeStateProtocol, ProtocolState, ProtocolMessage
from protocols.dead_letter_queue import DeadLetterQueue, DeadLetterEntry
from protocols.async_state_chain import AsyncStateChain, AsyncRequest, RequestState
from protocols.communication_guardrail import CommunicationGuardrail, GuardrailAction
from protocols.memory_compressor import MemoryCompressor
from protocols.agent_linkage import AgentLinkage, LinkageType
from protocols.skills_ecosystem import SkillsEcosystem, Skill, SkillStatus


class AgentCollaborationSystem:
    """Agent 协作系统"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        
        # 初始化各子系统
        self.protocol = ThreeStateProtocol(base_dir)
        self.dlq = DeadLetterQueue(base_dir)
        self.state_chain = AsyncStateChain(base_dir)
        self.guardrail = CommunicationGuardrail(base_dir)
        self.memory_compressor = MemoryCompressor(base_dir)
        self.linkage = AgentLinkage(base_dir)
    
    def create_request(
        self,
        source_agent: str,
        target_agent: str,
        action: str,
        payload: dict[str, Any],
        deadline_seconds: int = 3600,
    ) -> AsyncRequest:
        """创建请求（整合三态协议 + 异步状态链）"""
        # 1. Guardrail 检查
        checks = self.guardrail.check_message(
            source_agent=source_agent,
            target_agent=target_agent,
            message_type="request",
            payload=payload,
        )
        
        if not self.guardrail.should_allow(checks):
            raise ValueError(f"Guardrail blocked: {[c.reason for c in checks if not c.passed]}")
        
        # 2. 创建三态协议消息
        protocol_msg = self.protocol.create_request(
            source_agent=source_agent,
            target_agent=target_agent,
            action=action,
            deadline_seconds=deadline_seconds,
            payload=payload,
        )
        
        # 3. 创建异步状态链请求
        async_request = self.state_chain.create_request(
            source_agent=source_agent,
            target_agent=target_agent,
            intent=action,
            payload={
                **payload,
                "ack_id": protocol_msg.ack_id,
            },
            deadline_seconds=deadline_seconds,
        )
        
        return async_request
    
    def confirm_request(
        self,
        request_id: str,
        actor: str,
    ) -> bool:
        """确认请求"""
        # 获取请求
        request = self.state_chain._get_request(request_id)
        if not request:
            return False
        
        ack_id = request.payload.get("ack_id")
        
        # 更新三态协议
        if ack_id:
            self.protocol.confirm(ack_id, actor)
        
        # 更新异步状态链
        return self.state_chain.accept(request_id, actor)
    
    def start_request(
        self,
        request_id: str,
        actor: str,
    ) -> bool:
        """开始处理请求"""
        return self.state_chain.start_work(request_id, actor)
    
    def complete_request(
        self,
        request_id: str,
        actor: str,
        result: dict[str, Any],
    ) -> bool:
        """完成请求"""
        request = self.state_chain._get_request(request_id)
        if not request:
            return False
        
        # 更新异步状态链
        success = self.state_chain.complete(request_id, actor, result)
        
        if success:
            # 检查是否需要创建联动
            self._maybe_create_linkage(request, result)
        
        return success
    
    def deliver_request(
        self,
        request_id: str,
        actor: str,
    ) -> bool:
        """确认送达"""
        request = self.state_chain._get_request(request_id)
        if not request:
            return False
        
        ack_id = request.payload.get("ack_id")
        
        # 更新三态协议
        if ack_id:
            self.protocol.finalize(
                ack_id=ack_id,
                source_agent=actor,
                result="delivered",
            )
        
        # 更新异步状态链
        return self.state_chain.deliver(request_id, actor)
    
    def fail_request(
        self,
        request_id: str,
        actor: str,
        reason: str,
    ) -> bool:
        """标记请求失败"""
        request = self.state_chain._get_request(request_id)
        if not request:
            return False
        
        # 更新异步状态链
        success = self.state_chain.fail(request_id, actor, reason)
        
        if success:
            # 添加到 Dead Letter Queue
            self.dlq.add_entry(
                task_id=request_id,
                source_agent=request.source_agent,
                target_agent=request.target_agent,
                action=request.intent,
                failure_reason=reason,
                payload=request.payload,
            )
        
        return success
    
    def timeout_request(
        self,
        request_id: str,
    ) -> bool:
        """标记请求超时"""
        request = self.state_chain._get_request(request_id)
        if not request:
            return False
        
        ack_id = request.payload.get("ack_id")
        
        # 更新异步状态链
        success = self.state_chain.timeout(request_id)
        
        if success:
            # 升级到文件投递
            if ack_id:
                self.protocol.escalate_to_file(ack_id)
            
            # 添加到 Dead Letter Queue
            self.dlq.add_entry(
                task_id=request_id,
                source_agent=request.source_agent,
                target_agent=request.target_agent,
                action=request.intent,
                failure_reason="timeout",
                payload=request.payload,
            )
        
        return success
    
    def get_pending_deliveries(self) -> list[AsyncRequest]:
        """获取待投递的完成结果"""
        return self.state_chain.get_pending_deliveries()
    
    def get_dlq_entries(self) -> list[DeadLetterEntry]:
        """获取死信队列条目"""
        return self.dlq.get_pending_entries()
    
    def get_retryable_dlq_entries(self) -> list[DeadLetterEntry]:
        """获取可重试的死信条目"""
        return self.dlq.get_retryable_entries()
    
    def compress_memory(self, dry_run: bool = False) -> dict[str, Any]:
        """压缩记忆"""
        return self.memory_compressor.compress(dry_run)
    
    def create_linkage(
        self,
        linkage_type: LinkageType,
        source_agent: str,
        payload: dict[str, Any],
    ) -> Any:
        """创建联动"""
        if linkage_type == LinkageType.INTEL_TO_CONTENT:
            return self.linkage.create_intel_for_content(
                source_agent=source_agent,
                intel_data=payload.get("intel", {}),
                rewrite_hints=payload.get("rewrite_hints", {}),
            )
        elif linkage_type == LinkageType.TECH_RADAR:
            return self.linkage.create_tech_radar_entry(
                source_agent=source_agent,
                tech_name=payload.get("tech_name", ""),
                tech_data=payload.get("tech_data", {}),
            )
        elif linkage_type == LinkageType.MACRO_TO_TRADING:
            return self.linkage.create_macro_factors(
                source_agent=source_agent,
                factors=payload.get("factors", {}),
            )
        else:
            raise ValueError(f"Unsupported linkage type: {linkage_type}")
    
    def get_system_status(self) -> dict[str, Any]:
        """获取系统状态"""
        return {
            "protocol": {
                "active_count": len(list(self.protocol.active_file.read_text(encoding="utf-8").strip().split("\n"))) if self.protocol.active_file.exists() else 0,
            },
            "dlq": self.dlq.get_statistics(),
            "state_chain": self.state_chain.get_statistics(),
            "guardrail": self.guardrail.get_statistics(),
            "memory": self.memory_compressor.analyze_memory(),
        }
    
    def _maybe_create_linkage(
        self,
        request: AsyncRequest,
        result: dict[str, Any],
    ) -> None:
        """检查是否需要创建联动"""
        # 简单实现：根据 intent 判断
        intent = request.intent
        
        if "tech" in intent.lower() or "radar" in intent.lower():
            self.linkage.create_tech_radar_entry(
                source_agent=request.source_agent,
                tech_name=result.get("tech_name", "unknown"),
                tech_data=result,
            )
        elif "macro" in intent.lower():
            self.linkage.create_macro_factors(
                source_agent=request.source_agent,
                factors=result,
            )


# 便捷函数
def create_collaboration_system(base_dir: Optional[Path] = None) -> AgentCollaborationSystem:
    """创建 Agent 协作系统"""
    if not base_dir:
        base_dir = Path(__file__).parent.parent
    return AgentCollaborationSystem(base_dir)