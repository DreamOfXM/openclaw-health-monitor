#!/usr/bin/env python3
"""
Qbot 每日复盘 - 验证交易、计算盈亏、同步主人
"""

import json
import time
from pathlib import Path
from datetime import datetime

QBOT_DIR = Path("/Users/hangzhou/Desktop/Qbot-lab")
RESULTS_DIR = QBOT_DIR / "results" / "a_share_sim"
LOGS_DIR = QBOT_DIR / "results" / "openclaw_logs"
MEMORY_DIR = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi/memory")

def get_latest_trade_log():
    """获取最新的交易日志"""
    if not LOGS_DIR.exists():
        return None
    
    logs = sorted(LOGS_DIR.glob("*.log"), reverse=True)
    if not logs:
        return None
    
    with open(logs[0]) as f:
        return json.load(f)

def get_trade_results(phase="close_phase"):
    """获取交易结果"""
    phase_dir = RESULTS_DIR / phase
    if not phase_dir.exists():
        return None
    
    # 读取交易记录
    trades_file = phase_dir / "simulation" / "trades.csv"
    positions_file = phase_dir / "simulation" / "positions.csv"
    daily_pnl_file = phase_dir / "simulation" / "daily_pnl.csv"
    
    results = {
        "trades": [],
        "positions": [],
        "daily_pnl": []
    }
    
    if trades_file.exists():
        import csv
        with open(trades_file) as f:
            reader = csv.DictReader(f)
            results["trades"] = list(reader)
    
    if positions_file.exists():
        import csv
        with open(positions_file) as f:
            reader = csv.DictReader(f)
            results["positions"] = list(reader)
    
    if daily_pnl_file.exists():
        import csv
        with open(daily_pnl_file) as f:
            reader = csv.DictReader(f)
            results["daily_pnl"] = list(reader)
    
    return results

def calculate_account_status(trade_log, results):
    """计算账户状态"""
    config = trade_log.get("config", {})
    
    # 初始资金
    initial_cash = config.get("initial_cash", 10000)
    
    # 当前持仓
    positions = results.get("positions", [])
    total_position_value = sum(float(p.get("market_value", 0)) for p in positions)
    
    # 可用资金（简化计算）
    available_cash = initial_cash - total_position_value
    
    # 总资产
    total_assets = available_cash + total_position_value
    
    # 当日盈亏
    daily_pnl = results.get("daily_pnl", [])
    today_pnl = 0
    if daily_pnl:
        today_pnl = float(daily_pnl[-1].get("pnl", 0))
    
    return {
        "initial_cash": initial_cash,
        "position_value": total_position_value,
        "available_cash": available_cash,
        "total_assets": total_assets,
        "today_pnl": today_pnl,
        "today_pnl_pct": (today_pnl / initial_cash * 100) if initial_cash > 0 else 0
    }

def generate_daily_report(trade_log, results, account):
    """生成每日报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    report = f"""# 每日复盘 - {today}

## 账户状态
- 本金：{account['initial_cash']:,.0f} 元
- 持仓市值：{account['position_value']:,.0f} 元
- 可用资金：{account['available_cash']:,.0f} 元
- 总资产：{account['total_assets']:,.0f} 元
- 当日盈亏：{account['today_pnl']:,.0f} 元（{account['today_pnl_pct']:.2f}%）

## 交易记录
"""
    
    trades = results.get("trades", [])
    if trades:
        report += "| 股票 | 操作 | 价格 | 数量 | 金额 |\n"
        report += "|------|------|------|------|------|\n"
        for t in trades[:10]:
            price = float(t.get('price', 0) or 0)
            quantity = int(t.get('quantity', 0) or 0)
            amount = float(t.get('amount', 0) or 0)
            report += f"| {t.get('symbol', 'N/A')} | {t.get('direction', 'N/A')} | {price:.2f} | {quantity} | {amount:,.0f} |\n"
    else:
        report += "今日无交易\n"
    
    report += "\n## 持仓情况\n"
    
    positions = results.get("positions", [])
    if positions:
        report += "| 股票 | 数量 | 成本 | 现价 | 盈亏 | 仓位 |\n"
        report += "|------|------|------|------|------|------|\n"
        for p in positions[:10]:
            cost_price = float(p.get('cost_price', 0) or 0)
            current_price = float(p.get('current_price', 0) or 0)
            pnl_pct = float(p.get('pnl_pct', 0) or 0)
            weight = float(p.get('weight', 0) or 0)
            report += f"| {p.get('symbol', 'N/A')} | {p.get('quantity', 0)} | {cost_price:.2f} | {current_price:.2f} | {pnl_pct:.2f}% | {weight:.2f}% |\n"
    else:
        report += "当前无持仓\n"
    
    report += f"""
## 风险提示
- 本金剩余：{account['available_cash']:,.0f} 元
- 风险状态：{"正常" if account['available_cash'] > 5000 else "警告"}

## 下一步
- 继续监控持仓
- 等待信号触发
- 严格控制风险

---

**核心原则：本金安全第一，收益第二。**
"""
    
    return report

def save_report(report):
    """保存报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    report_file = MEMORY_DIR / f"{today}-qbot-review.md"
    
    with open(report_file, "w") as f:
        f.write(report)
    
    return report_file

def main():
    print(f"=== Qbot 每日复盘 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # 获取最新交易日志
    print("\n1. 获取交易日志...")
    trade_log = get_latest_trade_log()
    if not trade_log:
        print("   未找到交易日志")
        return 1
    
    print(f"   状态: {trade_log.get('status', 'unknown')}")
    print(f"   交易次数: {trade_log.get('trade_count', 0)}")
    
    # 获取交易结果
    print("\n2. 获取交易结果...")
    results = get_trade_results()
    if not results:
        print("   未找到交易结果")
        return 1
    
    print(f"   交易记录: {len(results.get('trades', []))} 条")
    print(f"   持仓记录: {len(results.get('positions', []))} 条")
    
    # 计算账户状态
    print("\n3. 计算账户状态...")
    account = calculate_account_status(trade_log, results)
    
    print(f"   本金: {account['initial_cash']:,.0f} 元")
    print(f"   总资产: {account['total_assets']:,.0f} 元")
    print(f"   当日盈亏: {account['today_pnl']:,.0f} 元 ({account['today_pnl_pct']:.2f}%)")
    
    # 生成报告
    print("\n4. 生成每日报告...")
    report = generate_daily_report(trade_log, results, account)
    
    # 保存报告
    print("\n5. 保存报告...")
    report_file = save_report(report)
    print(f"   已保存到 {report_file}")
    
    # 风险检查
    print("\n6. 风险检查...")
    if account['available_cash'] < 5000:
        print("   ⚠️ 警告：本金不足 5000 元")
    else:
        print("   ✅ 风险正常")
    
    print("\n=== 完成 ===")
    return 0

if __name__ == "__main__":
    exit(main())
