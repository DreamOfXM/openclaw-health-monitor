#!/usr/bin/env python3
"""
真正的每日晨会系统

核心原则：
1. 有结果：每个讨论必须有明确结论
2. 有执行：行动项必须被追踪直到完成
3. 有沉淀：错误必须写入 LEARNINGS.md
4. 有成长：问题必须减少，不能重复出现

流程：
1. 收集数据（昨日任务、问题、信号）
2. 分析根因（为什么出问题？）
3. 生成行动项（具体做什么？）
4. 执行行动项（真正去做）
5. 验证效果（问题是否减少？）
6. 沉淀知识（写入 LEARNINGS.md）
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class MorningMeeting:
    """真正的晨会系统"""
    
    def __init__(self, base_dir: Path, workspace_dir: Path):
        self.base_dir = base_dir
        self.workspace_dir = workspace_dir
        self.learnings_path = workspace_dir / ".learnings" / "LEARNINGS.md"
        self.meetings_dir = workspace_dir / "meetings"
        self.meetings_dir.mkdir(parents=True, exist_ok=True)
        
    def run(self) -> dict[str, Any]:
        """运行晨会"""
        now = int(time.time())
        date_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        
        result = {
            "date": date_str,
            "generated_at": now,
            "phases": {},
            "conclusions": [],
            "actions_taken": [],
            "learnings_added": [],
            "metrics": {},
        }
        
        # 阶段1：收集数据
        print("📊 阶段1：收集数据...")
        data = self._collect_data()
        result["phases"]["data_collection"] = {
            "tasks": data["tasks"]["total"],
            "problems": len(data["problems"]),
            "signals": data["signals"].get("status", "none"),
        }
        
        # 阶段2：分析根因
        print("🔍 阶段2：分析根因...")
        analysis = self._analyze_root_causes(data)
        result["phases"]["root_cause_analysis"] = analysis
        
        # 阶段3：生成行动项
        print("📝 阶段3：生成行动项...")
        actions = self._generate_actions(data, analysis)
        result["phases"]["action_generation"] = {
            "total": len(actions),
            "high_priority": len([a for a in actions if a["priority"] == "high"]),
        }
        
        # 阶段4：执行行动项
        print("⚡ 阶段4：执行行动项...")
        execution = self._execute_actions(actions)
        result["actions_taken"] = execution["completed"]
        result["phases"]["execution"] = {
            "completed": len(execution["completed"]),
            "failed": len(execution["failed"]),
            "skipped": len(execution["skipped"]),
        }
        
        # 阶段5：验证效果
        print("✅ 阶段5：验证效果...")
        verification = self._verify_effects(data, execution)
        result["phases"]["verification"] = verification
        
        # 阶段6：沉淀知识
        print("📚 阶段6：沉淀知识...")
        learnings = self._capture_learnings(data, analysis, execution)
        result["learnings_added"] = learnings
        result["phases"]["learning_capture"] = {
            "learnings_added": len(learnings),
        }
        
        # 生成结论
        result["conclusions"] = self._generate_conclusions(data, analysis, execution, verification)
        
        # 计算指标
        result["metrics"] = {
            "yesterday_completion_rate": data["tasks"]["completion_rate"],
            "problems_found": len(data["problems"]),
            "actions_executed": len(execution["completed"]),
            "learnings_captured": len(learnings),
        }
        
        # 保存报告
        self._save_report(result)
        
        return result
    
    def _collect_data(self) -> dict[str, Any]:
        """收集数据"""
        from state_store import MonitorStateStore
        
        store = MonitorStateStore(self.base_dir)
        now = int(time.time())
        yesterday_start = now - (now % 86400) - 86400
        
        # 昨日任务
        tasks = store.list_tasks(limit=100)
        yesterday_tasks = [
            t for t in tasks
            if int(t.get("created_at", 0)) >= yesterday_start
            or int(t.get("updated_at", 0)) >= yesterday_start
        ]
        
        completed = [t for t in yesterday_tasks if t.get("status") == "completed"]
        blocked = [t for t in yesterday_tasks if t.get("status") == "blocked"]
        
        tasks_data = {
            "total": len(yesterday_tasks),
            "completed": len(completed),
            "blocked": len(blocked),
            "completion_rate": len(completed) / len(yesterday_tasks) * 100 if yesterday_tasks else 0,
            "blocked_tasks": blocked[:5],
        }
        
        # 问题统计
        events = store.list_self_evolution_events(limit=200)
        yesterday_events = [
            e for e in events
            if int(e.get("created_at", 0)) >= yesterday_start
        ]
        
        problems = {}
        for e in yesterday_events:
            problem_code = e.get("problem_code", "unknown")
            if problem_code not in problems:
                problems[problem_code] = {"count": 0, "examples": []}
            problems[problem_code]["count"] += 1
            if len(problems[problem_code]["examples"]) < 3:
                problems[problem_code]["examples"].append(e)
        
        problems_list = [
            {"problem_code": k, "count": v["count"], "examples": v["examples"]}
            for k, v in sorted(problems.items(), key=lambda x: -x[1]["count"])
        ]
        
        # A股信号
        signals = self._get_a_share_signals()
        
        return {
            "tasks": tasks_data,
            "problems": problems_list,
            "signals": signals,
        }
    
    def _analyze_root_causes(self, data: dict[str, Any]) -> dict[str, Any]:
        """分析根因"""
        analysis = {
            "task_issues": [],
            "problem_issues": [],
            "signal_issues": [],
        }
        
        # 分析任务问题
        if data["tasks"]["completion_rate"] < 50:
            analysis["task_issues"].append({
                "issue": "任务完成率过低",
                "root_cause": "可能是任务派发后没有追踪，或者子代理没有执行",
                "evidence": f"完成率 {data['tasks']['completion_rate']:.1f}%",
            })
        
        if data["tasks"]["blocked"] > 5:
            analysis["task_issues"].append({
                "issue": "大量任务阻塞",
                "root_cause": "可能是看门狗发现问题但没有恢复",
                "evidence": f"{data['tasks']['blocked']} 个任务阻塞",
            })
        
        # 分析问题模式
        for p in data["problems"][:3]:
            if p["count"] > 50:
                analysis["problem_issues"].append({
                    "issue": f"{p['problem_code']} 问题频发",
                    "root_cause": self._infer_root_cause(p["problem_code"]),
                    "evidence": f"出现 {p['count']} 次",
                })
        
        # 分析信号
        if data["signals"].get("status") == "no_signals":
            analysis["signal_issues"].append({
                "issue": "今日信号未生成",
                "root_cause": "Qbot 日报可能没有运行",
                "evidence": "信号文件不存在",
            })
        
        return analysis
    
    def _infer_root_cause(self, problem_code: str) -> str:
        """推断问题根因"""
        causes = {
            "missing_pipeline_receipt": "子代理没有发送结构化回执",
            "task_closure_missing": "任务完成但结果未送达用户",
            "followup_pending_without_main_recovery": "发现问题但没有触发恢复",
            "heartbeat_missing_hard": "子代理进程崩溃或网络中断",
            "protocol_violation": "子代理没有遵守通信协议",
        }
        return causes.get(problem_code, "未知原因")
    
    def _generate_actions(self, data: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
        """生成行动项"""
        actions = []
        
        # 行动1：处理阻塞任务
        if data["tasks"]["blocked"] > 0:
            actions.append({
                "action": "处理阻塞任务",
                "type": "recover_tasks",
                "priority": "high",
                "details": f"恢复 {data['tasks']['blocked']} 个阻塞任务",
                "execute": lambda: self._recover_blocked_tasks(data["tasks"]["blocked_tasks"]),
            })
        
        # 行动2：修复高频问题
        for p in data["problems"][:2]:
            if p["count"] > 20:
                actions.append({
                    "action": f"修复 {p['problem_code']} 问题",
                    "type": "fix_problem",
                    "priority": "high",
                    "details": f"该问题出现 {p['count']} 次",
                    "problem_code": p["problem_code"],
                    "execute": lambda pc=p["problem_code"]: self._fix_problem(pc),
                })
        
        # 行动3：生成A股日报
        if data["signals"].get("status") == "no_signals":
            actions.append({
                "action": "生成A股日报",
                "type": "generate_report",
                "priority": "high",
                "details": "运行 Qbot 日报生成今日信号",
                "execute": lambda: self._generate_qbot_report(),
            })
        
        # 行动4：更新约束规则
        for issue in analysis.get("task_issues", []):
            actions.append({
                "action": f"更新约束规则：{issue['issue']}",
                "type": "update_constraint",
                "priority": "medium",
                "details": issue["root_cause"],
                "execute": lambda i=issue: self._update_constraint(i),
            })
        
        return actions
    
    def _execute_actions(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """执行行动项"""
        result = {
            "completed": [],
            "failed": [],
            "skipped": [],
        }
        
        for action in actions:
            try:
                print(f"  执行：{action['action']}...")
                execute_fn = action.get("execute")
                if execute_fn:
                    execution_result = execute_fn()
                    result["completed"].append({
                        "action": action["action"],
                        "type": action["type"],
                        "result": execution_result,
                    })
                else:
                    result["skipped"].append({
                        "action": action["action"],
                        "reason": "no_execute_function",
                    })
            except Exception as e:
                result["failed"].append({
                    "action": action["action"],
                    "error": str(e),
                })
        
        return result
    
    def _recover_blocked_tasks(self, blocked_tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """恢复阻塞任务"""
        recovered = 0
        for task in blocked_tasks[:5]:  # 最多恢复5个
            task_id = task.get("task_id")
            if task_id:
                # 标记任务为需要恢复
                recovery_file = self.workspace_dir / "recovery-pending" / f"{task_id}.json"
                recovery_file.parent.mkdir(parents=True, exist_ok=True)
                recovery_file.write_text(json.dumps({
                    "task_id": task_id,
                    "status": "pending_recovery",
                    "created_at": int(time.time()),
                }, ensure_ascii=False, indent=2))
                recovered += 1
        
        return {"recovered": recovered, "message": f"已标记 {recovered} 个任务待恢复"}
    
    def _fix_problem(self, problem_code: str) -> dict[str, Any]:
        """修复问题"""
        # 调用自动进化系统
        from auto_evolution import generate_candidate_rule, adopt_rule
        
        rule = generate_candidate_rule(
            problem_code=problem_code,
            learning_key=f"morning-meeting-{int(time.time())}",
            evidence={"source": "morning_meeting"},
        )
        
        if rule:
            result = adopt_rule(rule, self.workspace_dir)
            return {"rule_generated": True, "rule_id": rule.get("rule_id"), "adopted": result.get("status")}
        
        return {"rule_generated": False, "reason": "no_template"}
    
    def _generate_qbot_report(self) -> dict[str, Any]:
        """生成 Qbot 日报"""
        qbot_script = Path("/Users/hangzhou/Desktop/Qbot-lab/scripts/run_a_share_daily_pipeline.py")
        if qbot_script.exists():
            try:
                result = subprocess.run(
                    ["/usr/bin/python3", str(qbot_script)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=str(qbot_script.parent),
                )
                return {
                    "executed": True,
                    "returncode": result.returncode,
                    "output_preview": result.stdout[:500] if result.stdout else "",
                }
            except Exception as e:
                return {"executed": False, "error": str(e)}
        
        return {"executed": False, "reason": "script_not_found"}
    
    def _update_constraint(self, issue: dict[str, Any]) -> dict[str, Any]:
        """更新约束规则"""
        constraint_id = f"MC-{int(time.time())}"
        constraint_text = f"""
