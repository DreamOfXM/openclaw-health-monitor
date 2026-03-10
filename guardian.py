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
import resource
import hashlib
import shlex
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List

from monitor_config import DEFAULT_CONFIG, load_config as load_shared_config
from snapshot_manager import SnapshotManager
from state_store import MonitorStateStore
from task_contracts import infer_task_contract, load_task_contract_catalog

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
OFFICIAL_MANAGER = BASE_DIR / "manage_official_openclaw.sh"
DESKTOP_RUNTIME = BASE_DIR / "desktop_runtime.sh"

CONFIG = {}
ALERTS = {}
VERSIONS = {"current": None, "history": []}
STORE = MonitorStateStore(BASE_DIR)
SNAPSHOTS = SnapshotManager(BASE_DIR, OPENCLAW_HOME)


def raise_nofile_limit(target: int = 65536) -> None:
    """Best-effort bump of RLIMIT_NOFILE for long-running local agents."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = min(max(soft, target), hard)
        if desired > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except Exception:
        pass


def load_config():
    """加载配置文件"""
    global CONFIG
    CONFIG = load_shared_config(BASE_DIR)


def active_env_id() -> str:
    env_id = str(CONFIG.get("ACTIVE_OPENCLAW_ENV", "primary")).strip() or "primary"
    return env_id if env_id in {"primary", "official"} else "primary"


def current_env_spec() -> dict[str, Any]:
    env_id = active_env_id()
    if env_id == "official":
        return {
            "id": "official",
            "home": Path(str(CONFIG.get("OPENCLAW_OFFICIAL_STATE", str(Path.home() / ".openclaw-official")))),
            "code": Path(str(CONFIG.get("OPENCLAW_OFFICIAL_CODE", str(Path.home() / "openclaw-workspace" / "openclaw-official")))),
            "port": int(CONFIG.get("OPENCLAW_OFFICIAL_PORT", 19001)),
        }
    return {
        "id": "primary",
        "home": Path(str(CONFIG.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")))),
        "code": Path(str(CONFIG.get("OPENCLAW_CODE", str(Path.home() / "openclaw-workspace" / "openclaw")))),
        "port": int(CONFIG.get("GATEWAY_PORT", 18789)),
    }


def snapshot_targets() -> list[tuple[str, SnapshotManager]]:
    primary_home = Path(str(CONFIG.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))))
    official_home = Path(str(CONFIG.get("OPENCLAW_OFFICIAL_STATE", str(Path.home() / ".openclaw-official"))))
    return [
        ("primary", SnapshotManager(BASE_DIR, primary_home)),
        ("official", SnapshotManager(BASE_DIR, official_home)),
    ]


def current_gateway_log() -> Path:
    return current_env_spec()["home"] / "logs" / "gateway.log"


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


def run_args(
    args: list[str], timeout: int = 30, env: Optional[Dict[str, str]] = None
) -> tuple[int, str, str]:
    """Run a subprocess without going through a shell."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def openclaw_runtime_env() -> Dict[str, str]:
    """Build a subprocess env pinned to the active OpenClaw environment."""
    spec = current_env_spec()
    env = os.environ.copy()
    env["OPENCLAW_STATE_DIR"] = str(spec["home"])
    env["OPENCLAW_CONFIG_PATH"] = str(spec["home"] / "openclaw.json")
    env["OPENCLAW_GATEWAY_PORT"] = str(spec["port"])
    return env


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
    spec = current_env_spec()
    if spec["id"] == "official":
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{spec['port']}/health")
            with urllib.request.urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
            return bool(payload.get("ok"))
        except Exception:
            return False

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
    return get_listener_pid(int(current_env_spec()["port"])) is not None


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
    return current_gateway_log()


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
    sanitized = line
    lower_line = line.lower()
    if '"feishu[default] dm from ' in lower_line or '"feishu[default]:' in lower_line:
        if "Feishu[default]" in line and ": " in line:
            sanitized = line.split(": ", 1)[1]
            sanitized = sanitized.split('","_meta"', 1)[0]
            sanitized = sanitized.split('"}', 1)[0]
            sanitized = sanitized.strip()
            if sanitized:
                return sanitized[:80]
    lower = line.lower()
    ignore = (
        "dispatching to agent",
        "dispatch complete",
        "pipeline_progress:",
        "pipeline_receipt:",
        "announce_skip",
        "guardian_followup",
        "guardian_escalation",
    )
    if any(marker in lower for marker in ignore):
        return None
    sanitized_lower = sanitized.lower()
    if " dm from " in sanitized_lower and ": " in sanitized:
        idx = sanitized.find(": ")
        if idx > 0:
            return sanitized[idx + 2 :].strip()[:80]
    if "message in" in sanitized_lower and ": " in sanitized:
        idx = sanitized.find(": ")
        if idx > 0:
            return sanitized[idx + 2 :].strip()[:80]
    if "feishu[default]:" in sanitized_lower and ": " in sanitized:
        idx = sanitized.find(": ")
        if idx > 0:
            return sanitized[idx + 2 :].strip()[:80]
    return None


def normalize_task_question(text: str | None) -> str:
    """Normalize runtime question text into a user-visible task title."""
    raw = (text or "").strip()
    if not raw:
        return "未知任务"
    lower = raw.lower()
    if "dispatching to agent" in lower or "dispatch complete" in lower:
        return "未知任务"
    if "received message from " in lower:
        return "未知任务"
    if "feishu[default] dm from " in lower and ": " in raw:
        raw = raw.split(": ", 1)[1].strip()
    raw = raw.split('","_meta"', 1)[0].strip()
    return raw[:120] if raw else "未知任务"


def extract_pipeline_marker(line: str) -> str | None:
    """Extract a pipeline progress marker from the runtime logs."""
    marker = "PIPELINE_PROGRESS:"
    if marker not in line:
        return None
    return line.split(marker, 1)[1].strip()[:120]


def extract_pipeline_receipt(line: str) -> dict[str, str] | None:
    """Extract a structured PIPELINE_RECEIPT payload from runtime logs."""
    marker = "PIPELINE_RECEIPT:"
    if marker not in line:
        return None
    payload = line.split(marker, 1)[1].strip()
    receipt: dict[str, str] = {}
    for part in payload.split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        receipt[key.strip()] = value.strip()
    if not receipt.get("agent"):
        return None
    return receipt


def is_visible_completion_message(line: str) -> bool:
    """Heuristically detect a user-visible completion reply in runtime logs."""
    text = line.strip()
    if not text:
        return False

    lower = text.lower()
    ignore_markers = (
        "dispatching to agent",
        "dispatch complete",
        "pipeline_progress:",
        "[gateway]",
        "[feishu]",
        "[plugins]",
        "[ws]",
        "guardIAN_followup".lower(),
        "guardian_escalation",
        "received message from",
        "dm from ",
        "message in ",
        "signal sigterm",
        "gateway closed",
        "error:",
        "config warnings",
    )
    if any(marker in lower for marker in ignore_markers):
        return False

    completion_markers = (
        "任务已完成",
        "已完成：",
        "已全部完成",
        "全部配置完成",
        "当前状态： 已结束",
        "当前状态：已结束",
        "无需后续操作",
        "系统已就绪",
        "以后每天都会自动推送",
        "可以在飞书群里查看",
    )
    return any(marker in text for marker in completion_markers)


def extract_requester_open_id(line: str) -> str | None:
    """Extract Feishu requester open_id from a dispatch session key."""
    marker = "session=agent:main:feishu:direct:"
    if marker not in line:
        return None
    tail = line.split(marker, 1)[1]
    open_id = tail.split(")", 1)[0].strip()
    return open_id or None


