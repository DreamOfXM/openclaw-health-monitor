#!/usr/bin/env python3
"""
OpenClaw Guardian - 独立守护进程
功能：进程守护、健康检查、告警通知、自动更新
"""

import os
import sys
import json
import time
import signal
import socket
import subprocess
import threading
import requests
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List

from monitor_config import DEFAULT_CONFIG, load_config as load_shared_config
from snapshot_manager import SnapshotManager
from state_store import MonitorStateStore

# ========== 配置 ==========
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.conf"
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.conf"
ALERTS_FILE = BASE_DIR / "alerts.json"
VERSIONS_FILE = BASE_DIR / "versions.json"
LOG_FILE = BASE_DIR / "logs" / "guardian.log"

OPENCLAW_HOME = Path.home() / ".openclaw"
OPENCLAW_CODE = Path.home() / "openclaw-workspace" / "openclaw"
GATEWAY_LOG = OPENCLAW_HOME / "logs" / "gateway.log"
TMP_OPENCLAW_LOG_DIR = Path("/tmp/openclaw")

CONFIG = {}
ALERTS = {}
VERSIONS = {"current": None, "history": []}
STORE = MonitorStateStore(BASE_DIR)
SNAPSHOTS = SnapshotManager(BASE_DIR, OPENCLAW_HOME)


def load_config():
    """加载配置文件"""
    global CONFIG
    CONFIG = load_shared_config(BASE_DIR)


def load_alerts():
    """加载告警历史"""
    global ALERTS
    ALERTS = STORE.load_alerts(ALERTS_FILE)


def save_alerts():
    """保存告警历史"""
    STORE.save_alerts(ALERTS)
    with open(ALERTS_FILE, "w") as f:
        json.dump(ALERTS, f, indent=2)


def load_versions():
    """加载版本历史"""
    global VERSIONS
    VERSIONS = STORE.load_versions(VERSIONS_FILE)


def save_versions():
    """保存版本历史"""
    STORE.save_versions(VERSIONS)
    with open(VERSIONS_FILE, "w") as f:
        json.dump(VERSIONS, f, indent=2)


def log(msg: str, level: str = "INFO"):
    """日志输出"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run_cmd(cmd: str) -> tuple:
    """执行命令"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def get_process_info(name: str) -> Optional[Dict]:
    """获取进程信息"""
    code, stdout, _ = run_cmd(f'ps aux | grep -i "{name}" | grep -v grep')
    if code != 0 or not stdout:
        return None
    
    lines = stdout.strip().split("\n")
    if not lines:
        return None
    
    parts = lines[0].split()
    if len(parts) < 11:
        return None
    
    return {
        "pid": int(parts[1]),
        "cpu": float(parts[2]),
        "mem": float(parts[3]),
        "cmd": " ".join(parts[10:]),
    }


def get_listener_pid(port: int) -> Optional[int]:
    """返回监听指定端口的 PID。"""
    code, stdout, _ = run_cmd(f"lsof -ti tcp:{port} -sTCP:LISTEN")
    if code != 0 or not stdout.strip():
        return None
    try:
        return int(stdout.strip().splitlines()[0])
    except ValueError:
        return None


def check_gateway_health() -> bool:
    """检查 Gateway 健康状态"""
    retries = CONFIG.get("HEALTH_CHECK_RETRIES", 3)
    delay = CONFIG.get("HEALTH_CHECK_DELAY", 5)

    for i in range(retries):
        code, stdout, stderr = run_cmd("openclaw gateway health")
        output = f"{stdout}\n{stderr}".lower()
        if code == 0 and "gateway target" not in output:
            return True
        if i < retries - 1:
            time.sleep(delay)
    return False


def check_process_running() -> bool:
    """检查进程是否运行"""
    return get_listener_pid(int(CONFIG.get("GATEWAY_PORT", 18789))) is not None


