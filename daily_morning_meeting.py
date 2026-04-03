#!/usr/bin/env python3
"""
真正的每日晨会系统

核心原则：
1. 有结果：每个讨论必须有明确结论
2. 有执行：行动项必须被追踪直到完成
3. 有沉淀：错误必须写入 LEARNINGS.md
4. 有成长：问题必须减少，不能重复出现
5. 有送达：晨会完成必须发送给主人（LRN-20260321-005）

流程：
1. 收集数据（昨日任务、问题、信号）
2. 分析根因（为什么出问题？）
3. 生成行动项（具体做什么？）
4. 执行行动项（真正去做）
5. 验证效果（问题是否减少？）
6. 沉淀知识（写入 LEARNINGS.md）
7. 发送给主人（必须送达）
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 默认接收晨会消息的用户（Feishu open_id）
DEFAULT_RECIPIENT_OPEN_ID = "ou_2b6d39ab847fff83c5427b16882a0d9f"


class MorningMeeting:
    """真正的晨会系统"""
    
    def __init__(self, base_dir: Path, workspace_dir: Path):
        self.base_dir = base_dir
        self.workspace_dir = workspace_dir
        self.learnings_path = workspace_dir / ".learnings" / "LEARNINGS.md"
        self.meetings_dir = workspace_dir / "meetings"
        self.meetings_dir.mkdir(parents=True, exist_ok=True)

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        """Best-effort numeric coercion for meeting metrics."""
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                cleaned = value.strip().replace(',', '').replace('%', '')
                if cleaned == '':
                    return default
                return float(cleaned)
        except Exception:
            pass
        return default

    def _build_discussion_sections(self, data: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the three mandatory morning-meeting discussion sections."""
        sections: list[dict[str, Any]] = []

        system_points: list[str] = []
        if data['problems']:
            for item in data['problems'][:3]:
                system_points.append(f"问题 {item['problem_code']} 出现 {item['count']} 次")
        else:
            system_points.append('最近未检测到新的高频系统问题')
        for issue in analysis.get('task_issues', [])[:2]:
            system_points.append(f"{issue['issue']}：{issue['root_cause']}（{issue['evidence']}）")
        sections.append({
            'title': 'OpenClaw 问题与解决',
            'agent_mode': '该议题需要 builder / guardrail / main 全程参与，不单独成段',
            'summary': '只讨论真实问题；没有问题就不硬凑解决方案。',
            'points': system_points,
        })

        projects_data = self._load_projects_data() or {}
        summary = projects_data.get('summary', {}) if isinstance(projects_data, dict) else {}
        monthly_target = self._to_float(summary.get('monthly_target', 0))
        monthly_achieved = self._to_float(summary.get('monthly_achieved', 0))
        gap = self._to_float(summary.get('gap_to_target', monthly_target - monthly_achieved))
        wealth_points = [
            f'本月目标 {monthly_target:.1f} 元，当前已实现 {monthly_achieved:.1f} 元，差距 {gap:.1f} 元',
        ]
        running_projects = []
        if isinstance(projects_data, dict):
            running_projects = [p for p in projects_data.get('projects', []) if p.get('status') == 'running']
        if running_projects:
            for proj in running_projects[:3]:
                wealth_points.append(
                    f"项目 {proj.get('name', '未知项目')}：下一步 {str(proj.get('next_action', '待补'))[:40]}"
                )
        else:
            wealth_points.append('当前缺少真实项目台账，先补齐项目数据再做优先级排序')
        sections.append({
            'title': '发财致富方案',
            'agent_mode': '该议题也必须让相关子 agent 参与论证，而不是我单口输出',
            'summary': '输出赚钱方向、依据、优先级、风险和下一步。',
            'points': wealth_points,
        })

        domain_points: list[str] = []
        running_projects = [p for p in (projects_data.get('projects', []) if isinstance(projects_data, dict) else []) if p.get('status') == 'running']
        for proj in running_projects[:3]:
            domain_points.append(
                f"项目域 {proj.get('name', '未知项目')}（{proj.get('type', 'unknown')}）：下一步 {str(proj.get('next_action', '待补'))[:50]}"
            )
        signals = data.get('signals', {})
        missing_domains = list(signals.get('missing_domains', []) or [])
        if missing_domains:
            domain_points.append(f"缺少结构化输入的任务域：{', '.join(missing_domains[:5])}")
        if not domain_points:
            domain_points.append('当前没有需要单独升级为晨会主议题的任务域')
        sections.append({
            'title': '任务域讨论',
            'agent_mode': '按当前运行中的项目域讨论，不允许默认把单一项目写成系统主线',
            'summary': '根据运行中的项目域，讨论其下一步、阻塞与是否需要资源倾斜。',
            'points': domain_points,
        })

        return sections
        
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
        
        # 生成晨会三大议题与结论
        result["discussion_sections"] = self._build_discussion_sections(data, analysis)
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
        
        # 发送给主人（必须送达）
        self._send_to_user(result)
        
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
        
        # 任务域输入（默认不绑定单一项目）
        domain_inputs = self._get_domain_inputs()
        
        return {
            "tasks": tasks_data,
            "problems": problems_list,
            "signals": domain_inputs,
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
        
        # 分析任务域输入
        missing_domains = list((data.get("signals") or {}).get("missing_domains") or [])
        if missing_domains:
            analysis["signal_issues"].append({
                "issue": "部分任务域输入缺失",
                "root_cause": "通用上下文输入未按约定产出或缺少更新",
                "evidence": f"缺失任务域: {', '.join(missing_domains[:5])}",
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
        
        # 行动3：补齐任务域输入
        missing_domains = list((data.get("signals") or {}).get("missing_domains") or [])
        if missing_domains:
            actions.append({
                "action": "补齐缺失的任务域输入",
                "type": "generate_report",
                "priority": "high",
                "details": f"缺失任务域: {', '.join(missing_domains[:5])}",
                "execute": lambda: self._refresh_domain_inputs(missing_domains),
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
        """更新约束规则。

        晨会不再生成“每次遇到此问题，必须检查并修复”这类空泛约束；
        没有具体改动、验证方法、追踪期时，只记录为待主脑处理。
        """
        return {
            "updated": False,
            "reason": "generic_constraint_disabled",
            "next_step": "需要主脑补充具体代码改动、验证结果和追踪期后，才能形成真实约束",
        }
    
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
        """生成结论（围绕三大议题，且数值字段统一安全转换）"""
        conclusions: list[str] = []

        rate = self._to_float(data.get("tasks", {}).get("completion_rate", 0))
        if rate >= 80:
            conclusions.append(f"✅ 昨日表现优秀，完成率 {rate:.1f}%")
        elif rate >= 50:
            conclusions.append(f"⚠️ 昨日表现一般，完成率 {rate:.1f}%，需改进")
        else:
            conclusions.append(f"❌ 昨日表现不佳，完成率 {rate:.1f}%，需重点关注")

        if execution.get("completed"):
            conclusions.append(f"✅ 已执行 {len(execution['completed'])} 个行动项")
        if execution.get("failed"):
            conclusions.append(f"❌ {len(execution['failed'])} 个行动项执行失败")

        if data.get("problems"):
            top_problem = data["problems"][0]
            conclusions.append(f"🧠 OpenClaw 当前首要问题：{top_problem['problem_code']}（出现 {top_problem['count']} 次）")
        else:
            conclusions.append("🧠 OpenClaw 当前无新增高频问题，本议题无需额外解决方案")

        projects_data = self._load_projects_data() or {}
        summary = projects_data.get("summary", {}) if isinstance(projects_data, dict) else {}
        monthly_target = self._to_float(summary.get("monthly_target", 0))
        monthly_achieved = self._to_float(summary.get("monthly_achieved", 0))
        gap = self._to_float(summary.get("gap_to_target", monthly_target - monthly_achieved))
        conclusions.append(f"💰 资源主线：本月目标 {monthly_target:.1f} 元，当前 {monthly_achieved:.1f} 元，差距 {gap:.1f} 元")

        running_projects = [p for p in (projects_data.get("projects", []) if isinstance(projects_data, dict) else []) if p.get("status") == "running"]
        if running_projects:
            top_project = running_projects[0]
            conclusions.append(
                f"📦 当前主项目域：{top_project.get('name', '未知项目')}（{top_project.get('type', 'unknown')}），下一步 {top_project.get('next_action', '待补')}"
            )
        missing_domains = list((data.get("signals") or {}).get("missing_domains") or [])
        if missing_domains:
            conclusions.append(f"🧩 当前缺失输入的任务域：{', '.join(missing_domains[:5])}")

        conclusions.append("🤝 工作方式：以上三大议题都必须让相关子 agent 参与讨论，不允许变成我的独角戏")
        return conclusions

    def _load_projects_data(self) -> dict[str, Any] | None:
        """加载项目追踪数据"""
        projects_path = self.workspace_dir / "projects.json"
        if not projects_path.exists():
            return None
        try:
            return json.loads(projects_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _get_domain_inputs(self) -> dict[str, Any]:
        """检查通用任务域输入，不默认绑定单一项目。"""
        intel_dir = self.workspace_dir / "shared-context" / "intel"
        expected = {"daily-check", "system-focus"}
        present = set()
        if intel_dir.exists():
            for path in intel_dir.glob("*.json"):
                present.add(path.stem)
        missing = sorted(expected - present)
        return {
            "status": "ready" if not missing else "partial",
            "missing_domains": missing,
            "present_domains": sorted(present),
        }

    def _refresh_domain_inputs(self, missing_domains: list[str]) -> bool:
        """补齐最基础的通用任务域输入骨架。"""
        intel_dir = self.workspace_dir / "shared-context" / "intel"
        intel_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now().isoformat()
        changed = False
        templates = {
            "daily-check": {
                "updated_at": now,
                "source": "main",
                "summary": "待补今日主脑检查结果",
                "consumers": ["all"],
            },
            "system-focus": {
                "updated_at": now,
                "source": "main",
                "summary": "待补当前系统主线与优先级",
                "consumers": ["all"],
            },
        }
        for domain in missing_domains:
            if domain not in templates:
                continue
            path = intel_dir / f"{domain}.json"
            if not path.exists():
                path.write_text(json.dumps(templates[domain], ensure_ascii=False, indent=2), encoding="utf-8")
                changed = True
        return changed
    
    def _load_a_share_data(self) -> dict[str, Any] | None:
        """加载 A 股交易数据"""
        # 尝试读取最新的交易回执
        receipt_dir = Path("/Users/hangzhou/Desktop/Qbot-lab/results/openclaw_receipts/latest")
        if not receipt_dir.exists():
            return None
        
        # 读取 daily.json 或 close.json
        for name in ["daily.json", "close.json", "status.json"]:
            path = receipt_dir / name
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    # 读取持仓数据
                    positions_path = Path("/Users/hangzhou/Desktop/Qbot-lab/results/a_share_sim/daily_cycle/simulation/positions.csv")
                    if positions_path.exists():
                        import csv
                        with positions_path.open("r", encoding="utf-8") as fp:
                            reader = csv.DictReader(fp)
                            data["positions"] = [row for row in reader if int(float(row.get("qty", 0))) > 0]
                    # 读取每日盈亏
                    daily_pnl_path = Path("/Users/hangzhou/Desktop/Qbot-lab/results/a_share_sim/daily_cycle/simulation/daily_pnl.csv")
                    if daily_pnl_path.exists():
                        import csv
                        with daily_pnl_path.open("r", encoding="utf-8") as fp:
                            reader = csv.DictReader(fp)
                            rows = list(reader)
                            if rows:
                                data["daily_pnl"] = rows[-1]  # 最新一行
                    return data
                except Exception:
                    continue
        return None
    
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
    
    def _send_to_user(self, result: dict[str, Any], open_id: str | None = None) -> bool:
        """
        发送晨会结论给用户（必须送达）
        
        根据 LRN-20260321-005 约束：晨会完成 != 主人收到结果
        必须在生成报告后发送消息给主人
        """
        target_open_id = open_id or DEFAULT_RECIPIENT_OPEN_ID
        if not target_open_id:
            print("⚠️ 未配置接收用户，跳过发送")
            return False
        
        # 构建简洁的消息内容（不是完整报告，而是结论摘要）
        lines = [
            "📅 **每日晨会** " + result["date"],
            "",
        ]
        
        # 三大议题摘要
        for section in result.get("discussion_sections", []):
            lines.append(f"## {section['title']}")
            lines.append(section.get("summary", ""))
            for point in section.get("points", [])[:4]:
                lines.append(f"- {point}")
            lines.append(f"- 协作要求：{section.get('agent_mode', '')}")
            lines.append("")

        # 核心结论
        for c in result["conclusions"]:
            lines.append(c)
        
        lines.extend([
            "",
            "---",
            "",
            "📊 **指标**",
            f"- 昨日完成率：{result['metrics']['yesterday_completion_rate']:.1f}%",
            f"- 发现问题：{result['metrics']['problems_found']} 类",
            f"- 执行行动：{result['metrics']['actions_executed']} 个",
            f"- 沉淀知识：{result['metrics']['learnings_captured']} 条",
        ])
        
        message = "\n".join(lines)
        
        # 使用 openclaw message send 发送
        target = f"user:{target_open_id}"
        openclaw_path = "/Users/hangzhou/Library/pnpm/openclaw"
        cmd = [
            openclaw_path, "message", "send",
            "--channel", "feishu",
            "--target", target,
            "--message", message,
        ]
        
        try:
            result_proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result_proc.returncode == 0:
                print(f"✅ 晨会结论已发送给主人")
                return True
            else:
                print(f"❌ 发送失败: {result_proc.stderr or result_proc.stdout}")
                return False
        except Exception as e:
            print(f"❌ 发送异常: {e}")
            return False
    
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
        
        for section in result.get("discussion_sections", []):
            lines.append(f"### {section['title']}")
            lines.append("")
            lines.append(section.get("summary", ""))
            lines.append("")
            for point in section.get("points", []):
                lines.append(f"- {point}")
            lines.append(f"- 协作要求：{section.get('agent_mode', '')}")
            lines.append("")

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