def extract_runtime_session_key(line: str) -> str | None:
    """Extract the session key from a runtime dispatch line when present."""
    marker = "session="
    if marker not in line:
        return None
    tail = line.split(marker, 1)[1]
    for sep in (")", " ", "\n"):
        tail = tail.split(sep, 1)[0]
    session_key = tail.strip()
    return session_key or None


def normalize_stage_label(marker: str) -> str:
    """Convert a pipeline marker into a generic human-readable stage label."""
    text = (marker or "").strip()
    if not text:
        return "处理中"
    if "BLOCKED" in text:
        return "当前阶段阻塞"
    if "RUNNING" in text:
        return "当前阶段运行中"
    if "ANALYZING" in text:
        return "当前阶段分析中"
    if "IMPLEMENTING" in text:
        return "当前阶段处理中"
    if "->" in text:
        left, right = [part.strip() for part in text.split("->", 1)]
        return f"阶段切换: {left} -> {right}"
    return text.replace("_", " ")


def classify_guardian_followup_error(output: str) -> str:
    """Classify follow-up failures into coarse blocking reasons."""
    text = (output or "").lower()
    if "session file locked" in text or ".jsonl.lock" in text:
        return "session_lock"
    if "oauth token refresh failed" in text or "re-authenticate" in text or "(auth)" in text:
        return "model_auth"
    if "model_not_found" in text or "404 page not found" in text:
        return "model_unavailable"
    if "all models failed" in text:
        return "model_pool_failed"
    if "timeout" in text:
        return "timeout"
    return "unknown"


def blocked_reason_label(reason: str) -> str:
    """Render a user-facing blocked reason label."""
    mapping = {
        "session_lock": "会话资源被占用",
        "model_auth": "模型认证失效",
        "model_unavailable": "模型不可用",
        "model_pool_failed": "模型链路不可用",
        "timeout": "会话响应超时",
        "unknown": "内部执行异常",
    }
    return mapping.get(reason, "内部执行异常")