def get_system_metrics() -> Dict:
    """获取系统指标"""
    cpu = 0.0
    mem_used = 0
    mem_total = 32
    
    code, stdout, _ = run_cmd("top -l 1 -n 0")
    if code == 0:
        for line in stdout.split("\n"):
            if "CPU usage" in line:
                try:
                    user = float([x for x in line.split() if "user" in x][0].replace("%user", ""))
                    system = float([x for x in line.split() if "sys" in x][0].replace("%sys", ""))
                    cpu = user + system
                except:
                    pass
            if "PhysMem" in line:
                try:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if "G" in p and "used" in p:
                            mem_used = int(p.replace("G", "").replace("used", ""))
                        if "G" in p and "unused" in p:
                            mem_total = mem_used + int(p.replace("G", "").replace("unused", ""))
                except:
                    pass
    
    return {"cpu": round(cpu, 1), "mem_used": mem_used, "mem_total": mem_total}


def analyze_slow_sessions() -> List[Dict]:
    """分析慢会话"""
    if not GATEWAY_LOG.exists():
        return []
    
    slow_threshold = CONFIG.get("SLOW_RESPONSE_THRESHOLD", 30)
    sessions = []
    now = time.time()
    
    try:
        with open(GATEWAY_LOG) as f:
            lines = f.readlines()[-2000:]  # 最近2000行
        
        dispatch_time = {}
        
        for line in lines:
            if "dispatching to agent" in line.lower():
                try:
                    # 提取时间戳
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").timestamp()
                    dispatch_time["dispatch"] = ts
                except:
                    pass
            elif "dispatch complete" in line.lower():
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").timestamp()
                    if "dispatch" in dispatch_time:
                        duration = ts - dispatch_time["dispatch"]
                        if duration > slow_threshold:
                            sessions.append({
                                "time": ts_str,
                                "duration": int(duration),
                                "reason": "LLM响应慢" if duration > 60 else "处理耗时"
                            })
                        dispatch_time = {}
                except:
                    pass
    except Exception as e:
        log(f"分析慢会话失败: {e}")
    
    return sessions[-10:]  # 返回最近10个


def resolve_runtime_gateway_log() -> Path:
    """Return the most recent runtime log file."""
    candidates = sorted(TMP_OPENCLAW_LOG_DIR.glob("openclaw-*.log"), key=lambda p: p.stat().st_mtime)
    if candidates:
        return candidates[-1]
    return GATEWAY_LOG


def scan_pipeline_progress_events() -> None:
    """Scan runtime logs for pipeline progress markers and persist them once."""
    runtime_log = resolve_runtime_gateway_log()
    if not runtime_log.exists():
        return

    cursor = STORE.load_runtime_value("pipeline_progress_cursor", {})
    last_signature = cursor.get("last_signature", "")

    try:
        with open(runtime_log) as handle:
            lines = handle.readlines()[-2000:]
    except Exception as exc:
        log(f"读取运行日志失败: {exc}", "ERROR")
        return

    latest_signature = last_signature
    for line in lines:
        if "PIPELINE_PROGRESS:" not in line:
            continue
        signature = line.strip()
        if signature <= last_signature:
            continue

        marker = line.split("PIPELINE_PROGRESS:", 1)[1].strip()
        ts_match = line.split('"time":"')
        timestamp = ""
        if len(ts_match) > 1:
            timestamp = ts_match[1].split('"', 1)[0]

        message = f"多智能体进度: {marker}"
        details = {
            "marker": marker,
            "timestamp": timestamp,
            "source_log": str(runtime_log),
        }
        record_change_log("pipeline", message, details)
        latest_signature = signature

    if latest_signature != last_signature:
        STORE.save_runtime_value(
            "pipeline_progress_cursor",
            {"last_signature": latest_signature, "source_log": str(runtime_log)},
        )


def parse_runtime_timestamp(line: str) -> tuple[str, float | None]:
    """Parse ISO timestamp from either JSONL runtime logs or plain text logs."""
    if line.startswith("{"):
        marker = '"time":"'
        if marker in line:
            raw = line.split(marker, 1)[1].split('"', 1)[0]
            normalized = raw[:19]
            try:
                ts = datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S").timestamp()
                return raw, ts
            except Exception:
                return raw, None
    raw = line[:19]
    try:
        ts = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").timestamp()
        return raw, ts
    except Exception:
        return raw, None


