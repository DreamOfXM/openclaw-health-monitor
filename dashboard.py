#!/usr/bin/env python3
"""
OpenClaw 健康监控仪表盘
Web 界面展示系统状态和健康信息
"""

import os
import sys
import json
import time
import socket
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request

from typing import Optional

from monitor_config import SECRET_KEYS, load_config as load_shared_config, save_local_config_value, sanitize_config_for_ui
from snapshot_manager import SnapshotManager
from state_store import MonitorStateStore

BASE_DIR = Path(__file__).parent
OPENCLAW_HOME = Path.home() / ".openclaw"
CHANGE_LOG_DIR = BASE_DIR / "change-logs"
CONFIG_FILE = BASE_DIR / "config.conf"
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.conf"
STORE = MonitorStateStore(BASE_DIR)
SNAPSHOTS = SnapshotManager(BASE_DIR, OPENCLAW_HOME)


def get_change_log_path() -> Path:
    """获取今天的日志文件路径"""
    today = datetime.now().strftime("%Y-%m-%d")
    return CHANGE_LOG_DIR / f"{today}.json"


def record_change(change_type: str, message: str, details: Optional[dict] = None):
    """记录变更"""
    STORE.record_change(change_type, message, details)
    log_file = get_change_log_path()
    CHANGE_LOG_DIR.mkdir(exist_ok=True)
    
    logs = []
    if log_file.exists():
        with open(log_file) as f:
            logs = json.load(f)
    
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": change_type,
        "message": message,
        "details": details or {}
    }
    logs.append(entry)
    
    with open(log_file, "w") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


def get_recent_changes(days: int = 7) -> list:
    """获取最近变更"""
    db_changes = STORE.list_recent_changes(days=days, limit=100)
    if db_changes:
        return list(reversed(db_changes))

    all_changes = []
    today = datetime.now()
    
    for i in range(days):
        date = today.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        log_file = CHANGE_LOG_DIR / f"{date_str}.json"
        
        if log_file.exists():
            with open(log_file) as f:
                logs = json.load(f)
                for entry in logs:
                    entry["date"] = date_str
                    all_changes.append(entry)
    
    return all_changes[-100:]  # 最近100条


def list_snapshots(limit: int = 20) -> list[dict]:
    """列出最近的配置快照。"""
    snapshots = []
    for snapshot_dir in SNAPSHOTS.list_snapshots()[:limit]:
        manifest_file = snapshot_dir / "manifest.json"
        item = {
            "name": snapshot_dir.name,
            "created_at": "",
            "label": "",
            "file_count": 0,
        }
        if manifest_file.exists():
            try:
                with open(manifest_file) as handle:
                    manifest = json.load(handle)
                item["created_at"] = manifest.get("created_at", "")
                item["label"] = manifest.get("label", "")
                item["file_count"] = len(manifest.get("files", []))
            except Exception:
                pass
        snapshots.append(item)
    return snapshots


def stringify_config_value(key: str, value) -> str:
    """Render a config value for history without exposing secrets."""
    if key in SECRET_KEYS:
        return "已配置" if value else "未配置"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value in (None, ""):
        return "(empty)"
    return str(value)


def persist_versions(versions: dict) -> None:
    """Persist version metadata to sqlite and legacy json."""
    STORE.save_versions(versions)
    versions_file = BASE_DIR / "versions.json"
    with open(versions_file, "w") as handle:
        json.dump(versions, handle, indent=2, ensure_ascii=False)


def append_version_history(bucket: str, entry: dict, limit: int = 50) -> None:
    """Append a versioned history entry to the local metadata store."""
    versions = load_versions()
    versions.setdefault("history", [])
    versions.setdefault("config_history", [])
    versions.setdefault("restart_history", [])
    versions.setdefault(bucket, [])
    versions[bucket].append(entry)
    versions[bucket] = versions[bucket][-limit:]
    persist_versions(versions)


def apply_config_change(key: str, value: str) -> tuple[bool, dict]:
    """Save a config change and return a structured history record."""
    before_config = load_config()
    before_value = before_config.get(key)
    snapshot_dir = SNAPSHOTS.create_snapshot("before-config-change")
    if not save_local_config_value(BASE_DIR, key, value):
        return False, {}

    after_config = load_config()
    after_value = after_config.get(key)
    current_version = get_version()
    details = {
        "key": key,
        "before": stringify_config_value(key, before_value),
        "after": stringify_config_value(key, after_value),
        "changed": before_value != after_value,
        "gateway_version": current_version,
        "snapshot": snapshot_dir.name if snapshot_dir else "",
        "restart_required": True,
    }
    append_version_history(
        "config_history",
        {
            "time": datetime.now().isoformat(),
            "key": key,
            "before": details["before"],
            "after": details["after"],
            "gateway_version": current_version,
            "snapshot": details["snapshot"],
        },
    )
    return True, details


def backup_change_logs():
    """备份旧日志"""
    CHANGE_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    
    for f in CHANGE_LOG_DIR.glob("*.json"):
        if f.stem < today:
            backup_dir = CHANGE_LOG_DIR / "backups"
            backup_dir.mkdir(exist_ok=True)
            f.rename(backup_dir / f"{f.stem}.json")
OPENCLAW_CODE = Path.home() / "openclaw-workspace" / "openclaw"
GATEWAY_LOG = OPENCLAW_HOME / "logs" / "gateway.log"
GATEWAY_ERR_LOG = OPENCLAW_HOME / "logs" / "gateway.err.log"

app = Flask(__name__)

# ========== 数据收集函数 ==========

