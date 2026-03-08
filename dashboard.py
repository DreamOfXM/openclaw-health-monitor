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
import resource
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request

from typing import Optional

from monitor_config import load_config as load_shared_config, save_local_config_value, sanitize_config_for_ui
from snapshot_manager import SnapshotManager
from state_store import MonitorStateStore

BASE_DIR = Path(__file__).parent
OPENCLAW_HOME = Path.home() / ".openclaw"
CHANGE_LOG_DIR = BASE_DIR / "change-logs"
CONFIG_FILE = BASE_DIR / "config.conf"
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.conf"
STORE = MonitorStateStore(BASE_DIR)
SNAPSHOTS = SnapshotManager(BASE_DIR, OPENCLAW_HOME)
GUARDIAN_PID_FILE = BASE_DIR / "logs" / "guardian.pid"
DESKTOP_RUNTIME = BASE_DIR / "desktop_runtime.sh"
OFFICIAL_MANAGER = BASE_DIR / "manage_official_openclaw.sh"


def raise_nofile_limit(target: int = 65536) -> None:
    """Best-effort bump of RLIMIT_NOFILE for the dashboard server."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = min(max(soft, target), hard)
        if desired > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except Exception:
        pass


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


def get_recent_anomalies(limit: int = 8, days: int = 7) -> list:
    """Return recent anomaly and pipeline-related events for quick triage."""
    changes = get_recent_changes(days)
    anomalies = [item for item in reversed(changes) if item.get("type") in {"anomaly", "pipeline"}]
    return anomalies[:limit]


def build_incident_summary(events: list[dict]) -> dict:
    """Build an operator-friendly summary from recent events."""
    summary = {
        "headline": "最近未发现明显异常",
        "status": "ok",
        "focus": "继续观察即可",
        "action": "暂无需要立即处理的动作",
        "last_stage": "-",
        "last_question": "-",
    }
    if not events:
        return summary

    latest = events[0]
    latest_details = latest.get("details", {}) or {}
    summary["last_stage"] = latest_details.get("marker", "-")
    summary["last_question"] = latest_details.get("question", "-")

    anomaly = next((item for item in events if item.get("type") == "anomaly"), None)
    pipeline = next((item for item in events if item.get("type") == "pipeline"), None)
    if pipeline:
        summary["last_stage"] = (pipeline.get("details", {}) or {}).get("marker", summary["last_stage"])

    if not anomaly:
        summary["headline"] = "最近有阶段进度，未见异常告警"
        summary["status"] = "watch"
        summary["focus"] = f"最后进度阶段: {summary['last_stage']}"
        summary["action"] = "如果长时间停留在同一阶段，再检查 Gateway 日志"
        return summary

    details = anomaly.get("details", {}) or {}
    question = details.get("question") or "未知问题"
    duration = details.get("duration")
    marker = details.get("marker")
    message = anomaly.get("message", "检测到任务异常")
    summary["last_question"] = question

    if "没有可见回复" in message:
        summary["headline"] = "检测到任务完成但用户没有收到回复"
        summary["status"] = "error"
        summary["focus"] = f"问题: {question}"
        summary["action"] = "优先检查网关回包链路和 replies/queuedFinal 日志"
    elif "阶段长时间无进展" in message:
        summary["headline"] = "检测到任务卡在某个阶段"
        summary["status"] = "error"
        summary["focus"] = f"阶段: {marker or '-'} | 问题: {question}"
        summary["action"] = "优先检查该阶段前后的 PIPELINE_PROGRESS 和子代理执行日志"
    elif "长时间无最终结果" in message:
        summary["headline"] = "检测到任务长时间没有最终结果"
        summary["status"] = "error"
        summary["focus"] = f"问题: {question}"
        summary["action"] = "优先检查 dispatching/dispatch complete 是否成对出现"
    elif "WebSocket 异常关闭" in message:
        summary["headline"] = "检测到 Gateway WebSocket 异常关闭"
        summary["status"] = "error"
        summary["focus"] = "网关链路异常中断"
        summary["action"] = "优先检查 Gateway 进程状态和最近错误日志"
    else:
        summary["headline"] = message
        summary["status"] = "watch"
        summary["focus"] = f"问题: {question}"
        summary["action"] = "建议先查看最近异常详情和运行日志"

    if duration is not None:
        summary["focus"] += f" | 耗时: {duration}秒"
    if marker and summary["last_stage"] == "-":
        summary["last_stage"] = marker
    return summary


def format_change_details(change: dict) -> str:
    """Render compact change details for the dashboard."""
    details = change.get("details", {}) or {}
    change_type = change.get("type")
    if change_type == "pipeline":
        return f"阶段: {details.get('marker', '-')} | 时间: {details.get('timestamp', '-')}"
    if change_type == "anomaly":
        question = details.get("question", "-")
        duration = details.get("duration", "-")
        marker = details.get("marker")
        marker_text = f" | 阶段: {marker}" if marker else ""
        return f"问题: {question} | 耗时: {duration}秒{marker_text} | 时间: {details.get('timestamp', '-')}"
    if change_type == "restart":
        return f"PID: {details.get('old_pid', '-')} -> {details.get('new_pid', '-')}"
    if change_type == "recover":
        return f"快照: {details.get('snapshot', '-')}"
    if change_type == "snapshot":
        return f"快照: {details.get('snapshot', '-')}"
    if change_type == "version":
        if details.get("from") and details.get("to"):
            return f"{details.get('from')} -> {details.get('to')}"
        return details.get("commit", "-")
    return json.dumps(details, ensure_ascii=False) if details else "-"


def parse_mem_value_to_gb(raw: str) -> float:
    """Convert macOS memory strings like 2735M/32G/512K to GB."""
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    match = re.match(r"([\d.]+)\s*([KMGT])", raw, re.IGNORECASE)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2).upper()
    factors = {
        "K": 1 / (1024 * 1024),
        "M": 1 / 1024,
        "G": 1,
        "T": 1024,
    }
    return round(value * factors.get(unit, 0), 2)


def summarize_memory_usage(metrics: dict, top_processes: list[dict]) -> dict:
    """Build a memory attribution summary for operators."""
    mem_used = float(metrics.get("mem_used", 0) or 0)
    mem_total = float(metrics.get("mem_total", 0) or 0)
    wired = float(metrics.get("mem_wired", 0) or 0)
    compressed = float(metrics.get("mem_compressed", 0) or 0)
    top_process_sum = round(sum(float(p.get("mem_mb", 0) or 0) for p in top_processes) / 1024, 2)
    unattributed = round(max(mem_used - top_process_sum, 0), 2)
    system_other = round(max(unattributed - wired - compressed, 0), 2)
    process_coverage = round((top_process_sum / mem_used * 100), 1) if mem_used > 0 else 0.0

    items = [
        {"name": "Top 15 进程", "value_gb": top_process_sum, "kind": "process", "note": f"覆盖已用内存 {process_coverage}%"},
    ]
    if wired > 0:
        items.append({"name": "Kernel / Wired", "value_gb": wired, "kind": "system", "note": "内核与驱动占用"})
    if compressed > 0:
        items.append({"name": "Compressed", "value_gb": compressed, "kind": "system", "note": "压缩内存"})
    if system_other > 0:
        items.append({"name": "Other System", "value_gb": system_other, "kind": "system", "note": "缓存、共享内存等未归属项"})

    return {
        "top15_gb": top_process_sum,
        "unattributed_gb": unattributed,
        "process_coverage_percent": process_coverage,
        "items": items,
        "summary": f"{top_process_sum:.1f}G 进程 + {unattributed:.1f}G 系统/缓存 = {mem_used:.1f}G",
        "note": "系统项包含内核、压缩内存和无法直接归属到单进程的缓存/共享内存。",
        "total_gb": mem_total,
    }


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


def load_config() -> dict:
    """加载配置"""
    return load_shared_config(BASE_DIR)


def active_env_id(config: Optional[dict] = None) -> str:
    cfg = config or load_config()
    selected = str(cfg.get("ACTIVE_OPENCLAW_ENV", "primary")).strip() or "primary"
    return selected if selected in {"primary", "official"} else "primary"


def get_env_specs(config: Optional[dict] = None) -> dict[str, dict]:
    cfg = config or load_config()
    primary_port = int(cfg.get("GATEWAY_PORT", 18789))
    official_port = int(cfg.get("OPENCLAW_OFFICIAL_PORT", 19001))
    primary_home = Path(str(cfg.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))))
    primary_code = Path(str(cfg.get("OPENCLAW_CODE", str(Path.home() / "openclaw-workspace" / "openclaw"))))
    official_state = Path(str(cfg.get("OPENCLAW_OFFICIAL_STATE", str(Path.home() / ".openclaw-official"))))
    official_code = Path(str(cfg.get("OPENCLAW_OFFICIAL_CODE", str(Path.home() / "openclaw-workspace" / "openclaw-official"))))
    return {
        "primary": {
            "id": "primary",
            "name": "当前主用版",
            "description": "当前日常使用的 OpenClaw 环境",
            "home": primary_home,
            "code": primary_code,
            "port": primary_port,
            "kind": "primary",
        },
        "official": {
            "id": "official",
            "name": "官方验证版",
            "description": "用于并行验证官方最新版的隔离环境",
            "home": official_state,
            "code": official_code,
            "port": official_port,
            "kind": "official",
        },
    }


def env_spec(env_id: Optional[str], config: Optional[dict] = None) -> dict:
    specs = get_env_specs(config)
    return specs.get(env_id or active_env_id(config), specs["primary"])


def env_gateway_log(spec: dict) -> Path:
    return Path(spec["home"]) / "logs" / "gateway.log"


def env_gateway_err_log(spec: dict) -> Path:
    return Path(spec["home"]) / "logs" / "gateway.err.log"


def env_dashboard_url(spec: dict) -> str:
    token = ""
    config_path = Path(spec["home"]) / "openclaw.json"
    try:
        token = (
            json.loads(config_path.read_text(encoding="utf-8"))
            .get("gateway", {})
            .get("auth", {})
            .get("token", "")
        )
    except Exception:
        token = ""
    base = f"http://127.0.0.1:{spec['port']}/"
    return f"{base}#token={token}" if token else base


def read_git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def check_gateway_health_for_env(spec: dict) -> bool:
    if spec.get("id") == "primary":
        return check_gateway_health()
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--noproxy",
                "*",
                "-fsS",
                f"http://127.0.0.1:{spec['port']}/health",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout or "{}")
        return bool(payload.get("ok"))
    except Exception:
        return False


def get_gateway_process_for_env(spec: dict) -> Optional[dict]:
    pid = get_listener_pid(int(spec["port"]))
    if pid is None:
        return None
    return get_process_info_by_pid(pid)


def list_openclaw_environments(config: Optional[dict] = None) -> list[dict]:
    cfg = config or load_config()
    current = active_env_id(cfg)
    environments = []
    for item in get_env_specs(cfg).values():
        running = get_listener_pid(int(item["port"])) is not None
        environments.append(
            {
                "id": item["id"],
                "name": item["name"],
                "description": item["description"],
                "port": item["port"],
                "code": str(item["code"]),
                "home": str(item["home"]),
                "git_head": read_git_head(Path(item["code"])),
                "running": running,
                "healthy": check_gateway_health_for_env(item) if running else False,
                "dashboard_url": env_dashboard_url(item),
                "active": item["id"] == current,
            }
        )
    return environments


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


def get_process_info_by_pid(pid: int) -> Optional[dict]:
    """通过 PID 获取进程信息，避免模糊 grep 带来的误判。"""
    try:
        result = subprocess.run(
            f"ps -p {pid} -o pid=,%cpu=,%mem=,command=",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        line = result.stdout.strip()
        if not line:
            return None
        parts = line.split(None, 3)
        if len(parts) < 4:
            return None
        return {
            "pid": int(parts[0]),
            "cpu": float(parts[1]),
            "mem": float(parts[2]),
            "cmd": parts[3][:100],
        }
    except Exception:
        return None


def load_pid_file(pid_file: Path) -> Optional[int]:
    """读取 PID 文件并确认目标进程仍然存活。"""
    try:
        raw = pid_file.read_text().strip()
        if not raw:
            return None
        pid = int(raw)
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def get_guardian_process_info() -> Optional[dict]:
    """优先通过 guardian.pid 获取状态，找不到再回退到精确命令匹配。"""
    pid = load_pid_file(GUARDIAN_PID_FILE)
    if pid is not None:
        info = get_process_info_by_pid(pid)
        if info and "guardian.py" in info.get("cmd", ""):
            return info
    return get_process_info(r"[g]uardian\.py")


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
    mem_wired = 0.0
    mem_compressed = 0.0
    
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
                    used_match = re.search(r'([\d.]+[KMGT])\s*used', line)
                    unused_match = re.search(r'([\d.]+[KMGT])\s*unused', line)
                    wired_match = re.search(r'([\d.]+[KMGT])\s*wired', line)
                    compressor_match = re.search(r'([\d.]+[KMGT])\s*compressor', line)
                    
                    if used_match:
                        mem_used = round(parse_mem_value_to_gb(used_match.group(1)), 2)
                    if wired_match:
                        mem_wired = round(parse_mem_value_to_gb(wired_match.group(1)), 2)
                    if compressor_match:
                        mem_compressed = round(parse_mem_value_to_gb(compressor_match.group(1)), 2)
                    if wired_match:
                        mem_used = max(mem_used, mem_wired)
                    if unused_match and mem_used:
                        mem_total = round(mem_used + parse_mem_value_to_gb(unused_match.group(1)), 2)
                except:
                    pass
    except:
        pass
    
    return {
        "cpu": round(cpu, 1),
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_wired": mem_wired,
        "mem_compressed": mem_compressed,
    }


def analyze_sessions(minutes: int = 5, spec: Optional[dict] = None) -> dict:
    """分析会话 - 每5分钟统计"""
    env = spec or env_spec(None)
    gateway_log = env_gateway_log(env)
    if not gateway_log.exists():
        return {"total": 0, "slow": 0, "stuck": 0, "sessions": []}
    
    sessions = []
    dispatch_time = {}
    lines = []
    
    try:
        with open(gateway_log) as f:
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
                
                # 格式3: 飞书发送的消息 "助手名: 内容"
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


def get_error_logs(count: int = 20, spec: Optional[dict] = None) -> list:
    """获取错误日志"""
    errors = []
    env = spec or env_spec(None)

    for log_file in [env_gateway_err_log(env), env_gateway_log(env)]:
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


def get_version(spec: Optional[dict] = None) -> str:
    """获取版本"""
    env = spec or env_spec(None)
    try:
        result = subprocess.run(
            ["git", "-C", str(env["code"]), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5
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
    versions_file = BASE_DIR / "versions.json"
    if versions_file.exists():
        with open(versions_file) as f:
            return json.load(f)
    return {"current": None, "history": []}


def run_script(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:
        return -1, "", str(exc)


def switch_openclaw_environment(target_env: str) -> tuple[bool, str]:
    if target_env not in {"primary", "official"}:
        return False, "未知环境"

    if not save_config("ACTIVE_OPENCLAW_ENV", target_env):
        return False, "保存 ACTIVE_OPENCLAW_ENV 失败"
    STORE.save_runtime_value("active_openclaw_env", {"env_id": target_env, "updated_at": int(time.time())})

    if target_env == "official":
        run_script([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=60)
        code, stdout, stderr = run_script([str(OFFICIAL_MANAGER), "start"], timeout=300)
        if code != 0:
            return False, (stderr or stdout or "官方验证版启动失败").strip()
        return True, stdout.strip() or "已切换到官方验证版"

    run_script([str(OFFICIAL_MANAGER), "stop"], timeout=60)
    code, stdout, stderr = run_script([str(DESKTOP_RUNTIME), "start", "gateway"], timeout=180)
    if code != 0:
        return False, (stderr or stdout or "主用版启动失败").strip()
    return True, stdout.strip() or "已切换到当前主用版"


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
        .event-list { display: grid; gap: 10px; }
        .event-item {
            padding: 12px 14px; border-radius: 10px; background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
        }
        .event-item.anomaly { border-color: rgba(248,113,113,0.35); background: rgba(248,113,113,0.08); }
        .event-item.pipeline { border-color: rgba(96,165,250,0.35); background: rgba(96,165,250,0.08); }
        .event-meta { font-size: 12px; color: #9ca3af; margin-bottom: 6px; }
        .event-title { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
        .event-details { font-size: 12px; color: #d1d5db; line-height: 1.5; }
        .incident-grid { display: grid; grid-template-columns: 1.1fr 0.9fr 0.9fr; gap: 14px; }
        .incident-card {
            padding: 16px; border-radius: 12px; background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
        }
        .incident-card.error { border-color: rgba(248,113,113,0.35); background: rgba(248,113,113,0.1); }
        .incident-card.watch { border-color: rgba(251,191,36,0.35); background: rgba(251,191,36,0.08); }
        .incident-label { font-size: 12px; color: #9ca3af; margin-bottom: 8px; }
        .incident-main { font-size: 18px; font-weight: 700; line-height: 1.4; }
        .incident-sub { font-size: 13px; color: #d1d5db; margin-top: 8px; line-height: 1.5; }
        .memory-summary {
            display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 16px; margin-bottom: 14px;
        }
        .memory-box {
            padding: 14px 16px; border-radius: 12px; background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
        }
        .memory-box-title { font-size: 12px; color: #9ca3af; margin-bottom: 8px; }
        .memory-box-main { font-size: 18px; font-weight: 700; line-height: 1.4; }
        .memory-box-sub { font-size: 12px; color: #d1d5db; margin-top: 8px; line-height: 1.5; }
        .memory-items { display: grid; gap: 8px; }
        .memory-item {
            display: flex; justify-content: space-between; gap: 12px; align-items: center;
            padding: 10px 12px; border-radius: 10px; background: rgba(255,255,255,0.04);
        }
        .memory-item-name { font-size: 13px; font-weight: 600; }
        .memory-item-note { font-size: 11px; color: #9ca3af; margin-top: 4px; }
        .memory-item-value { font-size: 13px; color: #f3f4f6; white-space: nowrap; }
        .env-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 14px 0 6px; }
        .env-card {
            padding: 16px; border-radius: 12px; background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.08);
        }
        .env-card.active {
            border-color: rgba(74,222,128,0.45);
            box-shadow: 0 0 0 1px rgba(74,222,128,0.2) inset;
        }
        .env-title-row { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 8px; }
        .env-title { font-size: 18px; font-weight: 700; }
        .env-pill {
            font-size: 11px; padding: 4px 8px; border-radius: 999px; background: rgba(255,255,255,0.08);
            color: #d1d5db;
        }
        .env-pill.active { background: rgba(74,222,128,0.18); color: #bbf7d0; }
        .env-meta { font-size: 12px; color: #9ca3af; line-height: 1.7; margin-top: 10px; }
        .env-actions { display: flex; gap: 8px; margin-top: 14px; }
        .env-link { color: #93c5fd; text-decoration: none; font-size: 12px; }
        .env-link:hover { text-decoration: underline; }
        .env-link.disabled { color: #6b7280; pointer-events: none; cursor: not-allowed; text-decoration: none; }
        
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
                <button class="btn btn-primary" onclick="restartGateway()">🔁 重启 Gateway</button>
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

        <div class="section">
            <h2>🧭 版本环境</h2>
            <div id="environment-summary" class="memory-box" style="margin-bottom:14px;"></div>
            <div id="environment-cards" class="env-grid"></div>
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
            <h2>🎯 问题定位</h2>
            <div id="incident-summary" class="incident-grid"></div>
        </div>

        <div class="section">
            <h2>🚨 最近异常 / 进度</h2>
            <div id="recent-events" class="event-list"></div>
        </div>
        
        <div class="section">
            <h2>💻 内存归因：进程 Top 15 + 系统项</h2>
            <div id="memory-attribution" class="memory-summary"></div>
            <div id="memory-items" class="memory-items"></div>
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
                const memSummary = data.memory_summary || {};
                document.getElementById('mem-sub').textContent = `${memPercent}% | Top15覆盖 ${memSummary.process_coverage_percent || 0}%`;
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

                // 版本环境
                const envSummaryEl = document.getElementById('environment-summary');
                const envCardsEl = document.getElementById('environment-cards');
                const envs = data.environments || [];
                const activeEnv = envs.find(item => item.active) || null;
                envSummaryEl.innerHTML = activeEnv ? `
                    <div class="memory-box-title">当前守护目标</div>
                    <div class="memory-box-main">${activeEnv.name} · ${activeEnv.healthy ? '健康' : (activeEnv.running ? '运行中但健康检查失败' : '未运行')}</div>
                    <div class="memory-box-sub">端口 ${activeEnv.port} · 版本 ${activeEnv.git_head} · ${activeEnv.code}</div>
                ` : `
                    <div class="memory-box-title">当前守护目标</div>
                    <div class="memory-box-main">未识别</div>
                    <div class="memory-box-sub">请检查版本环境配置。</div>
                `;
                envCardsEl.innerHTML = envs.map(item => `
                    <div class="env-card ${item.active ? 'active' : ''}">
                        <div class="env-title-row">
                            <div class="env-title">${item.name}</div>
                            <div class="env-pill ${item.active ? 'active' : ''}">${item.active ? '当前守护中' : '可切换'}</div>
                        </div>
                        <div class="event-details">${item.description}</div>
                        <div class="env-meta">
                            端口: ${item.port}<br/>
                            版本: ${item.git_head}<br/>
                            状态: ${item.running ? (item.healthy ? '健康' : '运行中但异常') : '未运行'}
                        </div>
                        <div class="env-actions">
                            <button class="btn ${item.active ? '' : 'btn-primary'}" ${item.active ? 'disabled' : ''} onclick="switchEnvironment('${item.id}')">
                                ${item.active ? '当前环境' : '切换到这里'}
                            </button>
                            <a class="env-link ${item.running ? '' : 'disabled'}" ${item.running ? `href="${item.dashboard_url}" target="_blank" rel="noopener"` : 'href="javascript:void(0)" aria-disabled="true"'}>打开 Dashboard</a>
                        </div>
                    </div>
                `).join('');
                
                // 慢会话
                const tbody = document.getElementById('slow-sessions');
                tbody.innerHTML = data.sessions.sessions.length ? '' : '<tr><td colspan="5" style="text-align:center;color:#666">暂无会话</td></tr>';
                data.sessions.sessions.forEach(s => {
                    const row = document.createElement('tr');
                    row.innerHTML = `<td>${s.time}</td><td title="${s.question}">${s.question || '-'}</td><td>${s.replies || 0}条</td><td>${s.duration}s</td><td>${s.status}</td>`;
                    tbody.appendChild(row);
                });
                
                // 内存占用排行
                const memoryAttrEl = document.getElementById('memory-attribution');
                const memoryItemsEl = document.getElementById('memory-items');
                if (data.memory_summary) {
                    memoryAttrEl.innerHTML = `
                        <div class="memory-box">
                            <div class="memory-box-title">总览口径</div>
                            <div class="memory-box-main">${data.memory_summary.summary}</div>
                            <div class="memory-box-sub">${data.memory_summary.note}</div>
                        </div>
                        <div class="memory-box">
                            <div class="memory-box-title">对账结果</div>
                            <div class="memory-box-main">Top 15: ${data.memory_summary.top15_gb.toFixed(1)}G</div>
                            <div class="memory-box-sub">未归属到单进程: ${data.memory_summary.unattributed_gb.toFixed(1)}G</div>
                        </div>
                    `;
                    memoryItemsEl.innerHTML = (data.memory_summary.items || []).map(item => `
                        <div class="memory-item">
                            <div>
                                <div class="memory-item-name">${item.name}</div>
                                <div class="memory-item-note">${item.note || ''}</div>
                            </div>
                            <div class="memory-item-value">${item.value_gb.toFixed(1)}G</div>
                        </div>
                    `).join('');
                } else {
                    memoryAttrEl.innerHTML = '';
                    memoryItemsEl.innerHTML = '';
                }
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
                        ${d.action ? `<button class="diagnose-action" onclick="restartGateway()">${d.action}</button>` : ''}
                    </div>
                `).join('');

                // 问题定位摘要
                const incidentEl = document.getElementById('incident-summary');
                const incident = data.incident_summary || {};
                incidentEl.innerHTML = `
                    <div class="incident-card ${incident.status || 'ok'}">
                        <div class="incident-label">当前关注点</div>
                        <div class="incident-main">${incident.headline || '最近未发现明显异常'}</div>
                        <div class="incident-sub">${incident.focus || '-'}</div>
                    </div>
                    <div class="incident-card">
                        <div class="incident-label">最后阶段</div>
                        <div class="incident-main">${incident.last_stage || '-'}</div>
                        <div class="incident-sub">用于快速判断当前卡在哪个环节。</div>
                    </div>
                    <div class="incident-card">
                        <div class="incident-label">建议动作</div>
                        <div class="incident-main">${incident.action || '继续观察即可'}</div>
                        <div class="incident-sub">最近问题: ${incident.last_question || '-'}</div>
                    </div>
                `;

                // 最近异常 / 进度
                const eventsEl = document.getElementById('recent-events');
                if (!data.recent_events || data.recent_events.length === 0) {
                    eventsEl.innerHTML = '<div class="event-item"><div class="event-title">暂无最近异常</div><div class="event-details">Guardian 已启动，但最近没有记录到异常或阶段事件。</div></div>';
                } else {
                    eventsEl.innerHTML = data.recent_events.map(item => {
                        const details = formatChangeDetails(item);
                        const typeLabel = item.type === 'anomaly' ? '异常' : '进度';
                        const stamp = `${item.date || ''} ${item.time || ''}`.trim();
                        return `
                            <div class="event-item ${item.type}">
                                <div class="event-meta">${typeLabel} · ${stamp || '-'}</div>
                                <div class="event-title">${item.message}</div>
                                <div class="event-details">${details}</div>
                            </div>
                        `;
                    }).join('');
                }
                
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
                document.getElementById('version-history').textContent = data.version.history ? data.version.history.length + ' 个历史版本' : '无';
                
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
        
        async function restartGateway() {
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = '重启中...';
            try {
                const res = await fetch('/api/restart', {method: 'POST'});
                const data = await res.json();
                // 3秒后自动刷新数据
                setTimeout(() => {
                    loadData();
                    btn.disabled = false;
                    btn.textContent = '🔁 重启 Gateway';
                }, 3000);
            } catch(e) {
                btn.disabled = false;
                btn.textContent = '🔁 重启 Gateway';
                alert('重启请求失败');
            }
        }

        async function switchEnvironment(envId) {
            if (!confirm(`确认切换守护目标到 ${envId} 吗？这会停止另一边的 Gateway。`)) return;
            try {
                const res = await fetch('/api/environments/switch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({env_id: envId})
                });
                const data = await res.json();
                showToast(data.success ? 'success' : 'error', data.message || (data.success ? '切换成功' : '切换失败'));
                setTimeout(loadData, 2000);
            } catch (e) {
                showToast('error', e.message || '切换失败');
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
                if (data.success) loadData();
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
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666">暂无日志</td></tr>';
                    return;
                }
                tbody.innerHTML = data.changes.reverse().map(c => {
                    const typeIcon = {'restart': '🔁', 'config': '⚙️', 'recover': '🚨', 'update': '🔄', 'version': '📋', 'pipeline': '⏳', 'snapshot': '📦', 'anomaly': '🚨'}[c.type] || '📝';
                    return `<tr><td>${c.date || ''}</td><td>${c.time}</td><td>${typeIcon} ${c.type}</td><td>${c.message}</td><td>${formatChangeDetails(c)}</td></tr>`;
                }).join('');
            } catch(e) {
                console.error(e);
            }
        }

        function formatChangeDetails(change) {
            const details = change.details || {};
            if (change.type === 'pipeline') {
                return `阶段: ${details.marker || '-'} | 时间: ${details.timestamp || '-'}`;
            }
            if (change.type === 'anomaly') {
                const marker = details.marker ? ` | 阶段: ${details.marker}` : '';
                return `问题: ${details.question || '-'} | 耗时: ${details.duration || '-'}秒${marker} | 时间: ${details.timestamp || '-'}`;
            }
            if (change.type === 'restart') {
                return `PID: ${details.old_pid || '-'} -> ${details.new_pid || '-'}`;
            }
            if (change.type === 'recover') {
                return `快照: ${details.snapshot || '-'} `;
            }
            if (change.type === 'version') {
                if (details.from && details.to) return `${details.from} -> ${details.to}`;
                return details.commit || '-';
            }
            if (change.type === 'snapshot') {
                return `快照: ${details.snapshot || '-'} `;
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
    config = load_config()
    selected_env = env_spec(active_env_id(config), config)
    metrics = get_system_metrics()
    gateway_process = get_gateway_process_for_env(selected_env)
    guardian_process = get_guardian_process_info()
    gateway_healthy = check_gateway_health_for_env(selected_env)
    sessions = analyze_sessions(5, selected_env)
    errors = get_error_logs(20, selected_env)
    version = get_version(selected_env)
    safe_config = sanitize_config_for_ui(config)
    version_history = load_versions()
    diagnoses = get_diagnoses(metrics, sessions, [gateway_process, guardian_process])
    top_processes = get_top_processes(15)
    recent_events = get_recent_anomalies(limit=8, days=7)
    incident_summary = build_incident_summary(recent_events)
    memory_summary = summarize_memory_usage(metrics, top_processes)
    environments = list_openclaw_environments(config)
    
    data = {
        "active_environment": selected_env["id"],
        "environments": environments,
        "metrics": metrics,
        "gateway_process": gateway_process,
        "guardian_process": guardian_process,
        "gateway_healthy": gateway_healthy,
        "sessions": sessions,
        "errors": errors,
        "version": {"current": version, "history": version_history.get("history", [])},
        "config": safe_config,
        "diagnoses": diagnoses,
        "top_processes": top_processes,
        "recent_events": recent_events,
        "incident_summary": incident_summary,
        "memory_summary": memory_summary,
    }
    return app.response_class(
        response=json.dumps(data, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route("/api/environments/switch", methods=["POST"])
def api_switch_environment():
    """切换当前守护目标环境。"""
    try:
        data = request.get_json(silent=True) or {}
        env_id = str(data.get("env_id", "")).strip()
        success, message = switch_openclaw_environment(env_id)
        if success:
            record_change("version", f"切换守护环境到 {env_id}", {"to": env_id})
        return jsonify({"success": success, "message": message, "env_id": env_id})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """重启 Gateway"""
    try:
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
            record_change("restart", f"Gateway 重启成功 (PID: {old_pid_str} → {new_pid})",
                         {"old_pid": old_pid_str, "new_pid": new_pid})
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

        record_change("recover", f"恢复配置快照并重启: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
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
        record_change("snapshot", f"手动创建配置快照: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
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

        record_change("recover", f"恢复指定配置快照并重启: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
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
        
        if save_config(key, str(value)):
            return jsonify({"success": True, "message": "配置已更新"})
        else:
            return jsonify({"success": False, "message": "保存配置失败"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


if __name__ == "__main__":
    import socket
    raise_nofile_limit()
    
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

    requested_port = os.environ.get("DASHBOARD_PORT", "").strip()
    if requested_port.isdigit():
        port = int(requested_port)
    else:
        port = find_free_port()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    print("=" * 50)
    print("OpenClaw 健康监控中心")
    print(f"访问: http://{host}:{port}")
    print("=" * 50)
    app.run(host=host, port=port, debug=False)
