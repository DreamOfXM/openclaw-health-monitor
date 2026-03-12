#!/usr/bin/env python3
"""External task-contract helpers for the health-monitor control plane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ALLOWED_PIPELINE_ACTIONS = {"started", "completed", "blocked"}
RECEIPT_REQUIRED_FIELDS = ("agent", "phase", "action", "evidence")

DEFAULT_TASK_CONTRACTS = {
    "default_contract": "single_agent",
    "contracts": [
        {
            "id": "a_share_delivery_pipeline",
            "protocol_version": "hm.v1",
            "description": "A-share closed-loop sampling / strategy delivery work. Must not claim dev/test progress without real downstream receipts.",
            "keywords": [
                "a股",
                "A股",
                "沪深",
                "闭环采样",
                "采样策略",
                "股票策略",
                "选股",
                "交易日",
                "涨停",
                "跌停",
                "盘口",
                "k线",
                "择时",
            ],
            "required_receipts": [
                "pm:started",
                "pm:completed",
                "dev:started",
                "dev:completed",
                "test:started",
                "test:completed",
            ],
            "terminal_receipts": ["test:completed", "dev:blocked", "test:blocked"],
            "user_progress_rules": {
                "planning_only": "A股闭环方案已完成，但开发尚未启动。",
                "dev_running": "A股闭环实现已启动，当前存在真实开发回执。",
                "awaiting_test": "A股闭环开发已完成，但测试尚未启动。",
                "test_running": "A股闭环测试已启动，等待最终测试回执。",
                "blocked_unverified": "A股闭环任务缺少结构化流水线回执，守护系统已判定为阻塞。"
            }
        },
        {
            "id": "quant_guarded",
            "protocol_version": "hm.v1",
            "description": "Quant / financial / numerical work that should be backed by calculator and verifier receipts.",
            "keywords": [
                "量化",
                "回测",
                "收益率",
                "年化",
                "夏普",
                "回撤",
                "仓位",
                "持仓",
                "市值",
                "估值",
                "资金",
                "盈亏",
                "风控",
                "因子",
                "交易策略",
                "净值",
                "收益",
                "比例",
                "股票行情",
            ],
            "required_receipts": [
                "calculator:started",
                "calculator:completed",
                "verifier:completed",
            ],
            "terminal_receipts": ["verifier:completed", "calculator:blocked", "verifier:blocked", "risk:blocked"],
        },
        {
            "id": "delivery_pipeline",
            "protocol_version": "hm.v1",
            "description": "Product / implementation work that should continue through pm -> dev -> test.",
            "keywords": [
                "需求",
                "功能",
                "系统",
                "模块",
                "实现",
                "开发",
                "网站",
                "页面",
                "应用",
                "产品",
                "升级",
                "搭建",
                "接入",
                "迭代",
                "自动化",
                "支持",
                "能力",
            ],
            "required_receipts": [
                "pm:started",
                "pm:completed",
                "dev:started",
                "dev:completed",
                "test:started",
                "test:completed",
            ],
            "terminal_receipts": ["test:completed", "dev:blocked", "test:blocked"],
        },
        {
            "id": "single_agent",
            "protocol_version": "hm.v1",
            "description": "Ad-hoc work that does not require a multi-agent pipeline contract.",
            "keywords": [],
            "required_receipts": [],
            "terminal_receipts": ["main:completed", "main:blocked"],
        },
    ],
}


def normalize_pipeline_receipt(receipt: dict[str, Any] | None, *, timestamp: str = "") -> dict[str, str] | None:
    payload = {str(k).strip(): str(v).strip() for k, v in (receipt or {}).items() if k is not None and v is not None}
    if any(not payload.get(field) for field in RECEIPT_REQUIRED_FIELDS):
        return None
    if payload.get("action") not in ALLOWED_PIPELINE_ACTIONS:
        return None
    if not payload.get("ack_id"):
        raw = f"{payload['agent']}|{payload['phase']}|{payload['action']}|{payload['evidence']}|{timestamp}".encode("utf-8", errors="ignore")
        payload["ack_id"] = hashlib.sha1(raw).hexdigest()[:16]
    return payload


def contracts_file(base_dir: Path, configured_path: str | None = None) -> Path:
    if configured_path:
        return Path(configured_path).expanduser()
    return base_dir / "task_contracts.json"


def load_task_contract_catalog(base_dir: Path, configured_path: str | None = None) -> dict[str, Any]:
    path = contracts_file(base_dir, configured_path)
    if not path.exists():
        return DEFAULT_TASK_CONTRACTS
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_TASK_CONTRACTS


def get_contract_by_id(
    catalog: dict[str, Any], contract_id: str | None
) -> dict[str, Any]:
    contract_key = contract_id or catalog.get("default_contract") or "single_agent"
    for item in catalog.get("contracts", []):
        if item.get("id") == contract_key:
            return item
    for item in DEFAULT_TASK_CONTRACTS["contracts"]:
        if item["id"] == contract_key:
            return item
    return DEFAULT_TASK_CONTRACTS["contracts"][-1]


def infer_task_contract(
    question: str | None,
    *,
    catalog: dict[str, Any],
    existing_contract_id: str | None = None,
) -> dict[str, Any]:
    text = (question or "").strip()
    if not text and existing_contract_id:
        return get_contract_by_id(catalog, existing_contract_id)
    if not text:
        return get_contract_by_id(catalog, None)

    lowered = text.lower()
    best: tuple[int, int, dict[str, Any]] | None = None
    for item in catalog.get("contracts", []):
        keywords = item.get("keywords") or []
        score = sum(1 for keyword in keywords if keyword and keyword.lower() in lowered)
        if score <= 0:
            continue
        specificity = len(keywords)
        if not best or score > best[0] or (score == best[0] and specificity < best[1]):
            best = (score, specificity, item)

    if best:
        return best[2]
    return get_contract_by_id(catalog, None)