def get_process_info(name: str) -> Optional[dict]:
    """获取进程信息"""
    try:
        result = subprocess.run(
            f'ps aux | grep -i "{name}" | grep -v grep',
            shell=True, capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            parts = result.stdout.strip().split()
            return {
                "pid": int(parts[1]),
                "cpu": float(parts[2]),
                "mem": float(parts[3]),
                "cmd": " ".join(parts[10:])[:100]
            }
    except:
        pass
    return None


def get_listener_pid(port: int = 18789) -> Optional[int]:
    """返回监听指定端口的 PID。"""
    try:
        result = subprocess.run(
            f"lsof -ti tcp:{port} -sTCP:LISTEN",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            return int(result.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


def get_top_processes(limit: int = 15) -> list:
    """获取内存占用最高的进程"""
    processes = []
    try:
        result = subprocess.run(
            "ps aux -m | head -" + str(limit + 1),
            shell=True, capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")[1:]  # 跳过标题行
        for line in lines:
            parts = line.split()
            if len(parts) >= 11:
                try:
                    # RSS 是实际物理内存使用（KB）
                    rss_kb = int(parts[5])
                    rss_mb = rss_kb / 1024
                    processes.append({
                        "pid": int(parts[1]),
                        "user": parts[0],
                        "cpu": float(parts[2]),
                        "mem": float(parts[3]),
                        "mem_mb": rss_mb,
                        "cmd": " ".join(parts[10:])[:80]
                    })
                except:
                    pass
    except:
        pass
    return processes


def check_gateway_health() -> bool:
    """检查 Gateway 健康"""
    try:
        result = subprocess.run(
            "openclaw gateway health",
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = f"{result.stdout}\n{result.stderr}".lower()
        return result.returncode == 0 and "gateway target" not in output
    except Exception:
        return False


def get_system_metrics() -> dict:
    """获取系统指标"""
    cpu = 0.0
    mem_used = 0
    mem_total = 32
    
    try:
        result = subprocess.run("top -l 1 -n 0", shell=True, capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\n"):
            if "CPU usage" in line:
                try:
                    user_match = re.search(r'([\d.]+)%\s*user', line)
                    sys_match = re.search(r'([\d.]+)%\s*sys', line)
                    if user_match:
                        cpu = float(user_match.group(1))
                    if sys_match:
                        cpu += float(sys_match.group(1))
                except:
                    pass
            if "PhysMem" in line:
                try:
                    used_match = re.search(r'(\d+)G\s*used', line)
                    unused_match = re.search(r'(\d+)G\s*unused', line)
                    wired_match = re.search(r'(\d+)G\s*wired', line)
                    
                    if used_match:
                        mem_used = int(used_match.group(1))
                    if wired_match:
                        mem_used = int(wired_match.group(1))  # wired算已用
                    if unused_match:
                        mem_total = mem_used + int(unused_match.group(1))
                    else:
                        mem_total = 32  # 默认32G
                except:
                    pass
    except:
        pass
    
    return {"cpu": round(cpu, 1), "mem_used": mem_used, "mem_total": mem_total}


def analyze_sessions(minutes: int = 5) -> dict:
    """分析会话 - 每5分钟统计"""
    if not GATEWAY_LOG.exists():
        return {"total": 0, "slow": 0, "stuck": 0, "sessions": []}
    
    sessions = []
    dispatch_time = {}
    lines = []
    
    try:
        with open(GATEWAY_LOG) as f:
            lines = f.readlines()[-8000:]
        
        # 先收集所有问题 - 支持多种格式
        questions = {}
        for line in lines:
            try:
                ts = None
                ts_str = ""
                msg = None
                
                # 格式1: "message in group xxx: 问题"
                if "message in" in line.lower() and ": " in line and "did not mention" not in line.lower():
                    idx = line.find("message in")
                    msg_start = line.find(": ", idx)
                    if msg_start > 0:
                        msg = line[msg_start+2:].strip()[:50]
                
                # 格式2: "DM from xxx: 问题"  
                elif "dm from" in line.lower() and ": " in line:
                    idx = line.lower().find("dm from")
                    msg_start = line.find(": ", idx)
                    if msg_start > 0:
                        msg = line[msg_start+2:].strip()[:50]
                
                # 格式3: 飞书发送的消息 "叶子: 内容"
                elif "feishu[default]:" in line.lower() and ": " in line:
                    idx = line.lower().find("feishu[default]:")
                    if idx >= 0:
                        msg_start = line.find(": ", idx + 15)
                        if msg_start > 0:
                            msg = line[msg_start+2:].strip()[:50]
                
                if msg and len(msg) > 2:
                    if "+08:00" in line:
                        ts_str = line[:25]
                        ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                    elif "Z" in line[:25]:
                        ts_str = line[:24]
                        ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                        ts += 8 * 3600
                    if ts:
                        ts_key = int(ts)
                        questions[ts_key] = {"msg": msg[:30], "time": ts_str[11:19] if ts_str else ""}
            except:
                pass
        
        # 分析会话
        for line in lines:
            try:
                ts = None
                ts_str = ""
                if "+08:00" in line:
                    ts_str = line[:25]
                    ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                elif "Z" in line[:25]:
                    ts_str = line[:24]
                    ts = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                    ts += 8 * 3600
                
                if ts is None:
                    continue
                
                if "dispatching to agent" in line.lower():
                    dispatch_time["dispatch"] = ts
                    # 扩大搜索范围到前后10秒
                    ts_key = int(ts)
                    found = False
                    for i in range(-10, 15):
                        if ts_key + i in questions:
                            dispatch_time["question"] = questions[ts_key + i]["msg"]
                            dispatch_time["question_time"] = questions[ts_key + i]["time"]
                            found = True
                            break
                    if not found:
                        dispatch_time["question"] = "未知"
                            
                elif "dispatch complete" in line.lower() and "dispatch" in dispatch_time:
                    duration = ts - dispatch_time["dispatch"]
                    question = dispatch_time.get("question", "无法获取问题内容")
                    question_time = dispatch_time.get("question_time", "")
                    
                    # 提取回复数量
                    replies = 0
                    import re
                    m = re.search(r'replies=(\d+)', line)
                    if m:
                        replies = int(m.group(1))
                    
                    # 分析慢响应原因
                    reason = "正常"
                    if duration > 120:
                        reason = "严重卡顿"
                    elif duration > 30:
                        reason = "响应慢"
                    
                    # 检查是否有错误关键词
                    line_lower = line.lower()
                    if any(k in line_lower for k in ["timeout", "timed out", "error", "fail"]):
                        if "timeout" in line_lower:
                            reason = "LLM超时" if duration > 30 else "正常(有超时)"
                        elif "error" in line_lower or "fail" in line_lower:
                            reason = "处理出错"
                    
                    sessions.append({
                        "time": question_time,
                        "duration": int(duration),
                        "question": question,
                        "replies": replies,
                        "status": "❌" if duration > 120 else ("⚠️" if duration > 30 else "✅"),
                        "reason": reason
                    })
                    dispatch_time = {}
            except:
                pass
    except:
        pass
    
    sessions = sessions[-20:]
    slow = sum(1 for s in sessions if s["duration"] > 30)
    stuck = sum(1 for s in sessions if s["duration"] > 120)
    
    # 分析慢响应原因 - 检查整个日志
    slow_reasons = {}
    for line in lines:
        lower = line.lower()
        if "timeout" in lower or "timed out" in lower:
            slow_reasons["LLM超时"] = slow_reasons.get("LLM超时", 0) + 1
        elif "error" in lower and ("llm" in lower or "model" in lower):
            slow_reasons["模型错误"] = slow_reasons.get("模型错误", 0) + 1
        elif "fail" in lower and ("api" in lower or "key" in lower):
            slow_reasons["API错误"] = slow_reasons.get("API错误", 0) + 1
        elif "400" in line or "401" in line or "403" in line or "500" in line:
            slow_reasons["HTTP错误"] = slow_reasons.get("HTTP错误", 0) + 1
    
    return {"total": len(sessions), "slow": slow, "stuck": stuck, "sessions": sessions, "reasons": slow_reasons}


def get_error_logs(count: int = 20) -> list:
    """获取错误日志"""
    errors = []
    
    for log_file in [GATEWAY_ERR_LOG, GATEWAY_LOG]:
        if not log_file.exists():
            continue
        try:
            with open(log_file) as f:
                lines = f.readlines()[-500:]
            
            for line in lines:
                lower = line.lower()
                if any(kw in lower for kw in ["error", "fail", "exception", "crash"]):
                    try:
                        ts = line[:19]
                        msg = line[20:].strip()[:150]
                        errors.append({"time": ts[11:19], "message": msg})
                    except:
                        pass
            break
        except:
            pass
    
    return errors[:count]


def get_version() -> str:
    """获取版本"""
    try:
        result = subprocess.run(
            f"cd {OPENCLAW_CODE} && git describe --tags --always",
            shell=True, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except:
        pass
    return "unknown"


def get_diagnoses(metrics: dict, sessions: dict, processes: list) -> list:
    """获取诊断建议"""
    diagnoses = []
    
    # 内存
    if metrics["mem_total"] > 0:
        mem_percent = metrics["mem_used"] / metrics["mem_total"] * 100
        if mem_percent > 85:
            diagnoses.append({
                "level": "error",
                "title": "内存使用率过高",
                "message": f"当前 {mem_percent:.0f}%，建议重启 Gateway 释放内存",
                "action": "重启 Gateway"
            })
        elif mem_percent > 70:
            diagnoses.append({
                "level": "warning",
                "title": "内存使用率偏高",
                "message": f"当前 {mem_percent:.0f}%，注意监控",
                "action": None
            })
    
    # CPU
    if metrics["cpu"] > 90:
        diagnoses.append({
            "level": "error",
            "title": "CPU 使用率过高",
            "message": f"当前 {metrics['cpu']}%，检查是否有异常进程",
            "action": None
        })
    
    # 慢会话
    if sessions.get("stuck", 0) > 0:
        diagnoses.append({
            "level": "error",
            "title": f"存在 {sessions['stuck']} 个严重卡顿会话",
            "message": "会话响应超过2分钟，检查 LLM 响应或网络",
            "action": None
        })
    elif sessions.get("slow", 0) > 2:
        diagnoses.append({
            "level": "warning",
            "title": "响应缓慢",
            "message": f"过去30分钟有 {sessions['slow']} 个慢响应会话",
            "action": None
        })
    
    # 进程
    gateway_running = any("gateway" in p.get("cmd", "").lower() for p in processes if p)
    if not gateway_running:
        diagnoses.append({
            "level": "error",
            "title": "Gateway 进程未运行",
            "message": "进程已退出，需要立即处理",
            "action": "启动 Gateway"
        })
    
    # 正常
    if not diagnoses:
        diagnoses.append({
            "level": "success",
            "title": "系统运行正常",
            "message": "所有指标正常",
            "action": None
        })
    
    return diagnoses


def load_config() -> dict:
    """加载配置"""
    return load_shared_config(BASE_DIR)


def save_config(key: str, value: str) -> bool:
    """保存配置"""
    SNAPSHOTS.create_snapshot("before-config-change")
    return save_local_config_value(BASE_DIR, key, value)
    """加载告警历史"""
    alerts_file = BASE_DIR / "alerts.json"
    if alerts_file.exists():
        with open(alerts_file) as f:
            data = json.load(f)
            return [
                {"type": k, "time": datetime.fromtimestamp(v["last_alert"]).strftime("%H:%M:%S"), "count": v.get("count", 1)}
                for k, v in data.items()
            ]
    return []


def load_versions() -> dict:
    """加载版本历史"""
    versions = STORE.load_versions(BASE_DIR / "versions.json")
    versions.setdefault("current", None)
    versions.setdefault("history", [])
    versions.setdefault("config_history", [])
    versions.setdefault("restart_history", [])
    return versions


# ========== API 端点 ==========

@app.route("/")
def index():
    """主页"""
    html = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenClaw 健康监控中心</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #fff;
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        header { 
            display: flex; justify-content: space-between; align-items: center;
            padding: 20px 0; border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        h1 { font-size: 24px; display: flex; align-items: center; gap: 10px; }
        .refresh-info { font-size: 14px; color: #888; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin: 20px 0; }
        .card { 
            background: rgba(255,255,255,0.05); border-radius: 12px; padding: 20px;
            backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1);
        }
        .card h3 { font-size: 14px; color: #888; margin-bottom: 10px; }
        .card .value { font-size: 32px; font-weight: bold; }
        .card .sub { font-size: 12px; color: #666; }
        .progress { height: 6px; background: rgba(255,255,255,0.1); border-radius: 3px; margin-top: 10px; }
        .progress-bar { height: 100%; border-radius: 3px; transition: width 0.3s; }
        .good { background: #4ade80; }
        .warning { background: #fbbf24; }
        .error { background: #f87171; }
        
        .section { margin: 20px 0; }
        .section h2 { font-size: 18px; margin-bottom: 15px; padding-bottom: 10px; border-bottom: 1px solid rgba(255,255,255,0.1); }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.1); }
        th { color: #888; font-weight: normal; }
        
        .diagnose-item { 
            display: flex; align-items: center; gap: 15px; padding: 15px; 
            background: rgba(255,255,255,0.05); border-radius: 8px; margin-bottom: 10px;
        }
        .diagnose-icon { font-size: 24px; }
        .diagnose-content { flex: 1; }
        .diagnose-title { font-weight: bold; margin-bottom: 5px; }
        .diagnose-msg { font-size: 13px; color: #aaa; }
        .diagnose-action { 
            padding: 6px 12px; background: #3b82f6; border-radius: 6px; 
            font-size: 12px; cursor: pointer; border: none; color: #fff;
        }
        
        .status-ok { color: #4ade80; }
        .status-error { color: #f87171; }
        .status-warning { color: #fbbf24; }
        
        .row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .actions { display: flex; gap: 10px; }
        .btn {
            padding: 8px 16px; background: rgba(255,255,255,0.1); border: none;
            border-radius: 6px; color: #fff; cursor: pointer; font-size: 13px;
        }
        .btn:hover { background: rgba(255,255,255,0.2); }
        .btn-primary { background: #3b82f6; }
        
        /* 开关样式 */
        .switch {
            position: relative; display: inline-block; width: 44px; height: 24px;
        }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
            position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background-color: #444; transition: .4s; border-radius: 24px;
        }
        .slider:before {
            position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px;
            background-color: white; transition: .4s; border-radius: 50%;
        }
        input:checked + .slider { background-color: #3b82f6; }
        input:checked + .slider:before { transform: translateX(20px); }
        
        /* 配置按钮 */
        .config-btn {
            padding: 6px 12px; background: #3b82f6; border: none;
            border-radius: 6px; color: #fff; cursor: pointer; font-size: 12px;
        }
        .config-btn:hover { background: #2563eb; }
        .config-btn.configured { background: #10b981; }
        .config-value { font-size: 12px; color: #888; margin-left: 8px; }
        
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .live { animation: pulse 2s infinite; }
        
        .tabs { display: flex; gap: 5px; margin: 20px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .tab { padding: 10px 20px; background: transparent; border: none; color: #888; cursor: pointer; font-size: 14px; }
        .tab.active { color: #fff; border-bottom: 2px solid #3b82f6; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .toast-container {
            position: fixed; top: 20px; right: 20px; z-index: 9999;
        }
        .toast {
            padding: 15px 20px; margin-bottom: 10px; border-radius: 8px;
            color: #fff; font-size: 14px; animation: slideIn 0.3s;
            max-width: 400px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        .toast.error { background: #ef4444; }
        .toast.warning { background: #f59e0b; }
        .toast.success { background: #10b981; }
        .toast.info { background: #3b82f6; }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🛡️ OpenClaw 健康监控中心</h1>
            <div class="actions">
                <button class="btn" onclick="location.reload()">🔄 刷新</button>
                <button class="btn btn-primary" onclick="restartGateway('manual_restart', 'dashboard_button', event)">🔁 重启 Gateway</button>
                <button class="btn" style="background:#dc2626" onclick="emergencyRecover()">🚨 急救</button>
            </div>
        </header>
        
        <div class="tabs">
            <button class="tab active" onclick="switchTab('dashboard', event)">📊 监控</button>
            <button class="tab" onclick="switchTab('changes', event)">📝 变更日志</button>
            <button class="tab" onclick="switchTab('snapshots', event)">📦 配置快照</button>
        </div>
        
        <div id="tab-dashboard" class="tab-content active">
        <div class="grid">
            <div class="card">
                <h3>CPU 使用率</h3>
                <div class="value" id="cpu">--</div>
                <div class="sub" id="cpu-sub">--</div>
                <div class="progress"><div class="progress-bar" id="cpu-bar"></div></div>
            </div>
            <div class="card">
                <h3>内存使用</h3>
                <div class="value" id="mem">--</div>
                <div class="sub" id="mem-sub">--</div>
                <div class="progress"><div class="progress-bar" id="mem-bar"></div></div>
            </div>
            <div class="card">
                <h3>会话统计 (5分钟)</h3>
                <div class="value" id="sessions">--</div>
                <div class="sub" id="sessions-sub">--</div>
                <div id="slow-reasons" style="font-size:11px;color:#888;margin-top:5px"></div>
            </div>
            <div class="card">
                <h3>服务状态</h3>
                <div class="value" id="gateway-status">--</div>
                <div class="sub" id="process-pid">--</div>
            </div>
        </div>
        
        <div class="row">
            <div class="section">
                <h2>🔍 会话分析 (最近5分钟)</h2>
                <table>
                    <thead><tr><th>时间</th><th>问题</th><th>回复</th><th>耗时</th><th>状态</th></tr></thead>
                    <tbody id="slow-sessions"></tbody>
                </table>
            </div>
            <div class="section">
                <h2>🛠️ 诊断 & 建议</h2>
                <div id="diagnoses"></div>
            </div>
        </div>
        
        <div class="section">
            <h2>💻 内存占用排行 (Top 15)</h2>
            <table>
                <thead><tr><th>PID</th><th>用户</th><th>CPU %</th><th>内存</th><th>进程</th></tr></thead>
                <tbody id="top-processes"></tbody>
            </table>
        </div>
        
        <div class="row">
            <div class="section">
                <h2>📋 错误日志</h2>
                <table>
                    <thead><tr><th>时间</th><th>错误信息</th></tr></thead>
                    <tbody id="error-logs"></tbody>
                </table>
            </div>
            <div class="section">
                <h2>📊 进程监控</h2>
                <table>
                    <thead><tr><th>进程</th><th>PID</th><th>CPU %</th><th>内存 %</th></tr></thead>
                    <tbody id="processes"></tbody>
                </table>
            </div>
        </div>
        
        <div class="section">
            <h2>⚙️ 配置管理</h2>
            <table>
                <tr>
                    <td width="200">自动更新</td>
                    <td>
                        <label class="switch">
                            <input type="checkbox" id="auto-update-toggle" onchange="toggleAutoUpdate(this)">
                            <span class="slider"></span>
                        </label>
                        <span id="auto-update-status" class="config-value"></span>
                    </td>
                </tr>
                <tr>
                    <td>当前版本</td>
                    <td id="current-version">--</td>
                </tr>
                <tr>
                    <td>版本历史</td>
                    <td id="version-history">--</td>
                </tr>
                <tr>
                    <td>钉钉通知</td>
                    <td>
                        <button class="config-btn" id="dingtalk-btn" onclick="configureWebhook('DINGTALK')">配置</button>
                        <span id="dingtalk-status" class="config-value"></span>
                    </td>
                </tr>
                <tr>
                    <td>飞书通知</td>
                    <td>
                        <button class="config-btn" id="feishu-btn" onclick="configureWebhook('FEISHU')">配置</button>
                        <span id="feishu-status" class="config-value"></span>
                    </td>
                </tr>
            </table>
        </div>
        
        <footer style="text-align: center; padding: 20px; color: #666; font-size: 12px;">
            <span class="live">●</span> 自动刷新中 | <span id="last-update">--</span>
        </footer>
    </div>
    
    <div id="tab-changes" class="tab-content">
        <div class="section">
            <h2>📝 变更日志</h2>
            <div style="margin-bottom: 15px;">
                <button class="btn" onclick="loadChanges()">🔄 刷新</button>
            </div>
            <table>
                    <thead><tr><th>日期</th><th>时间</th><th>类型</th><th>摘要</th><th>详情</th></tr></thead>
                    <tbody id="change-logs"></tbody>
                </table>
            </div>
    </div>

    <div id="tab-snapshots" class="tab-content">
        <div class="section">
            <h2>📦 配置快照</h2>
            <div style="margin-bottom: 15px;">
                <button class="btn" onclick="loadSnapshots()">🔄 刷新</button>
                <button class="btn btn-primary" onclick="captureSnapshot()">➕ 创建快照</button>
            </div>
            <table>
                <thead><tr><th>名称</th><th>标签</th><th>创建时间</th><th>文件数</th><th>操作</th></tr></thead>
                <tbody id="snapshot-logs"></tbody>
            </table>
        </div>
    </div>
    
    <script>
        let currentData = null;
        
        async function loadData() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    document.getElementById('cpu').textContent = 'Error: ' + res.status;
                    return;
                }
                const data = await res.json();
                currentData = data;
                
                // CPU
                document.getElementById('cpu').textContent = data.metrics.cpu + '%';
                document.getElementById('cpu-sub').textContent = data.metrics.cpu > 90 ? '过高' : '正常';
                const cpuBar = document.getElementById('cpu-bar');
                cpuBar.style.width = Math.min(data.metrics.cpu, 100) + '%';
                cpuBar.className = 'progress-bar ' + (data.metrics.cpu > 90 ? 'error' : data.metrics.cpu > 70 ? 'warning' : 'good');
                
                // 内存
                document.getElementById('mem').textContent = data.metrics.mem_used + 'G';
                const memPercent = (data.metrics.mem_used / data.metrics.mem_total * 100).toFixed(0);
                document.getElementById('mem-sub').textContent = memPercent + '%';
                const memBar = document.getElementById('mem-bar');
                memBar.style.width = memPercent + '%';
                memBar.className = 'progress-bar ' + (memPercent > 85 ? 'error' : memPercent > 70 ? 'warning' : 'good');
                
                // 会话
                document.getElementById('sessions').textContent = data.sessions.total;
                document.getElementById('sessions-sub').textContent = `慢: ${data.sessions.slow} | 卡: ${data.sessions.stuck}`;
                
                // 慢响应原因统计
                const reasonsEl = document.getElementById('slow-reasons');
                const reasons = data.sessions.reasons || {};
                if (Object.keys(reasons).length > 0) {
                    const reasonText = Object.entries(reasons).map(([k,v]) => `${k}:${v}`).join(' | ');
                    reasonsEl.textContent = reasonText;
                } else {
                    reasonsEl.textContent = '';
                }
                
                // Gateway 状态
                const statusEl = document.getElementById('gateway-status');
                if (data.gateway_healthy) {
                    statusEl.innerHTML = '<span class="status-ok">● 运行中</span>';
                } else {
                    statusEl.innerHTML = '<span class="status-error">● 异常</span>';
                }
                document.getElementById('process-pid').textContent = data.gateway_process ? 'PID: ' + data.gateway_process.pid : '未运行';
                
                // 慢会话
                const tbody = document.getElementById('slow-sessions');
                tbody.innerHTML = data.sessions.sessions.length ? '' : '<tr><td colspan="5" style="text-align:center;color:#666">暂无会话</td></tr>';
                data.sessions.sessions.forEach(s => {
                    const row = document.createElement('tr');
                    row.innerHTML = `<td>${s.time}</td><td title="${s.question}">${s.question || '-'}</td><td>${s.replies || 0}条</td><td>${s.duration}s</td><td>${s.status}</td>`;
                    tbody.appendChild(row);
                });
                
                // 内存占用排行
                const procEl = document.getElementById('top-processes');
                procEl.innerHTML = data.top_processes && data.top_processes.length ? '' : '<tr><td colspan="5" style="text-align:center;color:#666">暂无数据</td></tr>';
                if (data.top_processes) {
                    data.top_processes.slice(0, 15).forEach(p => {
                        const row = document.createElement('tr');
                        row.innerHTML = `<td>${p.pid}</td><td>${p.user}</td><td>${p.cpu.toFixed(1)}%</td><td>${(p.mem_mb/1024).toFixed(1)} GB</td><td title="${p.cmd}">${p.cmd}</td>`;
                        procEl.appendChild(row);
                    });
                }
                
                // 诊断
                const diagEl = document.getElementById('diagnoses');
                diagEl.innerHTML = data.diagnoses.map(d => `
                    <div class="diagnose-item">
                        <span class="diagnose-icon">${d.level === 'error' ? '🔴' : d.level === 'warning' ? '🟡' : '🟢'}</span>
                        <div class="diagnose-content">
                            <div class="diagnose-title">${d.title}</div>
                            <div class="diagnose-msg">${d.message}</div>
                        </div>
                        ${d.action ? `<button class="diagnose-action" onclick="restartGateway('diagnostic_action', 'dashboard_diagnosis', event)">${d.action}</button>` : ''}
                    </div>
                `).join('');
                
                // 错误日志
                const errEl = document.getElementById('error-logs');
                errEl.innerHTML = data.errors.length ? '' : '<tr><td colspan="2" style="text-align:center;color:#666">暂无错误</td></tr>';
                data.errors.slice(0, 10).forEach(e => {
                    const row = document.createElement('tr');
                    row.innerHTML = `<td>${e.time}</td><td>${e.message}</td>`;
                    errEl.appendChild(row);
                });
                
                // 进程
                const procListEl = document.getElementById('processes');
                const procs = [
                    {name: 'Gateway', info: data.gateway_process},
                    {name: 'Guardian', info: data.guardian_process},
                ];
                procListEl.innerHTML = procs.map(p => p.info ? 
                    `<tr><td>${p.name}</td><td>${p.info.pid}</td><td>${p.info.cpu}%</td><td>${p.info.mem.toFixed(1)}%</td></tr>` :
                    `<tr><td>${p.name}</td><td colspan="3" class="status-error">未运行</td></tr>`
                ).join('');
                
                // 配置
                const autoUpdate = data.config.AUTO_UPDATE;
                document.getElementById('auto-update-toggle').checked = autoUpdate;
                document.getElementById('auto-update-status').textContent = autoUpdate ? '已开启' : '已关闭';
                document.getElementById('current-version').textContent = data.version.current || 'unknown';
                const versionCount = data.version.history ? data.version.history.length : 0;
                const configChangeCount = data.version.config_history ? data.version.config_history.length : 0;
                document.getElementById('version-history').textContent = `代码版本: ${versionCount} | 配置变更: ${configChangeCount}`;
                
                const dingtalkWebhook = data.config.DINGTALK_WEBHOOK;
                const feishuWebhook = data.config.FEISHU_WEBHOOK;
                
                const dingtalkBtn = document.getElementById('dingtalk-btn');
                const feishuBtn = document.getElementById('feishu-btn');
                
                if (dingtalkWebhook) {
                    dingtalkBtn.textContent = '已配置';
                    dingtalkBtn.classList.add('configured');
                    document.getElementById('dingtalk-status').textContent = '已配置';
                } else {
                    dingtalkBtn.textContent = '配置';
                    dingtalkBtn.classList.remove('configured');
                    document.getElementById('dingtalk-status').textContent = '未配置';
                }
                
                if (feishuWebhook) {
                    feishuBtn.textContent = '已配置';
                    feishuBtn.classList.add('configured');
                    document.getElementById('feishu-status').textContent = '已配置';
                } else {
                    feishuBtn.textContent = '配置';
                    feishuBtn.classList.remove('configured');
                    document.getElementById('feishu-status').textContent = '未配置';
                }
                
                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            } catch(e) {
                console.error(e);
            }
        }
        
        async function restartGateway(reason = 'manual_restart', requestedBy = 'dashboard', evt = null) {
            const btn = evt ? evt.target : null;
            if (btn) {
                btn.disabled = true;
                btn.textContent = '重启中...';
            }
            try {
                const res = await fetch('/api/restart', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({reason, requested_by: requestedBy})
                });
                const data = await res.json();
                setTimeout(() => {
                    loadData();
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = '🔁 重启 Gateway';
                    }
                }, 3000);
            } catch(e) {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '🔁 重启 Gateway';
                }
                alert('重启请求失败');
            }
        }
        
        async function emergencyRecover() {
            if (!confirm('🚨 急救模式将：\\n1. 恢复最近一次配置快照\\n2. 重新启动 Gateway\\n\\n确定要执行吗？')) return;
            try {
                const res = await fetch('/api/emergency-recover', {method: 'POST'});
                const data = await res.json();
                alert(data.message);
            } catch(e) {
                alert('急救请求失败');
            }
        }
        
        async function toggleAutoUpdate(checkbox) {
            const value = checkbox.checked;
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: 'AUTO_UPDATE', value: value})
                });
                const data = await res.json();
                if (!data.success) {
                    alert(data.message);
                    checkbox.checked = !value;
                } else if (confirm('配置已更新。是否现在重启 Gateway 使变更生效？')) {
                    restartGateway('config_change:AUTO_UPDATE', 'config_toggle');
                }
            } catch(e) {
                alert('配置保存失败');
                checkbox.checked = !value;
            }
        }
        
        async function configureWebhook(type) {
            const key = type === 'DINGTALK' ? 'DINGTALK_WEBHOOK' : 'FEISHU_WEBHOOK';
            const value = prompt('请输入 ' + (type === 'DINGTALK' ? '钉钉' : '飞书') + ' Webhook URL:');
            if (value === null) return;
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: key, value: '"' + value + '"'})
                });
                const data = await res.json();
                alert(data.message);
                if (data.success) {
                    loadData();
                    if (confirm('配置已更新。是否现在重启 Gateway 使变更生效？')) {
                        restartGateway('config_change:' + key, 'config_prompt');
                    }
                }
            } catch(e) {
                alert('配置保存失败');
            }
        }
        
        loadData();
        setInterval(loadData, 5000);
        
        function switchTab(tabName, evt) {
            console.log('Switching to:', tabName);
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            evt.target.classList.add('active');
            document.getElementById('tab-' + tabName).classList.add('active');
            console.log('Tab content element:', document.getElementById('tab-' + tabName));
            if (tabName === 'changes') {
                console.log('Calling loadChanges...');
                loadChanges();
            } else if (tabName === 'snapshots') {
                loadSnapshots();
            }
        }
        
        async function loadChanges() {
            console.log('Loading changes...');
            try {
                const res = await fetch('/api/changes?days=30');
                console.log('Response:', res.status);
                const data = await res.json();
                console.log('Data:', data);
                const tbody = document.getElementById('change-logs');
                if (!data.changes || data.changes.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#666">暂无日志</td></tr>';
                    return;
                }
                tbody.innerHTML = data.changes.reverse().map(c => {
                    const typeIcon = {'restart': '🔁', 'config': '⚙️', 'recover': '🚨', 'update': '🔄', 'version': '📋', 'snapshot': '📦'}[c.type] || '📝';
                    return `<tr><td>${c.date || ''}</td><td>${c.time}</td><td>${typeIcon} ${c.type}</td><td>${c.message}</td><td>${formatChangeDetails(c)}</td></tr>`;
                }).join('');
            } catch(e) {
                console.error(e);
            }
        }

        function formatChangeDetails(change) {
            const details = change.details || {};
            if (change.type === 'config') {
                return `配置项: ${details.key || '-'} | 旧值: ${details.before || '-'} | 新值: ${details.after || '-'} | 版本: ${details.gateway_version || '-'} | 快照: ${details.snapshot || '-'}`;
            }
            if (change.type === 'restart') {
                return `原因: ${details.reason || '未记录'} | 来源: ${details.requested_by || '-'} | 版本: ${details.gateway_version || '-'} | PID: ${details.old_pid || '-'} -> ${details.new_pid || '-'}`;
            }
            if (change.type === 'recover') {
                return `快照: ${details.snapshot || '-'} | 原因: ${details.reason || '恢复操作'} | 版本: ${details.gateway_version || '-'}`;
            }
            if (change.type === 'version') {
                return `提交: ${details.commit || details.to || '-'} | 来源: ${details.from || '-'}`;
            }
            if (change.type === 'snapshot') {
                return `快照: ${details.snapshot || '-'} | 标签: ${details.label || '-'}`;
            }
            return JSON.stringify(details);
        }

        async function loadSnapshots() {
            try {
                const res = await fetch('/api/snapshots');
                const data = await res.json();
                const tbody = document.getElementById('snapshot-logs');
                if (!data.snapshots || data.snapshots.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666">暂无快照</td></tr>';
                    return;
                }
                tbody.innerHTML = data.snapshots.map(s => {
                    const created = s.created_at ? new Date(s.created_at).toLocaleString() : '-';
                    return `<tr><td>${s.name}</td><td>${s.label || '-'}</td><td>${created}</td><td>${s.file_count}</td><td><button class="btn" onclick="restoreSnapshot('${s.name}')">恢复</button></td></tr>`;
                }).join('');
            } catch (e) {
                console.error(e);
            }
        }

        async function captureSnapshot() {
            const label = prompt('请输入快照标签:', 'manual');
            if (label === null) return;
            try {
                const res = await fetch('/api/snapshots', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({label})
                });
                const data = await res.json();
                alert(data.message);
                if (data.success) loadSnapshots();
            } catch (e) {
                alert('创建快照失败');
            }
        }

        async function restoreSnapshot(name) {
            if (!confirm('将恢复选中的配置快照，并发起 Gateway 重启。确定继续吗？')) return;
            try {
                const res = await fetch('/api/snapshots/restore', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name})
                });
                const data = await res.json();
                alert(data.message);
                if (data.success) loadSnapshots();
            } catch (e) {
                alert('恢复快照失败');
            }
        }
    </script>
    <script>
        window.onerror = function(msg, url, line) {
            document.body.innerHTML += '<div style="background:red;padding:10px;color:white">JS Error: ' + msg + ' line:' + line + '</div>';
        };
    </script>
</body>
</html>
'''
    return render_template_string(html)


@app.route("/api/status")
def api_status():
    """获取状态 API"""
    metrics = get_system_metrics()
    gateway_process = get_process_info("openclaw.*gateway")
    guardian_process = get_process_info("guardian")
    gateway_healthy = check_gateway_health()
    sessions = analyze_sessions(5)
    errors = get_error_logs(20)
    version = get_version()
    config = load_config()
    safe_config = sanitize_config_for_ui(config)
    version_history = load_versions()
    diagnoses = get_diagnoses(metrics, sessions, [gateway_process, guardian_process])
    top_processes = get_top_processes(15)
    
    data = {
        "metrics": metrics,
        "gateway_process": gateway_process,
        "guardian_process": guardian_process,
        "gateway_healthy": gateway_healthy,
        "sessions": sessions,
        "errors": errors,
        "version": {
            "current": version,
            "history": version_history.get("history", []),
            "config_history": version_history.get("config_history", []),
            "restart_history": version_history.get("restart_history", []),
        },
        "config": safe_config,
        "diagnoses": diagnoses,
        "top_processes": top_processes
    }
    return app.response_class(
        response=json.dumps(data, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """重启 Gateway"""
    try:
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason", "manual_restart")).strip() or "manual_restart"
        requested_by = str(payload.get("requested_by", "dashboard")).strip() or "dashboard"
        gateway_version = get_version()
        old_pid = get_listener_pid()
        if old_pid is not None:
            subprocess.run(f"kill {old_pid}", shell=True, check=False)
            time.sleep(2)

        with open(BASE_DIR / "logs" / "guardian.log", "a") as log_handle:
            subprocess.Popen(
                ["openclaw", "gateway", "run"],
                cwd=str(OPENCLAW_CODE),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

        time.sleep(5)
        listener_pid = get_listener_pid()
        new_pid = str(listener_pid) if listener_pid is not None else None
        old_pid_str = str(old_pid) if old_pid is not None else None
        
        if new_pid and new_pid != old_pid_str:
            details = {
                "old_pid": old_pid_str,
                "new_pid": new_pid,
                "reason": reason,
                "requested_by": requested_by,
                "gateway_version": gateway_version,
            }
            record_change("restart", f"Gateway 重启成功 (PID: {old_pid_str} → {new_pid})", details)
            append_version_history(
                "restart_history",
                {
                    "time": datetime.now().isoformat(),
                    "reason": reason,
                    "requested_by": requested_by,
                    "gateway_version": gateway_version,
                    "old_pid": old_pid_str,
                    "new_pid": new_pid,
                },
            )
            return jsonify({
                "success": True, 
                "message": f"Gateway 已重启\n旧PID: {old_pid_str or '无'}\n新PID: {new_pid}",
                "old_pid": old_pid_str,
                "new_pid": new_pid
            })
        elif new_pid:
            return jsonify({
                "success": True, 
                "message": f"Gateway 正在运行 (PID: {new_pid})",
                "new_pid": new_pid
            })
        else:
            return jsonify({"success": False, "message": "Gateway 启动失败"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/emergency-recover", methods=["POST"])
def api_emergency_recover():
    """急救：恢复最近一次配置快照并重启。"""
    try:
        config = load_config()
        if not config.get("ENABLE_SNAPSHOT_RECOVERY", True):
            return jsonify({
                "success": False,
                "message": "当前已禁用 snapshot recovery。请先在本地配置中显式开启 ENABLE_SNAPSHOT_RECOVERY=true。"
            })

        snapshot_dir = SNAPSHOTS.restore_latest_snapshot()
        if snapshot_dir is None:
            return jsonify({"success": False, "message": "没有可恢复的配置快照"})

        old_pid = get_listener_pid()
        if old_pid is not None:
            subprocess.run(f"kill {old_pid}", shell=True, check=False)
            time.sleep(2)

        with open(BASE_DIR / "logs" / "guardian.log", "a") as log_handle:
            subprocess.Popen(
                ["openclaw", "gateway", "run"],
                cwd=str(OPENCLAW_CODE),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

        details = {
            "snapshot": snapshot_dir.name,
            "reason": "emergency_recover",
            "gateway_version": get_version(),
        }
        record_change("recover", f"恢复配置快照并重启: {snapshot_dir.name}", details)
        return jsonify({"success": True, "message": f"已恢复配置快照并发起重启: {snapshot_dir.name}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/changes")
def api_changes():
    """获取变更日志"""
    days = request.args.get("days", 7, type=int)
    changes = get_recent_changes(days)
    return jsonify({"changes": changes})


@app.route("/api/snapshots")
def api_snapshots():
    """获取配置快照列表。"""
    return jsonify({"snapshots": list_snapshots()})


@app.route("/api/snapshots", methods=["POST"])
def api_snapshot_create():
    """手动创建配置快照。"""
    try:
        data = request.get_json(silent=True) or {}
        label = str(data.get("label", "manual")).strip() or "manual"
        snapshot_dir = SNAPSHOTS.create_snapshot(label)
        if snapshot_dir is None:
            return jsonify({"success": False, "message": "没有可快照的配置文件"})
        record_change("snapshot", f"手动创建配置快照: {snapshot_dir.name}", {"snapshot": snapshot_dir.name, "label": label})
        return jsonify({"success": True, "message": f"已创建配置快照: {snapshot_dir.name}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/snapshots/restore", methods=["POST"])
def api_snapshot_restore():
    """恢复指定快照并发起重启。"""
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"success": False, "message": "缺少快照名称"})

        snapshot_dir = SNAPSHOTS.snapshot_root / name
        if not snapshot_dir.exists() or not snapshot_dir.is_dir():
            return jsonify({"success": False, "message": "快照不存在"})

        SNAPSHOTS.restore_snapshot(snapshot_dir)

        old_pid = get_listener_pid()
        if old_pid is not None:
            subprocess.run(f"kill {old_pid}", shell=True, check=False)
            time.sleep(2)

        with open(BASE_DIR / "logs" / "guardian.log", "a") as log_handle:
            subprocess.Popen(
                ["openclaw", "gateway", "run"],
                cwd=str(OPENCLAW_CODE),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

        details = {
            "snapshot": snapshot_dir.name,
            "reason": "manual_snapshot_restore",
            "gateway_version": get_version(),
        }
        record_change("recover", f"恢复指定配置快照并重启: {snapshot_dir.name}", details)
        return jsonify({"success": True, "message": f"已恢复配置快照并发起重启: {snapshot_dir.name}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/config", methods=["POST"])
def api_config():
    """更新配置"""
    try:
        data = request.get_json()
        key = data.get("key")
        value = data.get("value")
        
        if not key:
            return jsonify({"success": False, "message": "缺少配置键"})
        
        ok, details = apply_config_change(key, str(value))
        if ok:
            record_change(
                "config",
                f"配置已更新: {key}",
                details,
            )
            return jsonify({"success": True, "message": "配置已更新，重启后生效", "details": details})
        else:
            return jsonify({"success": False, "message": "保存配置失败"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


if __name__ == "__main__":
    import socket
    
    def find_free_port(start=8080):
        for port in range(start, start + 10):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("", port))
                sock.close()
                return port
            except:
                continue
        return 8080
    
    port = find_free_port()
    print("=" * 50)
    print("OpenClaw 健康监控中心")
    print(f"访问: http://localhost:{port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