def format_duration_label(seconds: int) -> str:
    """Render durations in a human-friendly label for push notifications."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        minutes = total // 60
        remain = total % 60
        return f"{minutes}分钟" if remain == 0 else f"{minutes}分{remain}秒"
    hours = total // 3600
    remain = total % 3600
    minutes = remain // 60
    return f"{hours}小时" if minutes == 0 else f"{hours}小时{minutes}分钟"


def build_control_plane_followup(
    task: dict[str, Any],
    control: dict[str, Any],
    *,
    idle: int,
    total: int,
) -> str:
    """Build a task-contract-aware guardian control message."""
    question = str(task.get("question") or task.get("last_user_message") or "未知任务")
    stage = str(task.get("current_stage") or "处理中")
    control_state = str(control.get("control_state") or "unknown")
    next_action = str(control.get("next_action") or "require_receipt_or_block")
    next_actor = str(control.get("next_actor") or "guardian")
    claim_level = str(control.get("claim_level") or "received_only")
    contract = control.get("contract") or {}
    contract_id = str(contract.get("id") or "single_agent")
    missing = ", ".join(control.get("missing_receipts") or []) or "none"

    action_instruction = {
        "require_pm_receipt": "若产品阶段已开始，请补发 PIPELINE_RECEIPT: agent=pm | phase=planning | action=started/completed；若未开始，请明确阻塞原因。",
        "await_pm_receipt": "若方案仍在整理，请补发 pm 的 started/completed 回执；若无法继续，请明确阻塞原因。",
        "require_dev_receipt": "若方案已完成，请立即按官方 sessions_spawn(agentId='dev') 继续，并补发 dev started 回执；若开发无法启动，请明确阻塞原因。",
        "await_dev_receipt": "若开发已开始，请补发 dev completed/blocked 回执，不要只发口头进度。",
        "require_test_receipt": "若开发已完成，请立即按官方 sessions_spawn(agentId='test') 继续，并补发 test started 回执；若测试无法启动，请明确阻塞原因。",
        "await_test_receipt": "若测试已开始，请补发 test completed/blocked 回执，不要只发口头进度。",
        "require_calculator_start": "若这是量化/精算任务，请先启动 calculator，并补发 calculator started 回执；若未启动，请明确阻塞原因。",
        "await_calculator_receipt": "若 calculator 已开始，请补发 calculator completed/blocked 回执，不要只发口头进度。",
        "require_verifier_receipt": "若精算结果已得出，请继续 verifier，并补发 verifier completed 回执；若无法复核，请明确阻塞原因。",
        "manual_or_session_recovery": "当前任务需要人工恢复或重新发起，请不要再口头宣称链路正在推进。",
        "require_receipt_or_block": "请立即补发结构化 PIPELINE_RECEIPT；若链路未真正继续，请明确阻塞原因。",
    }.get(next_action, "请立即补发结构化 PIPELINE_RECEIPT；若链路未真正继续，请明确阻塞原因。")

    return (
        "GUARDIAN_TASK_CONTROL: 这不是用户新需求。"
        f"当前 task_id={task['task_id']}，任务合同={contract_id}，控制状态={control_state}，"
        f"对外可宣称级别={claim_level}，下一执行责任人={next_actor}，"
        f"当前阶段={stage}，已静默={format_duration_label(idle)}，累计运行={format_duration_label(total)}，"
        f"缺失回执={missing}。当前问题={question}。"
        f"{action_instruction}"
        "不要再口头宣称团队正在推进，也不要把这条控制消息当成新的用户需求。"
    )


def build_task_id(session_key: str, timestamp: str) -> str:
    raw = f"{session_key}|{timestamp}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def infer_task_channel(session_key: str) -> str:
    if ":feishu:direct:" in session_key:
        return "feishu_dm"
    if ":feishu:group:" in session_key:
        return "feishu_group"
    return "unknown"


def valid_task_question(text: str | None) -> bool:
    candidate = normalize_task_question(text)
    if not candidate or candidate == "未知任务":
        return False
    return True


def write_task_registry_snapshot() -> None:
    """Persist a compact task-registry summary for external consumers."""
    if not CONFIG.get("ENABLE_TASK_REGISTRY", True):
        return
    env_id = current_env_spec()["id"]
    current = STORE.get_current_task(env_id=env_id)
    tasks = STORE.list_tasks(limit=int(CONFIG.get("TASK_REGISTRY_RETENTION", 100)))
    filtered = [task for task in tasks if task.get("env_id") == env_id]

    def enrich_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not task:
            return None
        question = normalize_task_question(task.get("question"))
        last_user_message = normalize_task_question(task.get("last_user_message"))
        if question == "未知任务":
            question = STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
        if last_user_message == "未知任务":
            last_user_message = STORE.get_task_question_candidate(task["task_id"]) or "未知任务"
        control = STORE.derive_task_control_state(task["task_id"])
        return {
            **task,
            "question": question,
            "last_user_message": last_user_message,
            "control": control,
            "contract": control.get("contract") or {},
        }

    current_payload = enrich_task(current)
    tasks_payload = [task for task in (enrich_task(item) for item in filtered[:20]) if task]
    facts_current = current_payload or (tasks_payload[0] if tasks_payload else None)
    session_resolution = (
        STORE.derive_session_resolution(str((facts_current or {}).get("session_key") or ""))
        if facts_current and facts_current.get("session_key")
        else None
    )
    payload = {
        "generated_at": int(time.time()),
        "env_id": env_id,
        "summary": STORE.summarize_tasks(env_id=env_id),
        "current": current_payload,
        "tasks": tasks_payload,
        "session_resolution": session_resolution,
    }
    facts_payload = {
        "generated_at": payload["generated_at"],
        "env_id": env_id,
        "current_task": {
            "task_id": facts_current.get("task_id") if facts_current else None,
            "question": facts_current.get("question") if facts_current else None,
            "status": facts_current.get("status") if facts_current else None,
            "current_stage": facts_current.get("current_stage") if facts_current else None,
            "approved_summary": (facts_current or {}).get("control", {}).get("approved_summary"),
            "evidence_level": (facts_current or {}).get("control", {}).get("evidence_level"),
            "evidence_summary": (facts_current or {}).get("control", {}).get("evidence_summary"),
            "control_state": (facts_current or {}).get("control", {}).get("control_state"),
            "next_action": (facts_current or {}).get("control", {}).get("next_action"),
            "next_actor": (facts_current or {}).get("control", {}).get("next_actor"),
            "action_reason": (facts_current or {}).get("control", {}).get("action_reason"),
            "claim_level": (facts_current or {}).get("control", {}).get("claim_level"),
            "protocol": (facts_current or {}).get("control", {}).get("protocol") or {},
            "contract_id": ((facts_current or {}).get("control", {}).get("contract") or {}).get("id"),
            "missing_receipts": (facts_current or {}).get("control", {}).get("missing_receipts") or [],
            "control_action": (facts_current or {}).get("control", {}).get("control_action"),
            "phase_statuses": (facts_current or {}).get("control", {}).get("phase_statuses") or [],
        },
        "session_resolution": session_resolution,
    }
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "task-registry-summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (data_dir / "current-task-facts.json").write_text(
        json.dumps(facts_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    shared_dir = data_dir / "shared-state"
    shared_dir.mkdir(parents=True, exist_ok=True)
    control_plane = STORE.summarize_control_plane(env_id=env_id)
    try:
        gateway_running = check_process_running()
    except Exception:
        gateway_running = False
    try:
        gateway_healthy = check_gateway_health()
    except Exception:
        gateway_healthy = False
    runtime_health = {
        "generated_at": int(time.time()),
        "env_id": env_id,
        "metrics": get_system_metrics(),
        "gateway_running": gateway_running,
        "gateway_healthy": gateway_healthy,
    }
    learning_backlog = {
        "generated_at": int(time.time()),
        "summary": STORE.summarize_learnings(),
        "learnings": STORE.list_learnings(statuses=["pending", "reviewed", "promoted"], limit=50),
        "reflections": STORE.list_reflection_runs(limit=20),
    }
    (shared_dir / "task-registry-snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "control-action-queue.json").write_text(
        json.dumps(payload.get("control_queue") or [], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "runtime-health.json").write_text(
        json.dumps(runtime_health, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "learning-backlog.json").write_text(
        json.dumps(learning_backlog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (shared_dir / "control-plane-summary.json").write_text(
        json.dumps(control_plane, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    learnings_dir = BASE_DIR / ".learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    learnings = STORE.list_learnings(limit=200)
    errors_lines = [f"- {item.get('title')}: {item.get('detail')}" for item in learnings if str(item.get("status")) != "promoted"]
    promoted_lines = [f"- {item.get('title')}: {item.get('detail')}" for item in learnings if str(item.get("status")) == "promoted"]
    feature_lines = [f"- {item.get('title')}: {item.get('detail')}" for item in learnings if str(item.get("category")) == "feature_request"]
    (learnings_dir / "ERRORS.md").write_text("# Errors\n\n" + ("\n".join(errors_lines) if errors_lines else "- 暂无待处理错误模式\n"), encoding="utf-8")
    (learnings_dir / "LEARNINGS.md").write_text("# Learnings\n\n" + ("\n".join(promoted_lines or errors_lines[:20]) if (promoted_lines or errors_lines) else "- 暂无学习记录\n"), encoding="utf-8")
    (learnings_dir / "FEATURE_REQUESTS.md").write_text("# Feature Requests\n\n" + ("\n".join(feature_lines) if feature_lines else "- 暂无 feature requests\n"), encoding="utf-8")
    memory_dir = BASE_DIR / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    memory_body = "# Daily Memory\n\n" + json.dumps({"reflection_runs": learning_backlog["reflections"][:5], "summary": learning_backlog["summary"]}, ensure_ascii=False, indent=2)
    (memory_dir / f"{today}.md").write_text(memory_body + "\n", encoding="utf-8")
    (BASE_DIR / "MEMORY.md").write_text(
        "# Monitor Memory\n\n"
        f"- env: {env_id}\n"
        f"- current_task: {(facts_payload.get('current_task') or {}).get('task_id') or '-'}\n"
        f"- learning_total: {learning_backlog['summary'].get('total', 0)}\n"
        f"- promoted: {learning_backlog['summary'].get('promoted', 0)}\n",
        encoding="utf-8",
    )


def derive_learning_key(*parts: str) -> str:
    joined = "|".join(part.strip() for part in parts if part is not None)
    return hashlib.sha1(joined.encode("utf-8", errors="ignore")).hexdigest()[:24]


def capture_control_plane_learnings(outcomes: list[dict]) -> list[dict]:
    """Convert repeated blocked/follow-up outcomes into pending learnings."""
    if not CONFIG.get("ENABLE_EVOLUTION_PLANE", True):
        return []
    env_id = current_env_spec()["id"]
    captured: list[dict] = []
    for outcome in outcomes:
        task_id = str(outcome.get("task_id") or "")
        if not task_id:
            continue
        action = str(outcome.get("action") or "")
        if action not in {"blocked", "followup_sent"}:
            continue
        blocked_reason = str(outcome.get("blocked_reason") or "")
        control_state = str(outcome.get("control_state") or "")
        task = STORE.get_task(task_id) or {}
        title = "任务控制面发现可改进项"
        detail = f"task={task_id} control={control_state} action={action} blocked_reason={blocked_reason or '-'}"
        if action == "blocked":
            title = "任务因缺少结构化回执而阻塞"
        learning_key = derive_learning_key("control", env_id, control_state, blocked_reason or action)
        learning = STORE.upsert_learning(
            learning_key=learning_key,
            env_id=env_id,
            task_id=task_id,
            category="control_plane",
            title=title,
            detail=detail,
            evidence={
                "task_id": task_id,
                "question": task.get("question") or task.get("last_user_message") or "未知任务",
                "control_state": control_state,
                "blocked_reason": blocked_reason,
                "action": action,
            },
        )
        captured.append(learning)
    return captured


def run_reflection_cycle(force: bool = False) -> dict[str, Any]:
    """Promote repeated learnings into reviewed/promoted hypotheses."""
    if not CONFIG.get("ENABLE_EVOLUTION_PLANE", True):
        return {"status": "disabled", "promoted": 0, "reviewed": 0}
    now = int(time.time())
    interval = int(CONFIG.get("REFLECTION_INTERVAL_SECONDS", 3600))
    last_run = int(STORE.load_runtime_value("reflection_last_run_at", 0) or 0)
    if not force and last_run and now - last_run < interval:
        return {"status": "skipped", "promoted": 0, "reviewed": 0}

    threshold = max(2, int(CONFIG.get("LEARNING_PROMOTION_THRESHOLD", 3)))
    learnings = STORE.list_learnings(limit=100)
    promoted = 0
    reviewed = 0
    for learning in learnings:
        if learning.get("status") == "promoted":
            continue
        reviewed += 1
        next_status = "reviewed"
        promoted_target = ""
        if int(learning.get("occurrences") or 0) >= threshold:
            next_status = "promoted"
            category = str(learning.get("category") or "")
            promoted_target = "contract" if category == "control_plane" else "rule"
            promoted += 1
        STORE.upsert_learning(
            learning_key=str(learning.get("learning_key") or ""),
            env_id=str(learning.get("env_id") or current_env_spec()["id"]),
            task_id=str(learning.get("task_id") or ""),
            category=str(learning.get("category") or "misc"),
            title=str(learning.get("title") or ""),
            detail=str(learning.get("detail") or ""),
            evidence=learning.get("evidence") or {},
            status=next_status,
            promoted_target=promoted_target,
        )
    summary = {
        "status": "ok",
        "reviewed": reviewed,
        "promoted": promoted,
        "threshold": threshold,
        "generated_at": now,
    }
    STORE.record_reflection_run("scheduled", summary)
    STORE.save_runtime_value("reflection_last_run_at", now)
    return summary


def sync_runtime_task_registry(lines: list[str]) -> None:
    """Project runtime log activity into a persistent task registry."""
    if not CONFIG.get("ENABLE_TASK_REGISTRY", True):
        return

    env_id = current_env_spec()["id"]
    contract_catalog = load_task_contract_catalog(BASE_DIR, str(CONFIG.get("TASK_CONTRACTS_FILE", "") or ""))
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: dict[str, dict[str, Any]] = {}
    touched_task_ids: set[str] = set()

    def reconcile_task(task_id: str) -> None:
        task = STORE.get_task(task_id)
        if not task:
            return
        control = STORE.derive_task_control_state(task_id)
        STORE.reconcile_task_control_action(task, control)

    def most_recent_key() -> str | None:
        if not open_dispatches:
            return None
        return max(open_dispatches.items(), key=lambda item: item[1]["started_at"])[0]

    for line in lines:
        ts_raw, ts = parse_runtime_timestamp(line)
        if ts is None:
            continue

        question = normalize_task_question(extract_runtime_question(line))
        if question and question != "未知任务":
            question_candidates.append((int(ts), question))
            question_candidates = question_candidates[-50:]

        lower = line.lower()
        if "dispatching to agent" in lower:
            nearest_question = ""
            for qts, qmsg in reversed(question_candidates):
                if abs(int(ts) - qts) <= 15:
                    nearest_question = qmsg
                    break
            requester_open_id = extract_requester_open_id(line)
            session_key = extract_runtime_session_key(line) or requester_open_id or f"dispatch:{ts_raw}"
            existing = STORE.get_latest_task_for_session(session_key)
            question_text = normalize_task_question(nearest_question)
            if question_text == "未知任务" and existing:
                question_text = normalize_task_question(
                    existing.get("last_user_message") or existing.get("question", "")
                )
            existing_contract = STORE.get_task_contract(existing["task_id"]) if existing else None
            contract = infer_task_contract(
                question_text,
                catalog=contract_catalog,
                existing_contract_id=(existing_contract or {}).get("id"),
            )
            task_id = build_task_id(session_key, ts_raw)
            current_stage = "处理中"
            task = {
                "task_id": task_id,
                "session_key": session_key,
                "env_id": env_id,
                "channel": infer_task_channel(session_key),
                "status": "running",
                "current_stage": current_stage,
                "question": question_text,
                "last_user_message": question_text,
                "started_at": int(ts),
                "last_progress_at": int(ts),
                "created_at": int(ts),
                "updated_at": int(ts),
                "latest_receipt": {},
            }
            if int(CONFIG.get("TASK_REGISTRY_MAX_ACTIVE", 1)) <= 1:
                STORE.background_other_tasks_for_session(session_key, task_id)
            STORE.upsert_task(task)
            STORE.upsert_task_contract(task_id, contract)
            touched_task_ids.add(task_id)
            STORE.record_task_event(
                task_id,
                "dispatch_started",
                {
                    "session_key": session_key,
                    "question": question_text,
                    "channel": task["channel"],
                    "timestamp": ts_raw,
                    "env_id": env_id,
                    "contract_id": contract.get("id"),
                },
            )
            STORE.record_task_event(
                task_id,
                "contract_assigned",
                {
                    "contract": contract,
                    "timestamp": ts_raw,
                },
            )
            reconcile_task(task_id)
            open_dispatches[session_key] = {
                **task,
                "timestamp": ts_raw,
                "requester_open_id": requester_open_id or "",
                "marker": "",
                "contract": contract,
            }
            continue

        marker = extract_pipeline_marker(line)
        if marker and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            dispatch["marker"] = marker
            dispatch["current_stage"] = normalize_stage_label(marker)
            dispatch["last_progress_at"] = int(ts)
            dispatch["updated_at"] = int(ts)
            STORE.update_task_fields(
                dispatch["task_id"],
                current_stage=dispatch["current_stage"],
                last_progress_at=int(ts),
                updated_at=int(ts),
            )
            touched_task_ids.add(dispatch["task_id"])
            STORE.record_task_event(
                dispatch["task_id"],
                "stage_progress",
                {
                    "marker": marker,
                    "stage": dispatch["current_stage"],
                    "timestamp": ts_raw,
                },
            )
            reconcile_task(dispatch["task_id"])
            continue

        receipt = extract_pipeline_receipt(line)
        if receipt and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            action = receipt.get("action", "")
            phase = receipt.get("phase", "")
            stage_label = f"{phase}:{action}".strip(":")
            status = dispatch.get("status", "running")
            blocked_reason = ""
            if action == "blocked":
                status = "blocked"
                blocked_reason = receipt.get("evidence", "")
            elif action == "completed":
                status = "running"
            dispatch["current_stage"] = stage_label or dispatch.get("current_stage", "处理中")
            dispatch["last_progress_at"] = int(ts)
            dispatch["updated_at"] = int(ts)
            dispatch["status"] = status
            STORE.update_task_fields(
                dispatch["task_id"],
                status=status,
                current_stage=dispatch["current_stage"],
                last_progress_at=int(ts),
                updated_at=int(ts),
                blocked_reason=blocked_reason,
                latest_receipt=receipt,
            )
            touched_task_ids.add(dispatch["task_id"])
            STORE.record_task_event(
                dispatch["task_id"],
                "pipeline_receipt",
                {
                    "receipt": receipt,
                    "status": status,
                    "stage": dispatch["current_stage"],
                    "timestamp": ts_raw,
                },
            )
            reconcile_task(dispatch["task_id"])
            continue

        if is_visible_completion_message(line) and open_dispatches:
            current_key = most_recent_key()
            if current_key:
                dispatch = open_dispatches.pop(current_key)
                STORE.update_task_fields(
                    dispatch["task_id"],
                    status="completed",
                    current_stage="已完成",
                    updated_at=int(ts),
                    completed_at=int(ts),
                )
                touched_task_ids.add(dispatch["task_id"])
                STORE.record_task_event(
                    dispatch["task_id"],
                    "visible_completion",
                    {"timestamp": ts_raw, "message": line.strip()},
                )
                reconcile_task(dispatch["task_id"])
            continue

        if "dispatch complete" in lower and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches.pop(current_key)
            status = "completed"
            stage = "已完成"
            if "queuedfinal=false" in lower or "replies=0" in lower:
                status = "no_reply"
                stage = "完成但无可见回复"
            STORE.update_task_fields(
                dispatch["task_id"],
                status=status,
                current_stage=stage,
                updated_at=int(ts),
                completed_at=int(ts),
            )
            touched_task_ids.add(dispatch["task_id"])
            STORE.record_task_event(
                dispatch["task_id"],
                "dispatch_complete",
                {
                    "timestamp": ts_raw,
                    "status": status,
                    "stage": stage,
                    "line": line.strip(),
                },
            )
            reconcile_task(dispatch["task_id"])
    for task_id in touched_task_ids:
        STORE.repair_task_identity(task_id)
        reconcile_task(task_id)
    write_task_registry_snapshot()



def lookup_openclaw_session_id(session_key: str) -> str | None:
    """Resolve a sessionKey to sessionId using the local OpenClaw session store."""
    if not session_key:
        return None
    cache = STORE.load_runtime_value("openclaw_session_cache", {})
    cached = cache.get(session_key)
    if cached:
        return cached

    code, stdout, stderr = run_args(
        ["openclaw", "sessions", "--json", "--all-agents", "--active", "10080"],
        timeout=20,
    )
    if code != 0 or not stdout.strip():
        log(f"读取 OpenClaw 会话失败: {stderr or 'unknown error'}", "ERROR")
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        log("解析 OpenClaw 会话列表失败", "ERROR")
        return None

    for item in payload.get("sessions", []):
        key = item.get("key")
        session_id = item.get("sessionId")
        if key and session_id:
            cache[key] = session_id

    if cache:
        STORE.save_runtime_value("openclaw_session_cache", trim_runtime_seen(cache, keep=500))
    return cache.get(session_key)


def send_guardian_followup(
    session_key: str, message: str, *, deliver: bool = True
) -> tuple[bool, str | None]:
    """Send a marked system follow-up into an existing OpenClaw session."""
    session_id = lookup_openclaw_session_id(session_key)
    if not session_id:
        log(f"守护追问失败，未找到会话: {session_key}", "ERROR")
        return False, "unknown"

    args = [
        "openclaw",
        "agent",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
        "--timeout",
        str(int(CONFIG.get("GUARDIAN_FOLLOWUP_TIMEOUT", 120))),
    ]
    if deliver:
        args.append("--deliver")

    timeout = int(CONFIG.get("GUARDIAN_FOLLOWUP_TIMEOUT", 120)) + 30
    code, stdout, stderr = run_args(args, timeout=timeout, env=openclaw_runtime_env())
    if code == 0:
        log(f"守护追问已发送到会话 {session_key}: {message}")
        return True, None
    error_text = stderr or stdout or "unknown error"
    log(
        f"守护追问失败({session_key} -> {session_id}): "
        f"{error_text}",
        "ERROR",
    )
    return False, classify_guardian_followup_error(error_text)


def send_feishu_progress_push(open_id: str, message: str) -> bool:
    """Push a proactive progress message back to the user's Feishu DM."""
    if not open_id:
        return False
    target = open_id if ":" in open_id else f"user:{open_id}"
    quoted_target = json.dumps(target, ensure_ascii=False)
    quoted_message = json.dumps(message, ensure_ascii=False)
    code, _, stderr = run_cmd(
        f"openclaw message send --channel feishu --target {quoted_target} --message {quoted_message}"
    )
    if code == 0:
        log(f"进度推送已发送到 {target}: {message}")
        return True
    log(f"进度推送失败({target}): {stderr or 'unknown error'}", "ERROR")
    return False