def extract_runtime_question(line: str) -> str | None:
    """Extract a user-visible question from runtime logs when possible."""
    lower = line.lower()
    if " dm from " in lower and ": " in line:
        idx = line.find(": ")
        if idx > 0:
            return line[idx + 2 :].strip()[:80]
    if "message in" in lower and ": " in line:
        idx = line.find(": ")
        if idx > 0:
            return line[idx + 2 :].strip()[:80]
    if "feishu[default]:" in lower and ": " in line:
        idx = line.find(": ")
        if idx > 0:
            return line[idx + 2 :].strip()[:80]
    return None


def extract_pipeline_marker(line: str) -> str | None:
    """Extract a pipeline progress marker from the runtime logs."""
    marker = "PIPELINE_PROGRESS:"
    if marker not in line:
        return None
    return line.split(marker, 1)[1].strip()[:120]


def trim_runtime_seen(seen: dict[str, int], keep: int = 2000) -> dict[str, int]:
    """Bound the anomaly dedupe table so it cannot grow forever."""
    if len(seen) <= keep:
        return seen
    newest = sorted(seen.items(), key=lambda item: item[1], reverse=True)[:keep]
    return dict(newest)


def collect_runtime_anomalies(
    lines: list[str],
    *,
    now: float,
    slow_threshold: int,
    stalled_threshold: int,
) -> tuple[list[dict[str, Any]], str]:
    """Build anomaly records from recent runtime logs."""
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    latest_signature = ""

    for line in lines:
        signature = line.strip()
        if signature and signature > latest_signature:
            latest_signature = signature

        ts_raw, ts = parse_runtime_timestamp(line)
        message = extract_runtime_question(line)
        if message and ts is not None:
            question_candidates.append((int(ts), message))
            question_candidates = question_candidates[-50:]

        lower = line.lower()
        if "dispatching to agent" in lower and ts is not None:
            nearest_question = ""
            for qts, qmsg in reversed(question_candidates):
                if abs(int(ts) - qts) <= 15:
                    nearest_question = qmsg
                    break
            open_dispatches.append(
                {
                    "started_at": ts,
                    "timestamp": ts_raw,
                    "question": nearest_question or "未知问题",
                    "last_progress_at": ts,
                    "marker": "",
                }
            )
            continue

        marker = extract_pipeline_marker(line)
        if marker and ts is not None and open_dispatches:
            dispatch = open_dispatches[-1]
            dispatch["last_progress_at"] = ts
            dispatch["marker"] = marker
            continue

        if "dispatch complete" in lower and open_dispatches:
            dispatch = open_dispatches.pop(0)
            duration = int(ts - dispatch["started_at"]) if ts is not None else 0
            queued_final = "queuedfinal=true" in lower
            replies = 0
            if "replies=" in lower:
                try:
                    replies = int(lower.split("replies=", 1)[1].split(")", 1)[0].split(",", 1)[0])
                except Exception:
                    replies = 0

            details = {
                "question": dispatch["question"],
                "duration": duration,
                "timestamp": ts_raw,
            }
            if dispatch.get("marker"):
                details["marker"] = dispatch["marker"]

            if (not queued_final) or replies == 0:
                details["queued_final"] = queued_final
                details["replies"] = replies
                anomalies.append(
                    {
                        "signature": signature,
                        "type": "no_reply",
                        "message": "任务完成但没有可见回复",
                        "details": details,
                    }
                )
            elif duration >= stalled_threshold:
                anomalies.append(
                    {
                        "signature": signature,
                        "type": "stalled_reply",
                        "message": "任务响应严重超时",
                        "details": details,
                    }
                )
            elif duration >= slow_threshold:
                anomalies.append(
                    {
                        "signature": signature,
                        "type": "slow_reply",
                        "message": "任务响应偏慢",
                        "details": details,
                    }
                )
            continue

        if "gateway closed" in lower and "1006" in lower:
            anomalies.append(
                {
                    "signature": signature,
                    "type": "gateway_ws_closed",
                    "message": "Gateway WebSocket 异常关闭",
                    "details": {"timestamp": ts_raw},
                }
            )
        elif "abort failed" in lower and "no_active_run" in lower:
            anomalies.append(
                {
                    "signature": signature,
                    "type": "run_tracking_warning",
                    "message": "任务状态追踪异常",
                    "details": {"timestamp": ts_raw},
                }
            )

    for dispatch in open_dispatches:
        duration = int(now - dispatch["started_at"])
        details = {
            "question": dispatch["question"],
            "duration": duration,
            "timestamp": dispatch["timestamp"],
        }
        if dispatch.get("marker"):
            details["marker"] = dispatch["marker"]

        if dispatch.get("marker") and int(now - dispatch["last_progress_at"]) >= stalled_threshold:
            anomalies.append(
                {
                    "signature": f"stage:{dispatch['timestamp']}:{dispatch['marker']}",
                    "type": "stage_stuck",
                    "message": "任务阶段长时间无进展",
                    "details": details,
                }
            )
        elif duration >= stalled_threshold:
            anomalies.append(
                {
                    "signature": f"open:{dispatch['timestamp']}:{dispatch['question']}",
                    "type": "dispatch_stuck",
                    "message": "任务长时间无最终结果",
                    "details": details,
                }
            )

    return anomalies, latest_signature


