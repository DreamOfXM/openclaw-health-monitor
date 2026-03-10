#!/usr/bin/env python3

import unittest

from task_contracts import DEFAULT_TASK_CONTRACTS, infer_task_contract


class TaskContractsTests(unittest.TestCase):
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
