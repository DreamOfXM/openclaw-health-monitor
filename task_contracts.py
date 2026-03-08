#!/usr/bin/env python3
"""External task-contract helpers for the health-monitor control plane."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_TASK_CONTRACTS = {
    "default_contract": "single_agent",
    "contracts": [
        {
            "id": "quant_guarded",
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
        },
        {
            "id": "delivery_pipeline",
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
        },
        {
            "id": "single_agent",
            "description": "Ad-hoc work that does not require a multi-agent pipeline contract.",
            "keywords": [],
            "required_receipts": [],
        },
    ],
}


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
    best: tuple[int, dict[str, Any]] | None = None
    for item in catalog.get("contracts", []):
        keywords = item.get("keywords") or []
        score = sum(1 for keyword in keywords if keyword and keyword.lower() in lowered)
        if score <= 0:
            continue
        if not best or score > best[0]:
            best = (score, item)

    if best:
        return best[1]
    if existing_contract_id:
        return get_contract_by_id(catalog, existing_contract_id)
    return get_contract_by_id(catalog, None)
