#!/usr/bin/env python3
"""
Task Watcher - 异步任务监控器

功能：
1. 任务注册：register_task()
2. 状态轮询：watch_tasks()
3. 完成回调：on_complete_callback()

这是自我进化系统的核心组件：
- 把异步监控下沉到 cron 级别
- 不依赖 Agent session 持久化等待
- 所有状态持久化到 tasks.jsonl

使用：
    from task_watcher import TaskWatcher
    
    watcher = TaskWatcher()
    watcher.register_task(task_id, callback_url, timeout_seconds)
    watcher.watch_tasks()  # 在 cron 中调用
"""

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Callable

BASE_DIR = Path(__file__).parent.parent
SHARED_CONTEXT = BASE_DIR / "shared-context"
MONITOR_TASKS_DIR = SHARED_CONTEXT / "monitor-tasks"
TASKS_FILE = MONITOR_TASKS_DIR / "tasks.jsonl"
WATCHER_LOG = MONITOR_TASKS_DIR / "watcher.log"
AUDIT_LOG = MONITOR_TASKS_DIR / "audit.log"
DLQ_FILE = MONITOR_TASKS_DIR / "dlq.jsonl"


class TaskWatcher:
    """异步任务监控器"""
    
    def __init__(self):
        MONITOR_TASKS_DIR.mkdir(parents=True, exist_ok=True)
        self.tasks_file = TASKS_FILE
        self.watcher_log = WATCHER_LOG
        self.audit_log = AUDIT_LOG
        self.dlq_file = DLQ_FILE
    
    def _log(self, message: str, level: str = "INFO") -> None:
        """写入 watcher.log"""
        timestamp = datetime.now().isoformat()
        line = f"{timestamp} [{level}] {message}\n"
        with open(self.watcher_log, "a", encoding="utf-8") as f:
            f.write(line)
    
    def _audit(self, event: str, task_id: str, details: dict[str, Any]) -> None:
        """写入 audit.log"""
        entry = {
            "timestamp": int(time.time()),
            "iso_time": datetime.now().isoformat(),
            "event": event,
            "task_id": task_id,
            "details": details,
        }
        with open(self.audit_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def _dlq(self, task: dict[str, Any], reason: str) -> None:
        """写入死信队列"""
        entry = {
            "timestamp": int(time.time()),
            "iso_time": datetime.now().isoformat(),
            "task": task,
            "reason": reason,
        }
        with open(self.dlq_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    
    def register_task(
        self,
        task_id: str,
        callback_url: str | None = None,
        timeout_seconds: int = 3600,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """注册一个需要监控的任务"""
        now = int(time.time())
        task = {
            "task_id": task_id,
            "status": "registered",
            "callback_url": callback_url,
            "timeout_seconds": timeout_seconds,
            "created_at": now,
            "updated_at": now,
            "check_count": 0,
            "last_check_at": None,
            "completed_at": None,
            "delivered_at": None,
            "metadata": metadata or {},
        }
        
        with open(self.tasks_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
        
        self._audit("task_registered", task_id, {"timeout_seconds": timeout_seconds})
        self._log(f"Task registered: {task_id}")
        
        return task
    
    def update_task(self, task_id: str, **kwargs: Any) -> None:
        """更新任务状态"""
        now = int(time.time())
        
        # 读取所有任务
        tasks = []
        if self.tasks_file.exists():
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        task = json.loads(line)
                        tasks.append(task)
                    except json.JSONDecodeError:
                        continue
        
        # 更新匹配的任务
        updated = False
        for task in tasks:
            if task.get("task_id") == task_id:
                task.update(kwargs)
                task["updated_at"] = now
                updated = True
                break
        
        # 写回文件
        if updated:
            with open(self.tasks_file, "w", encoding="utf-8") as f:
                for task in tasks:
                    f.write(json.dumps(task, ensure_ascii=False) + "\n")
            
            self._audit("task_updated", task_id, kwargs)
    
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """获取任务信息"""
        if not self.tasks_file.exists():
            return None
        
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    task = json.loads(line)
                    if task.get("task_id") == task_id:
                        return task
                except json.JSONDecodeError:
                    continue
        
        return None
    
    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """列出任务"""
        if not self.tasks_file.exists():
            return []
        
        tasks = []
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    task = json.loads(line)
                    if status is None or task.get("status") == status:
                        tasks.append(task)
                except json.JSONDecodeError:
                    continue
        
        return tasks[-limit:]
    
    def check_task_completion(self, task_id: str) -> bool:
        """检查任务是否完成（需要子类实现具体逻辑）"""
        # 默认实现：检查任务状态
        task = self.get_task(task_id)
        if not task:
            return False
        
        return task.get("status") in ("completed", "failed", "delivered")
    
    def on_complete_callback(self, task: dict[str, Any]) -> None:
        """任务完成回调（需要子类实现具体逻辑）"""
        self._log(f"Task completed: {task.get('task_id')}")
        self._audit("task_completed", task.get("task_id", ""), {"status": task.get("status")})
    
    def watch_tasks(self) -> dict[str, int]:
        """监控所有注册的任务"""
        now = int(time.time())
        result = {
            "checked": 0,
            "completed": 0,
            "timeout": 0,
            "dlq": 0,
        }
        
        tasks = self.list_tasks(status="registered")
        
        for task in tasks:
            task_id = task.get("task_id")
            if not task_id:
                continue
            
            result["checked"] += 1
            
            # 更新检查计数
            self.update_task(
                task_id,
                check_count=task.get("check_count", 0) + 1,
                last_check_at=now,
            )
            
            # 检查是否完成
            if self.check_task_completion(task_id):
                self.update_task(task_id, status="completed", completed_at=now)
                self.on_complete_callback(task)
                result["completed"] += 1
                continue
            
            # 检查是否超时
            created_at = task.get("created_at", now)
            timeout_seconds = task.get("timeout_seconds", 3600)
            if now - created_at > timeout_seconds:
                self.update_task(task_id, status="timeout")
                self._dlq(task, "timeout")
                result["timeout"] += 1
                result["dlq"] += 1
        
        if result["checked"] > 0:
            self._log(f"Watched {result['checked']} tasks, completed {result['completed']}, timeout {result['timeout']}")
        
        return result


def main():
    """主入口（用于 cron 调用）"""
    watcher = TaskWatcher()
    result = watcher.watch_tasks()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()