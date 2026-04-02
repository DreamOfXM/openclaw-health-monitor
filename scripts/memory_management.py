#!/usr/bin/env python3
"""
记忆管理机制

让 Agent 形成有用的记忆，定期跟主人确认记忆是否准确。

这是自我进化系统的核心：记忆管理，持续学习。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
WORKSPACE_DIR = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi")
MEMORY_FILE = WORKSPACE_DIR / "MEMORY.md"
LEARNINGS_FILE = WORKSPACE_DIR / ".learnings" / "LEARNINGS.md"
MEMORY_DIR = WORKSPACE_DIR / "memory"


def get_memory_stats() -> dict[str, Any]:
    """获取记忆统计"""
    stats = {
        "memory_file_exists": MEMORY_FILE.exists(),
        "memory_lines": 0,
        "learnings_file_exists": LEARNINGS_FILE.exists(),
        "learnings_lines": 0,
        "memory_dir_exists": MEMORY_DIR.exists(),
        "memory_files": 0,
        "total_memory_lines": 0,
    }
    
    if MEMORY_FILE.exists():
        try:
            content = MEMORY_FILE.read_text(encoding="utf-8")
            stats["memory_lines"] = len(content.split("\n"))
        except:
            pass
    
    if LEARNINGS_FILE.exists():
        try:
            content = LEARNINGS_FILE.read_text(encoding="utf-8")
            stats["learnings_lines"] = len(content.split("\n"))
        except:
            pass
    
    if MEMORY_DIR.exists():
        try:
            for file in MEMORY_DIR.glob("*.md"):
                stats["memory_files"] += 1
                try:
                    content = file.read_text(encoding="utf-8")
                    stats["total_memory_lines"] += len(content.split("\n"))
                except:
                    pass
        except:
            pass
    
    return stats


def check_memory_conflicts() -> list[dict[str, Any]]:
    """检查记忆冲突"""
    conflicts = []
    
    # 检查 MEMORY.md 中是否有矛盾的内容
    if MEMORY_FILE.exists():
        try:
            content = MEMORY_FILE.read_text(encoding="utf-8")
            
            # 检查是否有矛盾的资金配置
            if "50,000" in content and "100,000" in content:
                conflicts.append({
                    "type": "资金配置矛盾",
                    "description": "发现 50,000 和 100,000 两个不同的资金配置",
                })
            
            # 检查是否有矛盾的系统位置
            if "Qbot-lab" in content and "Qbot" in content and "Qbot-lab" not in content.replace("Qbot-lab", ""):
                conflicts.append({
                    "type": "系统位置矛盾",
                    "description": "发现 Qbot 和 Qbot-lab 两个不同的系统位置",
                })
        except:
            pass
    
    return conflicts


def suggest_memory_cleanup() -> list[dict[str, Any]]:
    """建议记忆清理"""
    suggestions = []
    
    stats = get_memory_stats()
    
    # 如果记忆太多，建议清理
    if stats["memory_lines"] > 100:
        suggestions.append({
            "type": "MEMORY.md 太长",
            "description": f"MEMORY.md 有 {stats['memory_lines']} 行，建议精简",
        })
    
    if stats["learnings_lines"] > 200:
        suggestions.append({
            "type": "LEARNINGS.md 太长",
            "description": f"LEARNINGS.md 有 {stats['learnings_lines']} 行，建议精简",
        })
    
    return suggestions


def main():
    """主入口"""
    print("=== 记忆管理 ===")
    
    # 获取统计
    stats = get_memory_stats()
    print(f"MEMORY.md: {stats['memory_lines']} 行")
    print(f"LEARNINGS.md: {stats['learnings_lines']} 行")
    print(f"memory/ 目录: {stats['memory_files']} 个文件, {stats['total_memory_lines']} 行")
    
    # 检查冲突
    conflicts = check_memory_conflicts()
    if conflicts:
        print("\n⚠️ 发现记忆冲突:")
        for c in conflicts:
            print(f"  - {c['type']}: {c['description']}")
    
    # 建议清理
    suggestions = suggest_memory_cleanup()
    if suggestions:
        print("\n💡 建议清理:")
        for s in suggestions:
            print(f"  - {s['type']}: {s['description']}")


if __name__ == "__main__":
    main()