def deliver_guardian_progress_update(
    dispatch: dict[str, Any],
    *,
    followup_message: str,
    fallback_message: str,
) -> tuple[str | None, str | None]:
    """Deliver progress updates with retry, then fall back to direct Feishu push."""
    session_key = dispatch.get("session_key") or ""
    open_id = dispatch.get("requester_open_id") or ""
    retries = max(1, int(CONFIG.get("GUARDIAN_FOLLOWUP_RETRIES", 2)))
    retry_delay = max(0, int(CONFIG.get("GUARDIAN_FOLLOWUP_RETRY_DELAY", 3)))
    blocked_reason: str | None = None

    if session_key:
        for attempt in range(1, retries + 1):
            ok, error_kind = send_guardian_followup(session_key, followup_message)
            if ok:
                return "session", None
            blocked_reason = error_kind or blocked_reason
            if blocked_reason in {"session_lock", "model_auth", "model_unavailable", "model_pool_failed"}:
                break
            if attempt < retries and retry_delay:
                time.sleep(retry_delay)
        log(
            f"守护追问连续失败，降级为直接消息推送: {session_key}",
            "WARNING",
        )

    if open_id and send_feishu_progress_push(open_id, fallback_message):
        return "feishu", blocked_reason
    return None, blocked_reason


