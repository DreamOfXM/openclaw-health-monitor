#!/usr/bin/env python3

import unittest

from task_contracts import DEFAULT_TASK_CONTRACTS, infer_task_contract, normalize_pipeline_receipt


class TaskContractsTests(unittest.TestCase):
    def test_a_share_closed_loop_prefers_specialized_delivery_contract(self):
        contract = infer_task_contract(
            "继续实现 A股闭环采样策略的进度治理",
            catalog=DEFAULT_TASK_CONTRACTS,
        )

        self.assertEqual(contract["id"], "a_share_delivery_pipeline")

    def test_normalize_pipeline_receipt_requires_evidence(self):
        receipt = normalize_pipeline_receipt(
            {"agent": "dev", "phase": "implementation", "action": "started", "evidence": ""},
            timestamp="2026-03-12T08:00:00",
        )
        self.assertIsNone(receipt)

    def test_non_matching_question_does_not_inherit_previous_quant_contract(self):
        contract = infer_task_contract(
            "你是哪个模型呢",
            catalog=DEFAULT_TASK_CONTRACTS,
            existing_contract_id="quant_guarded",
        )

        self.assertEqual(contract["id"], "single_agent")

    def test_blank_question_can_reuse_existing_contract(self):
        contract = infer_task_contract(
            "",
            catalog=DEFAULT_TASK_CONTRACTS,
            existing_contract_id="quant_guarded",
        )

        self.assertEqual(contract["id"], "quant_guarded")


if __name__ == "__main__":
    unittest.main()
