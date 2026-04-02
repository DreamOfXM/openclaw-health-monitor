#!/usr/bin/env python3
"""
Agent 主动发现问题机制

让 Agent 能自己发现问题、自己解决，不需要主人追着改。

这是自我进化系统的核心。
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DISCOVERIES_FILE = DATA_DIR / "agent-discoveries.jsonl"


class AgentDiscovery:
    """Agent 主动发现问题"""
    
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.discoveries_file = DISCOVERIES_FILE
    
    def _log(self, entry: dict[str, Any]) -> None:
        """写入日志"""
        with open(self.discoveries_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def discover_problem(
        self,
        problem_type: str,
        description: str,
        severity: str = "medium",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发现问题"""
        now = int(time.time())
        entry = {
            "discovery_id": f"discovery-{now}",
            "problem_type": problem_type,
            "description": description,
            "severity": severity,
            "status": "discovered",
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._log(entry)
        return entry
    
    def propose_solution(
        self,
        discovery_id: str,
        solution: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """提出解决方案"""
        now = int(time.time())
        entry = {
            "discovery_id": discovery_id,
            "solution": solution,
            "status": "proposed",
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._log(entry)
        return entry
    
    def implement_solution(
        self,
        discovery_id: str,
        implementation: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """实施解决方案"""
        now = int(time.time())
        entry = {
            "discovery_id": discovery_id,
            "implementation": implementation,
            "status": "implemented",
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._log(entry)
        return entry
    
    def verify_solution(
        self,
        discovery_id: str,
        verification_result: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """验证解决方案"""
        now = int(time.time())
        entry = {
            "discovery_id": discovery_id,
            "verification_result": verification_result,
            "status": "verified",
            "created_at": now,
            "iso_time": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._log(entry)
        return entry
    
    def scan_for_problems(self) -> list[dict[str, Any]]:
        """扫描系统问题"""
        problems = []
        
        # 检查 background_root_missing
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from state_store import MonitorStateStore
        
        store = MonitorStateStore(BASE_DIR)
        tasks = store.list_tasks(limit=500)
        
        background_root_missing_count = 0
        for task in tasks:
            control = store.derive_task_control_state(task.get("task_id"))
            status = str(task.get("status") or "")
            root_task_id = str(task.get("root_task_id") or "")
            if (
                control.get("blocked_reason") == "background_root_task_missing"
                or (status == "background" and (not root_task_id or (root_task_id and not store.get_root_task(root_task_id))))
                or (status == "blocked" and str(task.get("blocked_reason") or "") == "background_root_task_missing")
            ):
                background_root_missing_count += 1
        
        if background_root_missing_count > 0:
            problems.append({
                "problem_type": "background_root_missing",
                "description": f"发现 {background_root_missing_count} 个孤儿任务",
                "severity": "high",
            })
        
        # 检查 blocked 任务
        blocked_count = sum(1 for t in tasks if str(t.get("status") or "") == "blocked")
        if blocked_count > 10:
            problems.append({
                "problem_type": "too_many_blocked",
                "description": f"发现 {blocked_count} 个阻塞任务",
                "severity": "medium",
            })
        
        return problems


def main():
    """主入口"""
    discovery = AgentDiscovery()
    
    # 扫描问题
    problems = discovery.scan_for_problems()
    print(f"发现 {len(problems)} 个问题")
    for p in problems:
        print(f"  {p['problem_type']}: {p['description']}")


if __name__ == "__main__":
    main()