def scan_runtime_anomalies() -> list[dict]:
    """Detect stalled or no-reply situations from runtime logs."""
    runtime_log = resolve_runtime_gateway_log()
    if not runtime_log.exists():
        return []

    cursor = STORE.load_runtime_value("runtime_anomaly_cursor", {})
    last_signature = cursor.get("last_signature", "")
    stalled_threshold = int(CONFIG.get("STALLED_RESPONSE_THRESHOLD", 90))
    slow_threshold = int(CONFIG.get("SLOW_RESPONSE_THRESHOLD", 30))

    try:
        with open(runtime_log) as handle:
            lines = handle.readlines()[-4000:]
    except Exception as exc:
        log(f"读取运行日志失败: {exc}", "ERROR")
        return []
    anomalies, latest_signature = collect_runtime_anomalies(
        lines,
        now=time.time(),
        slow_threshold=slow_threshold,
        stalled_threshold=stalled_threshold,
    )
    latest_signature = latest_signature or last_signature

    seen = STORE.load_runtime_value("runtime_anomaly_seen", {})
    recorded: list[dict] = []
    for anomaly in anomalies:
        signature = anomaly["signature"]
        if signature in seen:
            continue
        seen[signature] = int(time.time())
        record_change_log("anomaly", anomaly["message"], anomaly["details"])
        recorded.append(anomaly)

        alert_key = f"runtime_anomaly_{anomaly['type']}"
        if should_alert(alert_key):
            detail_lines = [f"类型: {anomaly['type']}"]
            if anomaly["details"].get("question"):
                detail_lines.append(f"问题: {anomaly['details']['question']}")
            if anomaly["details"].get("duration") is not None:
                detail_lines.append(f"耗时: {anomaly['details']['duration']}秒")
            if anomaly["details"].get("timestamp"):
                detail_lines.append(f"时间: {anomaly['details']['timestamp']}")
            notify("OpenClaw 任务异常", "\n".join(detail_lines), "warning")

    if latest_signature != last_signature:
        STORE.save_runtime_value(
            "runtime_anomaly_cursor",
            {"last_signature": latest_signature, "source_log": str(runtime_log)},
        )
    STORE.save_runtime_value("runtime_anomaly_seen", trim_runtime_seen(seen))
    return recorded