def trim_runtime_seen(seen: dict[str, int], keep: int = 2000) -> dict[str, int]:
    """Bound the anomaly dedupe table so it cannot grow forever."""
    if len(seen) <= keep:
        return seen
    newest = sorted(seen.items(), key=lambda item: item[1], reverse=True)[:keep]
    return dict(newest)


def trim_runtime_state_map(state: dict[str, dict[str, Any]], keep: int = 500) -> dict[str, dict[str, Any]]:
    """Bound runtime state maps keyed by task/session using their freshest timestamp."""
    if len(state) <= keep:
        return state
    newest = sorted(
        state.items(),
        key=lambda item: max(
            int((item[1] or {}).get("last_followup_at", 0)),
            int((item[1] or {}).get("last_stage_push", 0)),
            int((item[1] or {}).get("last_escalation_push", 0)),
            int((item[1] or {}).get("last_blocked_notice", 0)),
        ),
        reverse=True,
    )[:keep]
    return dict(newest)


def collect_open_runtime_dispatches(lines: list[str]) -> list[dict[str, Any]]:
    """Track currently open dispatches and their most recent visible progress."""
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: dict[str, dict[str, Any]] = {}

    def most_recent_key() -> str | None:
        if not open_dispatches:
            return None
        return max(open_dispatches.items(), key=lambda item: item[1]["started_at"])[0]

    for line in lines:
        ts_raw, ts = parse_runtime_timestamp(line)
        if ts is None:
            continue

        question = extract_runtime_question(line)
        if question:
            question_candidates.append((int(ts), question))
            question_candidates = question_candidates[-50:]

        lower = line.lower()
        if "dispatching to agent" in lower:
            nearest_question = ""
            for qts, qmsg in reversed(question_candidates):
                if abs(int(ts) - qts) <= 15:
                    nearest_question = qmsg
                    break
            session_key = (
                extract_runtime_session_key(line)
                or extract_requester_open_id(line)
                or f"dispatch:{ts_raw}"
            )
            open_dispatches[session_key] = {
                "session_key": session_key,
                "started_at": ts,
                "last_progress_at": ts,
                "timestamp": ts_raw,
                "question": nearest_question or "未知任务",
                "marker": "",
                "requester_open_id": extract_requester_open_id(line) or "",
            }
            continue

        marker = extract_pipeline_marker(line)
        if marker and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            dispatch["last_progress_at"] = ts
            dispatch["marker"] = marker
            continue

        if is_visible_completion_message(line) and open_dispatches:
            current_key = most_recent_key()
            if current_key:
                open_dispatches.pop(current_key, None)
            continue

        if "dispatch complete" in lower and open_dispatches:
            current_key = most_recent_key()
            if current_key:
                open_dispatches.pop(current_key, None)

    return sorted(open_dispatches.values(), key=lambda item: item["started_at"])