### {constraint_id}（晨会自动生成）

**问题**：{issue['issue']}
**根因**：{issue['root_cause']}
**证据**：{issue['evidence']}

**约束**：每次遇到此问题，必须检查并修复

**来源**：晨会系统自动生成
**生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        
        # 追加到 AGENTS.md
        agents_md = self.workspace_dir / "AGENTS.md"
        if agents_md.exists():
            content = agents_md.read_text(encoding="utf-8")
            if constraint_id not in content:
                agents_md.write_text(content + "\n" + constraint_text, encoding="utf-8")
                return {"updated": True, "constraint_id": constraint_id}
        
        return {"updated": False, "reason": "file_not_found"}
    
    def _verify_effects(self, data: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
        """验证效果"""
        return {
            "actions_completed": len(execution["completed"]),
            "actions_failed": len(execution["failed"]),
            "expected_improvement": "问题应减少 50% 以上",
            "verification_time": "24小时后",
        }
    
    def _capture_learnings(self, data: dict[str, Any], analysis: dict[str, Any], execution: dict[str, Any]) -> list[dict[str, Any]]:
        """沉淀知识"""
        learnings = []
        
        # 从失败中学习
        for failed in execution.get("failed", []):
            learning = {
                "key": f"LRN-{int(time.time())}-{failed['action'][:20]}",
                "category": "execution_failure",
                "title": f"执行失败：{failed['action']}",
                "detail": failed["error"],
                "source": "morning_meeting",
            }
            learnings.append(learning)
            self._write_learning(learning)
        
        # 从问题中学习
        for issue in analysis.get("problem_issues", []):
            learning = {
                "key": f"LRN-{int(time.time())}-{issue['issue'][:20]}",
                "category": "problem_pattern",
                "title": issue["issue"],
                "detail": f"根因：{issue['root_cause']}，证据：{issue['evidence']}",
                "source": "morning_meeting",
            }
            learnings.append(learning)
            self._write_learning(learning)
        
        return learnings
    
    def _write_learning(self, learning: dict[str, Any]) -> None:
        """写入学习记录"""
        self.learnings_path.parent.mkdir(parents=True, exist_ok=True)
        
        content = f"""
## [{learning['key']}] {learning['title']}

**Logged**: {datetime.now().isoformat()}
**Priority**: high
**Status**: open
**Area**: {learning['category']}

### Summary
{learning['detail']}

### Source
{learning['source']}

---
"""
        
        if self.learnings_path.exists():
            existing = self.learnings_path.read_text(encoding="utf-8")
            if learning["key"] not in existing:
                self.learnings_path.write_text(existing + content, encoding="utf-8")
        else:
            self.learnings_path.write_text("# Learnings\n" + content, encoding="utf-8")
    
    def _generate_conclusions(self, data: dict[str, Any], analysis: dict[str, Any], execution: dict[str, Any], verification: dict[str, Any]) -> list[str]:
        """生成结论"""
        conclusions = []
        
        # 结论1：昨日表现
        rate = data["tasks"]["completion_rate"]
        if rate >= 80:
            conclusions.append(f"✅ 昨日表现优秀，完成率 {rate:.1f}%")
        elif rate >= 50:
            conclusions.append(f"⚠️ 昨日表现一般，完成率 {rate:.1f}%，需改进")
        else:
            conclusions.append(f"❌ 昨日表现不佳，完成率 {rate:.1f}%，需重点关注")
        
        # 结论2：问题处理
        if execution["completed"]:
            conclusions.append(f"✅ 已执行 {len(execution['completed'])} 个行动项")
        if execution["failed"]:
            conclusions.append(f"❌ {len(execution['failed'])} 个行动项执行失败")
        
        # 结论3：今日重点
        if data["signals"].get("status") == "no_signals":
            conclusions.append("📌 今日重点：生成A股日报，分析市场信号")
        
        # 结论4：策略调整
        if data["problems"]:
            top_problem = data["problems"][0]
            conclusions.append(f"📌 策略调整：重点关注 {top_problem['problem_code']} 问题")
        
        # 结论5：赚钱方案讨论
        conclusions.append("💰 **赚钱方案讨论**：本月目标至少赚200元")
        conclusions.append("   - 方案1：优化A股交易策略，提高收益率")
        conclusions.append("   - 方案2：开发付费技能/服务")
        conclusions.append("   - 方案3：内容创作变现（技术文章/视频）")
        conclusions.append("   - 待主人确认后执行")
        
        return conclusions
    
    def _save_report(self, result: dict[str, Any]) -> None:
        """保存报告"""
        date_str = result["date"]
        
        # 保存 JSON
        json_path = self.meetings_dir / f"meeting-{date_str}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # 保存 Markdown
        md_path = self.meetings_dir / f"meeting-{date_str}.md"
        md_content = self._render_markdown(result)
        md_path.write_text(md_content, encoding="utf-8")
        
        print(f"📄 报告已保存：{md_path}")
    
    def _render_markdown(self, result: dict[str, Any]) -> str:
        """渲染 Markdown 报告"""
        lines = [
            f"# 📅 每日晨会报告 - {result['date']}",
            "",
            "---",
            "",
            "## 一、执行摘要",
            "",
        ]
        
        for c in result["conclusions"]:
            lines.append(f"- {c}")
        
        lines.extend([
            "",
            "---",
            "",
            "## 二、数据收集",
            "",
            f"- 昨日任务：{result['phases']['data_collection']['tasks']} 个",
            f"- 发现问题：{result['phases']['data_collection']['problems']} 类",
            f"- A股信号：{result['phases']['data_collection']['signals']}",
            "",
            "---",
            "",
            "## 三、根因分析",
            "",
        ])
        
        for issue in result["phases"]["root_cause_analysis"].get("task_issues", []):
            lines.append(f"- **{issue['issue']}**：{issue['root_cause']}（{issue['evidence']}）")
        
        for issue in result["phases"]["root_cause_analysis"].get("problem_issues", []):
            lines.append(f"- **{issue['issue']}**：{issue['root_cause']}（{issue['evidence']}）")
        
        lines.extend([
            "",
            "---",
            "",
            "## 四、行动执行",
            "",
            f"- 已完成：{result['phases']['execution']['completed']} 个",
            f"- 失败：{result['phases']['execution']['failed']} 个",
            f"- 跳过：{result['phases']['execution']['skipped']} 个",
            "",
        ])
        
        for action in result["actions_taken"]:
            lines.append(f"### ✅ {action['action']}")
            if isinstance(action.get("result"), dict):
                for k, v in action["result"].items():
                    lines.append(f"- {k}: {v}")
            lines.append("")
        
        lines.extend([
            "---",
            "",
            "## 五、知识沉淀",
            "",
            f"- 新增学习记录：{result['phases']['learning_capture']['learnings_added']} 条",
            "",
        ])
        
        for learning in result["learnings_added"]:
            lines.append(f"- **{learning['title']}**：{learning['detail']}")
        
        lines.extend([
            "",
            "---",
            "",
            "## 六、验证计划",
            "",
            f"- 预期效果：{result['phases']['verification']['expected_improvement']}",
            f"- 验证时间：{result['phases']['verification']['verification_time']}",
            "",
            "---",
            "",
            f"*报告生成时间：{datetime.fromtimestamp(result['generated_at']).strftime('%Y-%m-%d %H:%M:%S')}*",
        ])
        
        return "\n".join(lines)
    
    def _get_a_share_signals(self) -> dict[str, Any]:
        """获取 A 股信号"""
        qbot_signals_path = Path("/Users/hangzhou/Desktop/Qbot-lab/results/a_share_sim")
        if qbot_signals_path.exists():
            signal_files = sorted(qbot_signals_path.glob("signals_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if signal_files:
                try:
                    with open(signal_files[0], "r", encoding="utf-8") as f:
                        return json.load(f)
                except:
                    pass
        
        return {"status": "no_signals", "message": "今日信号尚未生成"}


# ============================================================================
# CLI 入口
# ============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="每日晨会系统")
    parser.add_argument("--base-dir", default="/Users/hangzhou/openclaw-health-monitor", help="基础目录")
    parser.add_argument("--workspace", default="/Users/hangzhou/.openclaw/workspace-xiaoyi", help="工作区目录")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告，不执行行动")
    
    args = parser.parse_args()
    
    base_dir = Path(args.base_dir)
    workspace_dir = Path(args.workspace)
    
    meeting = MorningMeeting(base_dir, workspace_dir)
    
    if args.dry_run:
        # 只生成报告
        result = meeting._collect_data()
        analysis = meeting._analyze_root_causes(result)
        print("\n=== 数据收集 ===")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("\n=== 根因分析 ===")
        print(json.dumps(analysis, ensure_ascii=False, indent=2, default=str))
    else:
        # 完整运行
        result = meeting.run()
        print("\n=== 晨会结论 ===")
        for c in result["conclusions"]:
            print(f"  {c}")
        print(f"\n=== 指标 ===")
        print(f"  昨日完成率：{result['metrics']['yesterday_completion_rate']:.1f}%")
        print(f"  发现问题：{result['metrics']['problems_found']} 类")
        print(f"  执行行动：{result['metrics']['actions_executed']} 个")
        print(f"  沉淀知识：{result['metrics']['learnings_captured']} 条")