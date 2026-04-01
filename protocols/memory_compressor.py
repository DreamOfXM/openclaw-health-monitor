#!/usr/bin/env python3
"""
记忆系统自主压缩

核心机制：
1. MEMORY.md 只留长期有效结论
2. 每日细节放 memory/YYYY-MM-DD.md
3. 方案/架构放 knowledge/
4. 趋势块"滚动压缩"
5. 排障经验保留"结论"

设计原则：
- SOUL.md 绝对不自动修改
- 压缩策略不是简单的"删旧的"
- 证明有长期价值的再 promote
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field


@dataclass
class MemorySection:
    """记忆区块"""
    title: str
    content: str
    category: str  # long_term, daily, knowledge, trend, troubleshooting
    created_at: int
    last_used_at: int
    use_count: int
    tokens_estimate: int
    should_promote: bool = False
    should_archive: bool = False
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content,
            "category": self.category,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "tokens_estimate": self.tokens_estimate,
            "should_promote": self.should_promote,
            "should_archive": self.should_archive,
        }


class MemoryCompressor:
    """记忆压缩管理器"""
    
    MAX_MEMORY_TOKENS = 3000
    MAX_TREND_ITEMS = 8
    ARCHIVE_THRESHOLD_DAYS = 14
    
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.memory_file = workspace_dir / "MEMORY.md"
        self.memory_dir = workspace_dir / "memory"
        self.knowledge_dir = workspace_dir / "knowledge"
        self.learnings_dir = workspace_dir / ".learnings"
        
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
    
    def analyze_memory(self) -> dict[str, Any]:
        """分析记忆状态"""
        if not self.memory_file.exists():
            return {"status": "no_memory_file", "sections": []}
        
        content = self.memory_file.read_text(encoding="utf-8")
        sections = self._parse_sections(content)
        
        total_tokens = sum(s.tokens_estimate for s in sections)
        
        return {
            "status": "ok",
            "total_tokens": total_tokens,
            "token_limit": self.MAX_MEMORY_TOKENS,
            "needs_compression": total_tokens > self.MAX_MEMORY_TOKENS,
            "sections": [s.to_dict() for s in sections],
            "section_count": len(sections),
        }
    
    def compress(self, dry_run: bool = False) -> dict[str, Any]:
        """执行压缩"""
        analysis = self.analyze_memory()
        if analysis["status"] != "ok":
            return analysis
        
        sections = [MemorySection(**s) for s in analysis["sections"]]
        
        # 1. 识别需要归档的区块
        self._identify_archive_candidates(sections)
        
        # 2. 识别需要提升的区块
        self._identify_promote_candidates(sections)
        
        # 3. 滚动压缩趋势块
        self._compress_trends(sections)
        
        # 4. 生成压缩报告
        report = {
            "analyzed_at": int(time.time()),
            "total_tokens_before": analysis["total_tokens"],
            "sections_to_archive": [s.title for s in sections if s.should_archive],
            "sections_to_promote": [s.title for s in sections if s.should_promote],
            "dry_run": dry_run,
        }
        
        if dry_run:
            report["status"] = "dry_run_complete"
            return report
        
        # 5. 执行压缩
        self._execute_compression(sections)
        
        # 6. 计算压缩后大小
        new_analysis = self.analyze_memory()
        report["total_tokens_after"] = new_analysis["total_tokens"]
        report["tokens_saved"] = report["total_tokens_before"] - report["total_tokens_after"]
        report["status"] = "compression_complete"
        
        return report
    
    def promote_daily_to_long_term(
        self,
        date_str: str,
        reason: str,
    ) -> bool:
        """将每日记忆提升为长期记忆"""
        daily_file = self.memory_dir / f"{date_str}.md"
        if not daily_file.exists():
            return False
        
        content = daily_file.read_text(encoding="utf-8")
        
        # 追加到 MEMORY.md
        with open(self.memory_file, "a", encoding="utf-8") as f:
            f.write(f"\n## 从 {date_str} 提升\n\n")
            f.write(f"**提升原因**: {reason}\n\n")
            f.write(content)
            f.write("\n")
        
        # 标记原文件为已提升
        promoted_file = self.memory_dir / f"{date_str}.promoted.md"
        daily_file.rename(promoted_file)
        
        return True
    
    def archive_old_learnings(self, days: int = 14) -> int:
        """归档旧的学习记录"""
        if not self.learnings_dir.exists():
            return 0
        
        threshold = int(time.time()) - days * 86400
        archived = 0
        
        for file in self.learnings_dir.glob("*.md"):
            if file.stat().st_mtime < threshold:
                archive_file = self.learnings_dir / "archive" / file.name
                archive_file.parent.mkdir(parents=True, exist_ok=True)
                file.rename(archive_file)
                archived += 1
        
        return archived
    
    def _parse_sections(self, content: str) -> list[MemorySection]:
        """解析 MEMORY.md 的区块"""
        sections = []
        
        # 简单的区块解析
        pattern = r"##\s+(.+)\n([\s\S]*?)(?=\n##\s+|$)"
        matches = re.findall(pattern, content)
        
        now = int(time.time())
        
        for title, body in matches:
            # 估算 token 数
            tokens_estimate = len(body) // 4  # 粗略估算
            
            # 判断类别
            category = self._classify_section(title, body)
            
            section = MemorySection(
                title=title.strip(),
                content=body.strip(),
                category=category,
                created_at=now,  # 无法准确获取，用当前时间
                last_used_at=now,
                use_count=1,
                tokens_estimate=tokens_estimate,
            )
            sections.append(section)
        
        return sections
    
    def _classify_section(self, title: str, content: str) -> str:
        """分类区块"""
        title_lower = title.lower()
        content_lower = content.lower()
        
        if "用户偏好" in title or "工作习惯" in title:
            return "long_term"
        elif "趋势" in title or "trend" in title_lower:
            return "trend"
        elif "排障" in title or "troubleshoot" in title_lower:
            return "troubleshooting"
        elif "方案" in title or "架构" in title:
            return "knowledge"
        else:
            return "daily"
    
    def _identify_archive_candidates(self, sections: list[MemorySection]) -> None:
        """识别需要归档的区块"""
        now = int(time.time())
        threshold = now - self.ARCHIVE_THRESHOLD_DAYS * 86400
        
        for section in sections:
            # 超过阈值且使用次数少的区块
            if section.created_at < threshold and section.use_count < 2:
                if section.category not in ("long_term",):
                    section.should_archive = True
    
    def _identify_promote_candidates(self, sections: list[MemorySection]) -> None:
        """识别需要提升的区块"""
        for section in sections:
            # 使用次数多且有长期价值的区块
            if section.use_count >= 3 and section.category in ("daily", "troubleshooting"):
                section.should_promote = True
    
    def _compress_trends(self, sections: list[MemorySection]) -> None:
        """滚动压缩趋势块"""
        trend_sections = [s for s in sections if s.category == "trend"]
        
        if len(trend_sections) <= self.MAX_TREND_ITEMS:
            return
        
        # 按使用次数排序，保留使用最多的
        trend_sections.sort(key=lambda s: s.use_count, reverse=True)
        
        for section in trend_sections[self.MAX_TREND_ITEMS:]:
            section.should_archive = True
    
    def _execute_compression(self, sections: list[MemorySection]) -> None:
        """执行压缩"""
        if not self.memory_file.exists():
            return
        
        content = self.memory_file.read_text(encoding="utf-8")
        
        # 移除需要归档的区块
        for section in sections:
            if section.should_archive:
                # 简单实现：移除区块内容
                pattern = rf"##\s+{re.escape(section.title)}\n[\s\S]*?(?=\n##\s+|$)"
                content = re.sub(pattern, "", content)
                
                # 写入归档文件
                archive_file = self.memory_dir / "archive" / f"{section.title[:30]}.md"
                archive_file.parent.mkdir(parents=True, exist_ok=True)
                archive_file.write_text(f"## {section.title}\n\n{section.content}\n", encoding="utf-8")
        
        # 写回 MEMORY.md
        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")