def has_config_changes() -> bool:
    """检查配置变更"""
    if (OPENCLAW_HOME / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {OPENCLAW_HOME} && git diff --quiet")
        if code != 0:
            return True
    
    if OPENCLAW_CODE.exists() and (OPENCLAW_CODE / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {OPENCLAW_CODE} && git diff --quiet")
        if code != 0:
            return True
    
    return False


def get_current_version() -> str:
    """获取当前版本"""
    if (OPENCLAW_CODE / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {OPENCLAW_CODE} && git describe --tags --always")
        if code == 0 and stdout.strip():
            return stdout.strip()
    return "unknown"


def should_alert(alert_type: str) -> bool:
    """检查是否应该发送告警（去重）"""
    now = time.time()
    interval = CONFIG.get("ALERT_DEDUP_INTERVAL", 600)
    
    if alert_type not in ALERTS:
        ALERTS[alert_type] = {"last_alert": now, "count": 1}
        return True
    
    last_alert = ALERTS[alert_type]["last_alert"]
    if now - last_alert < interval:
        return False
    
    ALERTS[alert_type] = {"last_alert": now, "count": ALERTS[alert_type].get("count", 0) + 1}
    return True


def notify(title: str, message: str, level: str = "info"):
    """发送通知"""
    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "success": "✅"}.get(level, "ℹ️")
    text = f"## {emoji} OpenClaw Guardian\n\n**{title}**\n\n{message}"
    
    # 钉钉
    if CONFIG.get("DINGTALK_WEBHOOK"):
        try:
            requests.post(
                CONFIG["DINGTALK_WEBHOOK"],
                json={
                    "msgtype": "markdown",
                    "markdown": {"title": title, "text": text}
                },
                timeout=10
            )
            log(f"钉钉通知已发送: {title}")
        except Exception as e:
            log(f"钉钉通知失败: {e}")
    
    # 飞书
    if CONFIG.get("FEISHU_WEBHOOK"):
        try:
            requests.post(
                CONFIG["FEISHU_WEBHOOK"],
                json={"msg_type": "text", "content": f"{title}\n{message}"},
                timeout=10
            )
            log(f"飞书通知已发送: {title}")
        except Exception as e:
            log(f"飞书通知失败: {e}")
    
    # macOS 通知
    if CONFIG.get("ENABLE_MAC_NOTIFY"):
        run_cmd(f'osascript -e \'display notification "{message}" with title "OpenClaw Guardian: {title}"\'')


def restart_gateway():
    """重启 Gateway"""
    log("尝试重启 Gateway...")

    port = int(CONFIG.get("GATEWAY_PORT", 18789))
    existing_pid = get_listener_pid(port)
    if existing_pid:
        try:
            os.kill(existing_pid, signal.SIGTERM)
            time.sleep(2)
        except OSError as exc:
            log(f"结束旧 Gateway 失败: {exc}", "ERROR")

    try:
        with open(LOG_FILE, "a") as log_handle:
            subprocess.Popen(
                ["openclaw", "gateway", "run"],
                cwd=str(OPENCLAW_CODE),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
    except Exception as exc:
        log(f"启动 Gateway 失败: {exc}", "ERROR")
        return False

    time.sleep(8)
    if check_gateway_health():
        log("Gateway 重启成功")
        return True

    log("Gateway 重启失败: 健康检查未通过", "ERROR")
    return False


def rollback_to_last_good() -> bool:
    """恢复最近一次配置快照。"""
    if not CONFIG.get("ENABLE_SNAPSHOT_RECOVERY", True):
        log("已禁用 snapshot recovery，跳过配置恢复")
        return False

    snapshot_dir = SNAPSHOTS.restore_latest_snapshot()
    if snapshot_dir is None:
        log("没有可用的配置快照")
        return False

    log(f"✅ 已恢复配置快照: {snapshot_dir.name}")
    record_change_log("recover", f"恢复配置快照: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
    return True


def check_update_available() -> bool:
    """检查是否有可用更新"""
    if not (OPENCLAW_CODE / ".git").exists():
        return False
    
    code, _, _ = run_cmd(f"cd {OPENCLAW_CODE} && git fetch --dry-run")
    return code == 0


def do_auto_update() -> bool:
    """执行自动更新"""
    if not CONFIG.get("AUTO_UPDATE", False):
        return False
    
    channel = CONFIG.get("UPDATE_CHANNEL", "stable")
    log(f"执行自动更新 ({channel})...")
    
    # 备份当前版本
    current_ver = get_current_version()
    VERSIONS["current"] = current_ver
    VERSIONS["history"].append({
        "version": current_ver,
        "date": datetime.now().isoformat(),
        "commit": run_cmd(f"cd {OPENCLAW_CODE} && git rev-parse HEAD")[1].strip()
    })
    # 保留最近5个版本
    VERSIONS["history"] = VERSIONS["history"][-5:]
    save_versions()
    
    # 执行更新
    code, _, stderr = run_cmd(f"openclaw update --channel {channel}")
    
    if code != 0:
        log(f"更新失败: {stderr}")
        # 回滚到稳定版本
        if rollback_to_last_good():
            restart_gateway()
        notify("自动更新失败", f"更新失败，已回退到上一版本\n{stderr[:200]}", "error")
        return False
    
    # 重启
    time.sleep(2)
    if not restart_gateway():
        # 回滚到稳定版本
        rollback_to_last_good()
        restart_gateway()
        notify("更新后启动失败", "已自动回退到上一版本", "error")
        return False
    
    new_ver = get_current_version()
    VERSIONS["current"] = new_ver
    save_versions()
    
    notify("自动更新成功", f"已更新到 {new_ver}", "success")
    return True


def save_stable_version():
    """保存当前稳定版本"""
    try:
        if (OPENCLAW_CODE / ".git").exists():
            commit = run_cmd(f"cd {OPENCLAW_CODE} && git rev-parse HEAD")[1].strip()
            old_commit = VERSIONS.get("stable", {}).get("commit", "")
            
            VERSIONS["stable"] = {
                "commit": commit,
                "date": datetime.now().isoformat()
            }
            save_versions()
            
            # 记录版本变更
            if old_commit and old_commit != commit:
                record_change_log("version", f"版本变更: {old_commit[:8]} → {commit[:8]}", 
                                 {"from": old_commit, "to": commit})
            elif not old_commit:
                record_change_log("version", f"初始稳定版本: {commit[:8]}", {"commit": commit})
            
            log(f"✅ 已标记稳定版本: {commit[:8]}")
    except:
        pass


def capture_snapshot(label: str) -> bool:
    """为当前 OpenClaw 关键配置创建快照。"""
    if not CONFIG.get("ENABLE_SNAPSHOT_RECOVERY", True):
        return False
    snapshot_dir = SNAPSHOTS.create_snapshot(label)
    if snapshot_dir is None:
        return False
    keep = int(CONFIG.get("SNAPSHOT_RETENTION", 10))
    SNAPSHOTS.prune(keep)
    record_change_log("snapshot", f"创建配置快照: {snapshot_dir.name}", {"snapshot": snapshot_dir.name})
    return True


def record_change_log(change_type: str, message: str, details: Optional[dict] = None):
    """记录变更到独立日志"""
    try:
        STORE.record_change(change_type, message, details)
        from pathlib import Path
        log_dir = Path(__file__).parent / "change-logs"
        log_dir.mkdir(exist_ok=True)
        
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.json"
        
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
    except:
        pass
def main():
    load_config()
    load_alerts()
    load_versions()
    
    log("=" * 50)
    log("OpenClaw Guardian 启动")
    log("=" * 50)
    
    notify("Guardian 启动", "OpenClaw 守护进程已运行", "info")
    
    gateway_was_healthy = False
    last_check_time = 0
    
    while True:
        try:
            # 系统指标
            metrics = get_system_metrics()
            
            # 进程状态
            process_running = check_process_running()
            gateway_healthy = check_gateway_health()
            
            now = time.time()
            
            # 状态变化检测
            if process_running and not gateway_was_healthy:
                log("Gateway 从异常变为正常")
                gateway_was_healthy = True
                
                # 检查是否有配置变更
                if has_config_changes():
                    log("检测到配置变更后启动成功，正常操作")
                else:
                    pass  # 正常启动，不通知
                
                # 标记当前版本为稳定版本
                save_stable_version()
                capture_snapshot("healthy")
            
            elif not process_running or not gateway_healthy:
                if gateway_was_healthy:
                    log("Gateway 从正常变为异常！")
                    
                    # 给一点时间等待手动重启（用户可能正在重启）
                    time.sleep(20)
                    process_running = check_process_running()
                    gateway_healthy = check_gateway_health()
                    
                    # 再次检查，如果仍然异常才处理
                    if process_running and gateway_healthy:
                        log("Gateway 恢复（可能是手动重启）")
                        gateway_was_healthy = True
                    else:
                        gateway_was_healthy = False
                    
                    if should_alert("gateway_down"):
                        notify(
                            "Gateway 异常",
                            f"进程: {'运行中' if process_running else '未运行'}\nHTTP: {'正常' if gateway_healthy else '无响应'}",
                            "error"
                        )
                    
                    # 尝试重启
                    if CONFIG.get("AUTO_RESTART", True):
                        if restart_gateway():
                            if should_alert("gateway_restarted"):
                                notify("Gateway 已重启", "自动重启成功", "success")
                        else:
                            # 第二级保护：重启失败，尝试回滚到稳定版本
                            log("重启失败，尝试回滚到稳定版本...")
                            if rollback_to_last_good():
                                time.sleep(3)
                                if restart_gateway():
                                    if should_alert("rollback_success"):
                                        notify("回滚成功", "已回滚配置并重启 Gateway", "success")
                                    record_change_log("recover", "自动回滚并重启成功", {})
                                else:
                                    # 第三级保护：回滚后仍失败，通知人工处理
                                    if should_alert("rollback_failed"):
                                        notify("回滚失败", "需要人工介入", "error")
                            else:
                                if should_alert("rollback_failed"):
                                    notify("回滚失败", "没有可用的稳定版本", "error")
                
                else:
                    log("Gateway 持续异常")
            
            # 性能告警
            if metrics["cpu"] > CONFIG.get("CPU_THRESHOLD", 90):
                if should_alert("high_cpu"):
                    notify("CPU 过高", f"当前使用率: {metrics['cpu']}%", "warning")
            
            mem_percent = (metrics["mem_used"] / metrics["mem_total"] * 100) if metrics["mem_total"] > 0 else 0
            if mem_percent > CONFIG.get("MEMORY_THRESHOLD", 85):
                if should_alert("high_memory"):
                    notify("内存过高", f"已使用: {metrics['mem_used']}G / {metrics['mem_total']}G ({mem_percent:.0f}%)", "warning")
            
            # 慢会话检测
            slow_sessions = analyze_slow_sessions()
            if slow_sessions and should_alert("slow_session"):
                latest = slow_sessions[-1]
                notify(
                    "慢响应会话",
                    f"响应时间: {latest['duration']}秒\n原因: {latest['reason']}",
                    "warning"
                )

            scan_pipeline_progress_events()
            scan_runtime_anomalies()
            
            # 自动更新检查（每小时）
            if now - last_check_time > 3600:
                last_check_time = now
                do_auto_update()
            
            # 保存告警状态
            save_alerts()

            STORE.record_health_sample(
                process_running=process_running,
                gateway_healthy=gateway_healthy,
                cpu=metrics["cpu"],
                mem_used=metrics["mem_used"],
                mem_total=metrics["mem_total"],
            )
            
            # 定期日志
            log(f"检查: 进程={'✓' if process_running else '✗'} HTTP={'✓' if gateway_healthy else '✗'} CPU={metrics['cpu']}% 内存={metrics['mem_used']}G")
            
            time.sleep(CONFIG.get("CHECK_INTERVAL", 30))
            
        except KeyboardInterrupt:
            log("收到退出信号，正在停止...")
            notify("Guardian 停止", "守护进程已停止", "info")
            break
        except Exception as e:
            log(f"监控循环异常: {e}", "ERROR")
            time.sleep(10)


if __name__ == "__main__":
    main()