def collect_runtime_anomalies(
    lines: list[str],
    *,
    now: float,
    slow_threshold: int,
    stalled_threshold: int,
) -> tuple[list[dict[str, Any]], str]:
    """Build anomaly records from recent runtime logs."""
    question_candidates: list[tuple[int, str]] = []
    open_dispatches: dict[str, dict[str, Any]] = {}
    anomalies: list[dict[str, Any]] = []
    latest_signature = ""

    def most_recent_key() -> str | None:
        if not open_dispatches:
            return None
        return max(open_dispatches.items(), key=lambda item: item[1]["started_at"])[0]

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
            requester_open_id = extract_requester_open_id(line)
            session_key = extract_runtime_session_key(line) or requester_open_id or f"dispatch:{ts_raw}"
            open_dispatches[session_key] = {
                "session_key": session_key,
                "started_at": ts,
                "timestamp": ts_raw,
                "question": nearest_question or "未知问题",
                "last_progress_at": ts,
                "marker": "",
                "requester_open_id": requester_open_id or "",
            }
            continue

        marker = extract_pipeline_marker(line)
        if marker and ts is not None and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches[current_key]
            dispatch["last_progress_at"] = ts
            dispatch["marker"] = marker
            continue

        if "dispatch complete" in lower and open_dispatches:
            current_key = most_recent_key()
            if not current_key:
                continue
            dispatch = open_dispatches.pop(current_key)
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

    for dispatch in sorted(open_dispatches.values(), key=lambda item: item["started_at"]):
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


def push_runtime_progress_updates() -> list[dict]:
    """Push updates only when runtime logs show a real silence window."""
    runtime_log = resolve_runtime_gateway_log()
    if not runtime_log.exists():
        return []

    try:
        with open(runtime_log) as handle:
            lines = handle.readlines()[-4000:]
    except Exception as exc:
        log(f"读取运行日志失败: {exc}", "ERROR")
        return []

    now = time.time()
    open_dispatches = collect_open_runtime_dispatches(lines)

    progress_interval = int(CONFIG.get("PROGRESS_PUSH_INTERVAL", 180))
    progress_cooldown = int(CONFIG.get("PROGRESS_PUSH_COOLDOWN", 300))
    escalation_interval = int(CONFIG.get("PROGRESS_ESCALATION_INTERVAL", 600))
    stale_task_max_age = int(CONFIG.get("GUARDIAN_STALE_TASK_MAX_AGE", 3600))
    blocked_cooldown = int(CONFIG.get("GUARDIAN_BLOCKED_COOLDOWN", 900))
    blocked_notice_interval = int(CONFIG.get("GUARDIAN_BLOCKED_NOTICE_INTERVAL", 1800))
    push_state = STORE.load_runtime_value("runtime_progress_push_state", {})
    pushed: list[dict] = []
    active_keys: set[str] = set()

    for dispatch in open_dispatches:
        open_id = dispatch.get("requester_open_id") or ""
        session_key = dispatch.get("session_key") or ""
        if not session_key and not open_id:
            continue

        duration = int(now - dispatch["started_at"])
        idle = int(now - dispatch["last_progress_at"])
        marker = dispatch.get("marker") or ""
        stage_label = normalize_stage_label(marker)
        push_key = session_key or f"{dispatch['timestamp']}:{open_id}"
        active_keys.add(push_key)
        state = push_state.get(push_key, {})
        last_seen_progress_at = int(state.get("last_seen_progress_at", 0))
        current_progress_at = int(dispatch["last_progress_at"])
        last_dispatch_timestamp = str(state.get("last_dispatch_timestamp", ""))
        current_dispatch_timestamp = str(dispatch["timestamp"])
        if current_dispatch_timestamp != last_dispatch_timestamp:
            state["last_dispatch_timestamp"] = current_dispatch_timestamp
            state["last_seen_progress_at"] = current_progress_at
            state["last_stage_push"] = 0
            state["last_escalation_push"] = 0
            state["last_marker"] = marker
            state["stale_suppressed_at"] = 0
        if current_progress_at != last_seen_progress_at:
            state["last_seen_progress_at"] = current_progress_at
            state["last_stage_push"] = 0
            state["last_escalation_push"] = 0
            state["last_marker"] = marker
            state["stale_suppressed_at"] = 0
            state["blocked_reason"] = ""
            state["blocked_until"] = 0

        last_stage_push = int(state.get("last_stage_push", 0))
        last_escalation_push = int(state.get("last_escalation_push", 0))
        stale_suppressed_at = int(state.get("stale_suppressed_at", 0))
        blocked_until = int(state.get("blocked_until", 0))
        blocked_reason = str(state.get("blocked_reason", ""))
        last_blocked_notice = int(state.get("last_blocked_notice", 0))

        if duration >= stale_task_max_age:
            if stale_suppressed_at == 0:
                log(
                    "检测到过时未完成任务，已抑制泛化跟进推送: "
                    f"{session_key or open_id} (idle={format_duration_label(idle)}, total={format_duration_label(duration)})",
                    "INFO",
                )
                record_change_log(
                    "anomaly",
                    "守护系统抑制过时任务跟进",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                    },
                )
                state["stale_suppressed_at"] = int(now)
            if state:
                push_state[push_key] = state
            continue

        if blocked_reason and blocked_until <= int(now) and now - last_blocked_notice >= blocked_notice_interval:
            reason_label = blocked_reason_label(blocked_reason)
            blocked_message = (
                f"任务当前已阻塞。当前阶段：{stage_label}。"
                f"阻塞原因：{reason_label}。"
                f"已静默 {format_duration_label(idle)}，累计运行 {format_duration_label(duration)}。"
                "系统已尝试自动恢复；若后续仍无结果，建议重新发起该任务。"
            )
            if open_id and send_feishu_progress_push(open_id, blocked_message):
                record_change_log(
                    "anomaly",
                    "守护系统阻塞提示",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                        "blocked_reason": blocked_reason,
                        "delivery_channel": "feishu",
                    },
                )
                state["last_blocked_notice"] = int(now)
                state["blocked_until"] = int(now) + blocked_notice_interval
                pushed.append(
                    {
                        "type": "blocked_notice",
                        "open_id": open_id,
                        "duration": duration,
                        "idle": idle,
                        "delivery_channel": "feishu",
                        "blocked_reason": blocked_reason,
                    }
                )
                push_state[push_key] = state
            continue

        if blocked_until > int(now):
            if state:
                push_state[push_key] = state
            continue

        if idle >= progress_interval and now - last_stage_push >= progress_cooldown:
            followup = (
                "GUARDIAN_FOLLOWUP: 这是一条守护系统自动追问，不是用户新需求。"
                "请不要开始新任务，也不要改写用户原始需求。"
                f"请仅基于当前会话同步进展。当前阶段={stage_label}；"
                f"距离上一次可见进展={format_duration_label(idle)}；"
                f"累计运行={format_duration_label(duration)}；"
                f"当前问题={dispatch['question']}。"
                "请优先给用户一句简洁进度说明；若当前任务仍在处理中，只汇报现状，不要重新起新任务。"
            )
            fallback_message = (
                f"任务暂时没有新的可见进展。当前阶段：{stage_label}。"
                f"距离上一次进展已过去 {format_duration_label(idle)}，"
                f"累计运行 {format_duration_label(duration)}，系统会继续自动跟进。"
            )
            channel, failed_reason = deliver_guardian_progress_update(
                dispatch,
                followup_message=followup,
                fallback_message=fallback_message,
            )
            if channel:
                record_change_log(
                    "pipeline",
                    "守护系统主动追问",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or "",
                    },
                )
                state["last_stage_push"] = int(now)
                if failed_reason in {"session_lock", "model_auth", "model_unavailable", "model_pool_failed"}:
                    state["blocked_reason"] = failed_reason
                    state["blocked_until"] = int(now) + blocked_cooldown
                pushed.append(
                    {
                        "type": "progress_push",
                        "open_id": open_id,
                        "duration": duration,
                        "idle": idle,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or "",
                    }
                )

        if idle >= escalation_interval and now - last_escalation_push >= escalation_interval:
            followup = (
                "GUARDIAN_ESCALATION: 这是一条守护系统升级催办，不是用户新需求。"
                "请不要启动新任务。"
                f"当前阶段={stage_label}；静默已持续={format_duration_label(idle)}；"
                f"累计运行={format_duration_label(duration)}；当前问题={dispatch['question']}。"
                "请明确向用户同步：当前是否仍在执行、是否已阻塞、下一步动作是什么。"
            )
            escalation_reason = blocked_reason_label(blocked_reason) if blocked_reason else ""
            fallback_message = (
                (
                    f"任务当前已阻塞。当前阶段：{stage_label}。"
                    f"阻塞原因：{escalation_reason}。"
                    f"已静默 {format_duration_label(idle)}，累计运行 {format_duration_label(duration)}。"
                    "系统会继续跟进，但会先等待阻塞解除。"
                )
                if blocked_reason
                else (
                    f"任务长时间没有新的可见进展。当前阶段：{stage_label}。"
                    f"静默已持续 {format_duration_label(idle)}，"
                    f"累计运行 {format_duration_label(duration)}，系统已将其升级关注并会继续同步。"
                )
            )
            channel, failed_reason = deliver_guardian_progress_update(
                dispatch,
                followup_message=followup,
                fallback_message=fallback_message,
            )
            if channel:
                record_change_log(
                    "anomaly",
                    "守护系统升级催办",
                    {
                        "question": dispatch["question"],
                        "marker": marker or stage_label,
                        "duration": duration,
                        "idle": idle,
                        "timestamp": dispatch["timestamp"],
                        "session_key": session_key,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or blocked_reason or "",
                    },
                )
                state["last_escalation_push"] = int(now)
                if failed_reason in {"session_lock", "model_auth", "model_unavailable", "model_pool_failed"}:
                    state["blocked_reason"] = failed_reason
                    state["blocked_until"] = int(now) + blocked_cooldown
                pushed.append(
                    {
                        "type": "escalation_push",
                        "open_id": open_id,
                        "duration": duration,
                        "idle": idle,
                        "delivery_channel": channel,
                        "blocked_reason": failed_reason or blocked_reason or "",
                    }
                )

        if state:
            push_state[push_key] = state

    for push_key in list(push_state.keys()):
        if push_key not in active_keys:
            push_state.pop(push_key, None)

    if push_state:
        STORE.save_runtime_value("runtime_progress_push_state", trim_runtime_state_map(push_state, keep=500))
    return pushed


