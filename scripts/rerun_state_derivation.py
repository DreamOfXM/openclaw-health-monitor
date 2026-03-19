#!/usr/bin/env python3
"""
重跑历史任务状态派生脚本

此脚本用于重新派生所有历史任务的状态，修复 current-task-facts.json 中的数据。
"""

import sys
import time
import os
from pathlib import Path

# 添加父目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_store import MonitorStateStore

def main():
    print("开始重跑历史任务状态派生...")
    
    # 初始化状态存储
    base_dir = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    store = MonitorStateStore(base_dir)
    
    # 获取所有任务（包括已完成和进行中的）
    # 使用较大的 limit 来获取所有任务
    all_tasks = store.list_tasks(limit=1000, statuses=None)
    
    print(f"找到 {len(all_tasks)} 个任务")
    
    success_count = 0
    error_count = 0
    
    for i, task in enumerate(all_tasks):
        task_id = task.get("task_id")
        if not task_id:
            continue
            
        try:
            # 重新同步 legacy task projection
            store.sync_legacy_task_projection(task_id)
            
            # 重新派生核心任务监督状态
            supervision = store.derive_core_task_supervision(task_id)
            
            success_count += 1
            if (i + 1) % 10 == 0:
                print(f"已处理 {i + 1}/{len(all_tasks)} 个任务...")
                
        except Exception as e:
            error_count += 1
            print(f"处理任务 {task_id} 时出错: {e}")
    
    print(f"\n重跑完成！")
    print(f"成功: {success_count}")
    print(f"失败: {error_count}")
    
    # 验证当前任务事实文件
    print("\n验证当前任务事实文件...")
    try:
        import json
        
        facts_path = base_dir / "data" / "current-task-facts.json"
        if facts_path.exists():
            with open(facts_path, "r") as f:
                facts = json.load(f)
            
            current_task = facts.get("current_task", {})
            print(f"当前任务 ID: {current_task.get('task_id', 'N/A')}")
            print(f"当前任务状态: {current_task.get('status', 'N/A')}")
            print(f"证据级别: {facts.get('evidence_level', 'N/A')}")
            print(f"控制状态: {facts.get('control_state', 'N/A')}")
        else:
            print(f"事实文件不存在: {facts_path}")
    except Exception as e:
        print(f"验证事实文件时出错: {e}")
    
    return 0 if error_count == 0 else 1

if __name__ == "__main__":
    sys.exit(main())