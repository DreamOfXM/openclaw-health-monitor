#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from promotion_controller import PromotionController, build_preflight, rewrite_path_string


class FakeStore:
    def __init__(self):
        self.saved: list[tuple[str, dict]] = []

    def save_runtime_value(self, key, value):
        self.saved.append((key, value))


class PromotionControllerTests(unittest.TestCase):
    def test_rewrite_path_string_avoids_prefix_overlap(self):
        source = Path("/Users/hangzhou/.openclaw")
        target = Path("/Users/hangzhou/.openclaw-official")
        self.assertEqual(
            rewrite_path_string("/Users/hangzhou/.openclaw/workspace", source, target),
            "/Users/hangzhou/.openclaw-official/workspace",
        )
        self.assertEqual(
            rewrite_path_string("/Users/hangzhou/.openclaw-official/workspace", source, target),
            "/Users/hangzhou/.openclaw-official/workspace",
        )

    def test_build_preflight_accepts_healthy_official(self):
        environments = [
            {"id": "primary", "git_head": "aaa111", "running": False, "healthy": False},
            {"id": "official", "git_head": "bbb222", "running": True, "healthy": True},
        ]
        task_registry = {"summary": {"blocked": 0}, "current": {"control": {"control_state": "completed_verified"}}}

        summary = build_preflight(environments, task_registry)

        self.assertTrue(summary["safe_to_promote"])
        diff_check = next(item for item in summary["checks"] if item["name"] == "candidate_differs")
        self.assertTrue(diff_check["ok"])

    def test_sync_primary_state_preserves_gateway_and_rewrites_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            primary_home = base / "primary-state"
            official_home = base / "official-state"
            primary_code = base / "primary-code"
            official_code = base / "official-code"
            for path in [primary_home, official_home, primary_code, official_code]:
                path.mkdir(parents=True, exist_ok=True)

            (official_home / "workspace-dev").mkdir()
            (primary_home / "workspace-dev").mkdir()
            (official_home / "agents" / "main" / "agent").mkdir(parents=True)
            (primary_home / "agents" / "main" / "agent").mkdir(parents=True)
            (primary_home / "agents" / "verifier" / "agent").mkdir(parents=True)

            (official_home / "workspace-dev" / "AGENTS.md").write_text(f"home={official_home}\ncode={official_code}\n", encoding="utf-8")
            (official_home / "workspace-dev" / "SOUL.md").write_text("ok\n", encoding="utf-8")
            (official_home / "agents" / "main" / "agent" / "auth-profiles.json").write_text('{"profiles":{}}', encoding="utf-8")
            (official_home / "agents" / "main" / "agent" / "models.json").write_text('{"gpt":1}', encoding="utf-8")

            official_cfg = {
                "agents": {"defaults": {"workspace": str(official_home / "workspace")}},
                "gateway": {"port": 19021, "auth": {"token": "official-token"}},
            }
            primary_cfg = {"gateway": {"port": 18789, "auth": {"token": "primary-token"}}}
            (official_home / "openclaw.json").write_text(json.dumps(official_cfg), encoding="utf-8")
            (primary_home / "openclaw.json").write_text(json.dumps(primary_cfg), encoding="utf-8")

            controller = PromotionController(
                base,
                FakeStore(),
                {
                    "OPENCLAW_HOME": str(primary_home),
                    "OPENCLAW_CODE": str(primary_code),
                    "OPENCLAW_OFFICIAL_STATE": str(official_home),
                    "OPENCLAW_OFFICIAL_CODE": str(official_code),
                    "GATEWAY_PORT": 18789,
                    "OPENCLAW_OFFICIAL_PORT": 19021,
                },
            )

            controller.sync_primary_state_from_official()

            synced_cfg = json.loads((primary_home / "openclaw.json").read_text(encoding="utf-8"))
            self.assertEqual(synced_cfg["gateway"]["auth"]["token"], "primary-token")
            self.assertEqual(synced_cfg["gateway"]["port"], 18789)
            self.assertIn(str(primary_home), synced_cfg["agents"]["defaults"]["workspace"])
            self.assertEqual((primary_home / "agents" / "verifier" / "agent" / "models.json").read_text(encoding="utf-8"), '{"gpt":1}')
            self.assertIn(str(primary_home), (primary_home / "workspace-dev" / "AGENTS.md").read_text(encoding="utf-8"))

    def test_sync_primary_state_does_not_double_rewrite_official_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            primary_home = base / ".openclaw"
            official_home = base / ".openclaw-official"
            primary_code = base / "openclaw"
            official_code = base / "openclaw-official-code"
            for path in [primary_home, official_home, primary_code, official_code]:
                path.mkdir(parents=True, exist_ok=True)

            official_cfg = {
                "agents": {
                    "defaults": {"workspace": str(official_home / "workspace")},
                    "list": [
                        {"id": "main", "workspace": str(official_home / "workspace-main")},
                        {"id": "dev", "workspace": str(official_home / "workspace-dev")},
                    ],
                },
                "gateway": {"port": 19021, "auth": {"token": "official-token"}},
            }
            (official_home / "openclaw.json").write_text(json.dumps(official_cfg), encoding="utf-8")
            (primary_home / "openclaw.json").write_text(json.dumps({"gateway": {"port": 18789, "auth": {"token": "primary-token"}}}), encoding="utf-8")
            (official_home / "agents" / "main" / "agent").mkdir(parents=True)
            (primary_home / "agents" / "main" / "agent").mkdir(parents=True)
            (official_home / "agents" / "main" / "agent" / "auth-profiles.json").write_text('{}', encoding='utf-8')
            (official_home / "agents" / "main" / "agent" / "models.json").write_text('{}', encoding='utf-8')

            controller = PromotionController(
                base,
                FakeStore(),
                {
                    "OPENCLAW_HOME": str(primary_home),
                    "OPENCLAW_CODE": str(primary_code),
                    "OPENCLAW_OFFICIAL_STATE": str(official_home),
                    "OPENCLAW_OFFICIAL_CODE": str(official_code),
                    "GATEWAY_PORT": 18789,
                    "OPENCLAW_OFFICIAL_PORT": 19021,
                },
            )

            controller.sync_primary_state_from_official()

            synced_cfg = json.loads((primary_home / "openclaw.json").read_text(encoding="utf-8"))
            self.assertEqual(synced_cfg["agents"]["defaults"]["workspace"], str(primary_home / "workspace"))
            self.assertEqual(synced_cfg["agents"]["list"][0]["workspace"], str(primary_home / "workspace-main"))
            self.assertNotIn("official-official", json.dumps(synced_cfg, ensure_ascii=False))

    def test_run_records_promoted_result(self):
        store = FakeStore()
        controller = PromotionController(Path("/tmp/base"), store, {})
        environments = [
            {"id": "primary", "git_head": "aaa111", "running": False, "healthy": False},
            {"id": "official", "git_head": "bbb222", "running": True, "healthy": True},
        ]
        task_registry = {"summary": {"blocked": 0}, "current": {"control": {"control_state": "completed_verified"}}}

        with mock.patch.object(controller, "capture_backups", return_value={"primary": "snap-a", "official": "snap-b"}), \
            mock.patch.object(controller, "sync_primary_code_from_official", return_value={"official_head": "bbb222"}), \
            mock.patch.object(controller, "sync_primary_state_from_official", return_value={"primary_config": "cfg"}), \
            mock.patch.object(controller, "cutover_primary", return_value={"message": "started"}), \
            mock.patch.object(controller, "verify_primary", return_value={"checks": [{"name": "main_agent", "ok": True}]}):
            result = controller.run(environments, task_registry)

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(store.saved[-1][1]["status"], "promoted")

    def test_run_allows_manual_promotion_when_preflight_warns(self):
        store = FakeStore()
        controller = PromotionController(Path("/tmp/base"), store, {})
        environments = [
            {"id": "primary", "git_head": "aaa111", "running": False, "healthy": False},
            {"id": "official", "git_head": "bbb222", "running": True, "healthy": True},
        ]
        task_registry = {"summary": {"blocked": 0}, "current": {"control": {"control_state": "blocked_unverified"}}}

        with mock.patch.object(controller, "capture_backups", return_value={"primary": "snap-a", "official": "snap-b"}), \
            mock.patch.object(controller, "sync_primary_code_from_official", return_value={"official_head": "bbb222"}), \
            mock.patch.object(controller, "sync_primary_state_from_official", return_value={"primary_config": "cfg"}), \
            mock.patch.object(controller, "cutover_primary", return_value={"message": "started"}), \
            mock.patch.object(controller, "verify_primary", return_value={"checks": [{"name": "main_agent", "ok": True}]}):
            result = controller.run(environments, task_registry)

        self.assertEqual(result["status"], "promoted")
        self.assertTrue(result["preflight_warning"])


if __name__ == "__main__":
    unittest.main()