def enforce_task_registry_control_plane() -> list[dict]:
    """Promote weak registry states into explicit recovery or blocked states."""
    if not CONFIG.get("ENABLE_TASK_REGISTRY", True):
        return []

    env_id = current_env_spec()["id"]
    now = int(time.time())
    grace = int(CONFIG.get("TASK_CONTROL_RECEIPT_GRACE", 180))
    cooldown = int(CONFIG.get("TASK_CONTROL_FOLLOWUP_COOLDOWN", 300))
    max_attempts = max(1, int(CONFIG.get("TASK_CONTROL_MAX_ATTEMPTS", 2)))
    block_timeout = int(CONFIG.get("TASK_CONTROL_BLOCK_TIMEOUT", 900))
    outcomes: list[dict] = []

    for task in STORE.list_tasks(limit=int(CONFIG.get("TASK_REGISTRY_RETENTION", 100))):
        if task.get("env_id") != env_id:
            continue

        control = STORE.derive_task_control_state(task["task_id"])
        action = STORE.reconcile_task_control_action(task, control)
        control = STORE.derive_task_control_state(task["task_id"])
        contract = control.get("contract") or {}
        if not (contract.get("required_receipts") or []):
            continue
        control_state = str(control.get("control_state") or "")
        if control_state not in {
            "received_only",
            "planning_only",
            "progress_only",
            "calculator_running",
            "awaiting_verifier",
            "dev_running",
            "awaiting_test",
            "test_running",
        }:
            continue

        idle = max(0, now - int(task.get("last_progress_at") or task.get("updated_at") or now))
        total = max(0, now - int(task.get("started_at") or now))
        action = action or control.get("control_action")
        attempts = int((action or {}).get("attempts", 0))
        last_followup_at = int((action or {}).get("last_followup_at", 0))

        if idle < grace:
            continue

        if task.get("status") == "blocked" and str(task.get("blocked_reason") or "") in {
            "missing_pipeline_receipt",
            "control_followup_failed",
        }:
            continue

        should_block = attempts >= max_attempts or total >= block_timeout
        if should_block:
            blocked_reason = "missing_pipeline_receipt"
            if attempts >= max_attempts and not task.get("session_key"):
                blocked_reason = "control_followup_failed"
            STORE.update_task_fields(
                task["task_id"],
                status="blocked",
                current_stage="等待结构化回执",
                blocked_reason=blocked_reason,
                updated_at=now,
            )
            STORE.record_task_event(
                task["task_id"],
                "control_blocked",
                {
                    "reason": blocked_reason,
                    "control_state": control_state,
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            record_change_log(
                "anomaly",
                "守护控制面判定任务阻塞",
                {
                    "question": task.get("question") or task.get("last_user_message") or "未知任务",
                    "control_state": control_state,
                    "idle": idle,
                    "duration": total,
                    "blocked_reason": blocked_reason,
                    "task_id": task["task_id"],
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "blocked",
                    "blocked_reason": blocked_reason,
                    "control_state": control_state,
                }
            )
            if action:
                STORE.update_control_action(
                    int(action["id"]),
                    status="blocked",
                    summary="任务缺少结构化回执，控制面已判阻塞。",
                    control_state=control_state,
                )
            continue

        if now - last_followup_at < cooldown:
            continue

        session_key = str(task.get("session_key") or "")
        question = str(task.get("question") or task.get("last_user_message") or "未知任务")
        stage = str(task.get("current_stage") or "处理中")
        if not session_key:
            STORE.update_task_fields(
                task["task_id"],
                status="blocked",
                current_stage="等待结构化回执",
                blocked_reason="control_followup_failed",
                updated_at=now,
            )
            STORE.record_task_event(
                task["task_id"],
                "control_blocked",
                {
                    "reason": "control_followup_failed",
                    "control_state": control_state,
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "blocked",
                    "blocked_reason": "control_followup_failed",
                    "control_state": control_state,
                }
            )
            if action:
                STORE.update_control_action(
                    int(action["id"]),
                    status="blocked",
                    summary="守护控制面无法继续催办，任务已阻塞。",
                    control_state=control_state,
                )
            continue

        control_message = build_control_plane_followup(
            task,
            control,
            idle=idle,
            total=total,
        )
        ok, error_kind = send_guardian_followup(session_key, control_message)
        next_attempts = attempts + 1
        if action:
            STORE.update_control_action(
                int(action["id"]),
                status="sent" if ok else "pending",
                attempts=next_attempts,
                last_followup_at=now,
                last_error=error_kind or "",
                control_state=control_state,
            )
        STORE.record_task_event(
            task["task_id"],
            "control_followup",
            {
                "control_state": control_state,
                "attempt": next_attempts,
                "idle": idle,
                "total": total,
                "sent": ok,
                "error_kind": error_kind or "",
                "timestamp": datetime.now().isoformat(),
            },
        )
        if ok:
            record_change_log(
                "pipeline",
                "守护控制面发起结构化回执催办",
                {
                    "question": question,
                    "task_id": task["task_id"],
                    "control_state": control_state,
                    "idle": idle,
                    "duration": total,
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "followup_sent",
                    "control_state": control_state,
                }
            )
        elif next_attempts >= max_attempts or error_kind in {
            "session_lock",
            "model_auth",
            "model_unavailable",
            "model_pool_failed",
            "unknown",
        }:
            STORE.update_task_fields(
                task["task_id"],
                status="blocked",
                current_stage="等待结构化回执",
                blocked_reason="control_followup_failed",
                updated_at=now,
            )
            STORE.record_task_event(
                task["task_id"],
                "control_blocked",
                {
                    "reason": "control_followup_failed",
                    "control_state": control_state,
                    "error_kind": error_kind or "",
                    "idle": idle,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            record_change_log(
                "anomaly",
                "守护控制面催办失败，任务已标记阻塞",
                {
                    "question": question,
                    "task_id": task["task_id"],
                    "control_state": control_state,
                    "blocked_reason": "control_followup_failed",
                    "error_kind": error_kind or "",
                    "idle": idle,
                    "duration": total,
                },
            )
            outcomes.append(
                {
                    "task_id": task["task_id"],
                    "action": "blocked",
                    "blocked_reason": "control_followup_failed",
                    "control_state": control_state,
                }
            )
            if action:
                STORE.update_control_action(
                    int(action["id"]),
                    status="blocked",
                    attempts=next_attempts,
                    last_followup_at=now,
                    last_error=error_kind or "",
                    summary="守护控制面催办失败，任务已阻塞。",
                    control_state=control_state,
                )
    capture_control_plane_learnings(outcomes)
    write_task_registry_snapshot()
    return outcomes


def has_config_changes() -> bool:
    """检查配置变更"""
    spec = current_env_spec()
    env_home = spec["home"]
    env_code = spec["code"]

    if (env_home / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {env_home} && git diff --quiet")
        if code != 0:
            return True
    
    if env_code.exists() and (env_code / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {env_code} && git diff --quiet")
        if code != 0:
            return True
    
    return False


def get_current_version() -> str:
    """获取当前版本"""
    env_code = current_env_spec()["code"]
    if (env_code / ".git").exists():
        code, stdout, _ = run_cmd(f"cd {env_code} && git describe --tags --always")
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

    def post_json(url: str, payload: dict[str, Any]) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return
    
    # 钉钉
    if CONFIG.get("DINGTALK_WEBHOOK"):
        try:
            post_json(
                CONFIG["DINGTALK_WEBHOOK"],
                {
                    "msgtype": "markdown",
                    "markdown": {"title": title, "text": text}
                },
            )
            log(f"钉钉通知已发送: {title}")
        except Exception as e:
            log(f"钉钉通知失败: {e}")
    
    # 飞书
    if CONFIG.get("FEISHU_WEBHOOK"):
        try:
            post_json(
                CONFIG["FEISHU_WEBHOOK"],
                {"msg_type": "text", "content": f"{title}\n{message}"},
            )
            log(f"飞书通知已发送: {title}")
        except Exception as e:
            log(f"飞书通知失败: {e}")
    
    # macOS 通知
    if CONFIG.get("ENABLE_MAC_NOTIFY"):
        run_cmd(f'osascript -e \'display notification "{message}" with title "OpenClaw Guardian: {title}"\'')


def restart_gateway():
    """重启 Gateway"""
    spec = current_env_spec()
    log(f"尝试重启 Gateway ({spec['id']})...")

    if spec["id"] == "official":
        run_args([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=120)
        run_args([str(OFFICIAL_MANAGER), "stop"], timeout=120)
        code, stdout, stderr = run_args([str(OFFICIAL_MANAGER), "start"], timeout=300)
        if code == 0 and check_gateway_health():
            log("官方验证版 Gateway 重启成功")
            return True
        log(f"官方验证版 Gateway 重启失败: {(stderr or stdout).strip()}", "ERROR")
        return False

    run_args([str(OFFICIAL_MANAGER), "stop"], timeout=120)
    run_args([str(DESKTOP_RUNTIME), "stop", "gateway"], timeout=120)
    code, stdout, stderr = run_args([str(DESKTOP_RUNTIME), "start", "gateway"], timeout=180)
    if code != 0:
        log(f"主用版 Gateway 重启失败: {(stderr or stdout).strip()}", "ERROR")
        return False

    time.sleep(5)
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
    env_code = current_env_spec()["code"]
    if not (env_code / ".git").exists():
        return False
    
    code, _, _ = run_cmd(f"cd {env_code} && git fetch --dry-run")
    return code == 0


def do_auto_update() -> bool:
    """执行自动更新"""
    spec = current_env_spec()
    if spec["id"] == "official":
        auto_update_enabled = CONFIG.get("OPENCLAW_OFFICIAL_AUTO_UPDATE", False)
    else:
        auto_update_enabled = CONFIG.get("AUTO_UPDATE", False)
    if not auto_update_enabled:
        return False
    
    channel = CONFIG.get("UPDATE_CHANNEL", "stable")
    log(f"执行自动更新 ({channel})...")
    
    # 备份当前版本
    current_ver = get_current_version()
    VERSIONS["current"] = current_ver
    VERSIONS["history"].append({
        "version": current_ver,
        "date": datetime.now().isoformat(),
        "commit": run_cmd(f"cd {spec['code']} && git rev-parse HEAD")[1].strip()
    })
    # 保留最近5个版本
    VERSIONS["history"] = VERSIONS["history"][-5:]
    save_versions()
    
    # 执行更新
    if spec["id"] == "official":
        code, stdout, stderr = run_args([str(OFFICIAL_MANAGER), "update"], timeout=1800)
    else:
        code, stdout, stderr = run_cmd(f"openclaw update --channel {channel}")
    
    if code != 0:
        log(f"更新失败: {stderr or stdout}")
        # 回滚到稳定版本
        if rollback_to_last_good():
            restart_gateway()
        notify("自动更新失败", f"更新失败，已回退到上一版本\n{(stderr or stdout)[:200]}", "error")
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
    """为 primary/official OpenClaw 关键配置创建快照。"""
    if not CONFIG.get("ENABLE_SNAPSHOT_RECOVERY", True):
        return False
    created: list[str] = []
    keep = int(CONFIG.get("SNAPSHOT_RETENTION", 10))
    for env_id, manager in snapshot_targets():
        snapshot_dir = manager.create_snapshot(f"{label}-{env_id}")
        manager.prune(keep)
        if snapshot_dir is not None:
            created.append(snapshot_dir.name)
    if not created:
        return False
    record_change_log("snapshot", "创建配置快照", {"snapshots": created})
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
    raise_nofile_limit()
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
            load_config()

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

            runtime_log = resolve_runtime_gateway_log()
            if runtime_log.exists():
                try:
                    with open(runtime_log) as handle:
                        sync_runtime_task_registry(handle.readlines()[-4000:])
                except Exception as exc:
                    log(f"同步任务注册表失败: {exc}", "ERROR")

            scan_pipeline_progress_events()
            scan_runtime_anomalies()
            push_runtime_progress_updates()
            enforce_task_registry_control_plane()
            run_reflection_cycle()
            
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
