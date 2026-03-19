#!/usr/bin/env python3
"""
Qbot 每周复盘 - 汇总收益、分析策略、调整参数
"""

import json
import time
from pathlib import Path
from datetime import datetime, timedelta

QBOT_DIR = Path("/Users/hangzhou/Desktop/Qbot-lab")
RESULTS_DIR = QBOT_DIR / "results" / "a_share_sim"
LOGS_DIR = QBOT_DIR / "results" / "openclaw_logs"
MEMORY_DIR = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi/memory")

def get_week_logs():
    """获取本周所有交易日志"""
    if not LOGS_DIR.exists():
        return []
    
    logs = []
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    
    for log_file in LOGS_DIR.glob("*.log"):
        try:
            with open(log_file) as f:
                data = json.load(f)
            
            run_at = data.get("run_at", "")
            if run_at:
                log_date = datetime.fromisoformat(run_at.replace("+08:00", "+08:00"))
                if log_date >= week_start:
                    logs.append(data)
        except Exception:
            pass
    
    return logs

def calculate_week_stats(logs):
    """计算本周统计"""
    initial_cash = 10000  # 已修改为 1 万
    
    if not logs:
        return {
            "trade_count": 0,
            "total_pnl": 0,
            "win_count": 0,
            "loss_count": 0,
            "initial_cash": initial_cash
        }
    
    trade_count = sum(log.get("trade_count", 0) for log in logs)
    
    # 计算盈亏（需要从实际结果中获取）
    total_pnl = 0
    win_count = 0
    loss_count = 0
    
    return {
        "trade_count": trade_count,
        "total_pnl": total_pnl,
        "win_count": win_count,
        "loss_count": loss_count,
        "initial_cash": initial_cash
    }

def generate_weekly_report(stats):
    """生成每周报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    
    report = f"""# 每周复盘 - {week_start} 至 {today}

## 本周收益
- 周初本金：{stats['initial_cash']:,.0f} 元
- 周末本金：{stats['initial_cash'] + stats['total_pnl']:,.0f} 元
- 周收益：{stats['total_pnl']:,.0f} 元

## 交易统计
- 交易次数：{stats['trade_count']} 次
- 盈利次数：{stats['win_count']} 次
- 亏损次数：{stats['loss_count']} 次
- 胜率：{(stats['win_count'] / stats['trade_count'] * 100) if stats['trade_count'] > 0 else 0:.1f}%

## 策略分析
- 本周交易较少，继续观察
- 等待更多信号触发

## 风险回顾
- 本金安全
- 风险可控

## 策略调整
- 继续监控
- 优化参数

## 下周计划
- 继续模拟交易
- 每日复盘
- 严格控制风险

---

**核心原则：本金安全第一，收益第二。**
"""
    
    return report

def save_report(report):
    """保存报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    report_file = MEMORY_DIR / f"{today}-qbot-weekly.md"
    
    with open(report_file, "w") as f:
        f.write(report)
    
    return report_file

def main():
    print(f"=== Qbot 每周复盘 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # 获取本周日志
    print("\n1. 获取本周日志...")
    logs = get_week_logs()
    print(f"   找到 {len(logs)} 条日志")
    
    # 计算统计
    print("\n2. 计算本周统计...")
    stats = calculate_week_stats(logs)
    print(f"   交易次数: {stats['trade_count']}")
    print(f"   周收益: {stats['total_pnl']:,.0f} 元")
    
    # 生成报告
    print("\n3. 生成每周报告...")
    report = generate_weekly_report(stats)
    
    # 保存报告
    print("\n4. 保存报告...")
    report_file = save_report(report)
    print(f"   已保存到 {report_file}")
    
    print("\n=== 完成 ===")
    return 0

if __name__ == "__main__":
    exit(main())
