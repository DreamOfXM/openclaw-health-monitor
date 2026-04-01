#!/usr/bin/env python3
"""
Agent 联动机制 - 信息加工后传递

核心设计：
- Agent 之间不是简单转发信息，而是上游主动为下游准备接口
- 标准化数据格式和路径约定
- 支持跨 Agent 读取

联动链路：
1. ainews → content: 情报到内容（改写要点）
2. ainews → main: Tech Radar 技术雷达
3. macro → trading: 宏观因子包
4. trading → macro: 美股跨时区联动
5. main → 全团队: 反思汇总
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum


class LinkageType(Enum):
    INTEL_TO_CONTENT = "intel_to_content"
    TECH_RADAR = "tech_radar"
    MACRO_TO_TRADING = "macro_to_trading"
    CROSS_TIMEZONE = "cross_timezone"
    REFLECTION_SUMMARY = "reflection_summary"


@dataclass
class LinkagePayload:
    """联动载荷"""
    linkage_type: LinkageType
    source_agent: str
    target_agent: str
    created_at: int
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "linkage_type": self.linkage_type.value,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "created_at": self.created_at,
            "payload": self.payload,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinkagePayload":
        return cls(
            linkage_type=LinkageType(data["linkage_type"]),
            source_agent=data["source_agent"],
            target_agent=data["target_agent"],
            created_at=data["created_at"],
            payload=data["payload"],
            metadata=data.get("metadata", {}),
        )


class AgentLinkage:
    """Agent 联动管理器"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.shared_context = Path.home() / ".openclaw" / "shared-context"
        self.shared_context.mkdir(parents=True, exist_ok=True)
        
        self.intel_dir = self.shared_context / "intel"
        self.intel_dir.mkdir(parents=True, exist_ok=True)
        
        self.tech_radar_file = self.shared_context / "tech-radar.json"
        self.linkage_log = self.shared_context / "linkage-log.jsonl"
    
    def create_intel_for_content(
        self,
        source_agent: str,
        intel_data: dict[str, Any],
        rewrite_hints: dict[str, Any],
    ) -> LinkagePayload:
        """为 Content Agent 准备情报"""
        payload = LinkagePayload(
            linkage_type=LinkageType.INTEL_TO_CONTENT,
            source_agent=source_agent,
            target_agent="content",
            created_at=int(time.time()),
            payload={
                "intel": intel_data,
                "rewrite_hints": rewrite_hints,
            },
            metadata={
                "format_version": "1.0",
            },
        )
        
        self._write_linkage(payload)
        return payload
    
    def create_tech_radar_entry(
        self,
        source_agent: str,
        tech_name: str,
        tech_data: dict[str, Any],
    ) -> LinkagePayload:
        """创建 Tech Radar 条目"""
        # 读取现有雷达
        radar = {}
        if self.tech_radar_file.exists():
            try:
                radar = json.loads(self.tech_radar_file.read_text(encoding="utf-8"))
            except Exception:
                radar = {}
        
        # 更新条目
        radar[tech_name] = {
            **tech_data,
            "updated_at": int(time.time()),
            "updated_by": source_agent,
        }
        
        # 写回
        self.tech_radar_file.write_text(
            json.dumps(radar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        payload = LinkagePayload(
            linkage_type=LinkageType.TECH_RADAR,
            source_agent=source_agent,
            target_agent="main",
            created_at=int(time.time()),
            payload={
                "tech_name": tech_name,
                "tech_data": tech_data,
            },
        )
        
        self._write_linkage(payload)
        return payload
    
    def create_macro_factors(
        self,
        source_agent: str,
        factors: dict[str, Any],
    ) -> LinkagePayload:
        """创建宏观因子包"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        factor_file = self.intel_dir / f"macro-factors-{date_str}.json"
        
        factor_file.write_text(
            json.dumps({
                "created_at": int(time.time()),
                "created_by": source_agent,
                "factors": factors,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        payload = LinkagePayload(
            linkage_type=LinkageType.MACRO_TO_TRADING,
            source_agent=source_agent,
            target_agent="trading",
            created_at=int(time.time()),
            payload={
                "date": date_str,
                "factors": factors,
            },
        )
        
        self._write_linkage(payload)
        return payload
    
    def read_macro_factors(self, date_str: Optional[str] = None) -> Optional[dict[str, Any]]:
        """读取宏观因子包"""
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        
        factor_file = self.intel_dir / f"macro-factors-{date_str}.json"
        if not factor_file.exists():
            return None
        
        try:
            return json.loads(factor_file.read_text(encoding="utf-8"))
        except Exception:
            return None
    
    def read_tech_radar(self) -> dict[str, Any]:
        """读取 Tech Radar"""
        if not self.tech_radar_file.exists():
            return {}
        
        try:
            return json.loads(self.tech_radar_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    
    def get_linkage_history(
        self,
        linkage_type: Optional[LinkageType] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取联动历史"""
        history = []
        
        if not self.linkage_log.exists():
            return history
        
        for line in self.linkage_log.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
                if linkage_type and entry.get("linkage_type") != linkage_type.value:
                    continue
                history.append(entry)
                if len(history) >= limit:
                    break
            except Exception:
                continue
        
        return history
    
    def _write_linkage(self, payload: LinkagePayload) -> None:
        """写入联动记录"""
        with open(self.linkage_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload.to_dict(), ensure_ascii=False) + "\n")


# 标准化路径约定
STANDARD_PATHS = {
    "macro_daily_check": "workspace-macro/knowledge/daily/{date}/daily-check.md",
    "trading_report": "workspace-trading/knowledge/daily/{date}/trading-report.md",
    "ainews_intel": "shared-context/intel/ainews-{date}.json",
    "tech_radar": "shared-context/tech-radar.json",
    "agent_sessions": "shared-context/agent-sessions/{agent}_{session_id}.json",
}


def get_standard_path(path_key: str, **kwargs) -> str:
    """获取标准化路径"""
    template = STANDARD_PATHS.get(path_key)
    if not template:
        raise ValueError(f"Unknown path key: {path_key}")
    return template.format(**kwargs)