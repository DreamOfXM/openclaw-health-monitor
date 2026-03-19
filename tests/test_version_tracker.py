import tempfile
import unittest
from pathlib import Path
from unittest import mock

import version_tracker


class VersionTrackerTests(unittest.TestCase):
    def test_update_versions_file_tracks_current_and_known_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "versions.json"
            first = {"describe": "v1", "commit": "aaa", "status": "observed"}
            second = {"describe": "v2", "commit": "bbb", "status": "running"}

            payload = version_tracker.update_versions_file(path, first, mark_known_good=False)
            self.assertEqual(payload["current"]["describe"], "v1")
            self.assertIsNone(payload["known_good"])

            payload = version_tracker.update_versions_file(path, second, mark_known_good=True)
            self.assertEqual(payload["current"]["describe"], "v2")
            self.assertEqual(payload["known_good"]["commit"], "bbb")
            self.assertEqual(len(payload["history"]), 2)

    @mock.patch("version_tracker._run_git")
    def test_collect_version_record_reads_git_metadata(self, run_git):
        run_git.side_effect = [
            "main",
            "deadbeef1234",
            "deadbeef",
            "v2026.3.11",
            "",
            "git@github.com:me/private.git",
            "https://github.com/openclaw/openclaw.git",
            "2 138",
        ]

        record = version_tracker.collect_version_record(
            code_root=Path("/tmp/openclaw"),
            env_id="primary",
            reason="test",
        )

        self.assertEqual(record["branch"], "main")
        self.assertEqual(record["describe"], "v2026.3.11")
        self.assertEqual(record["upstream_ahead"], 2)
        self.assertEqual(record["upstream_behind"], 138)
        self.assertFalse(record["dirty"])

    def test_build_recovery_profile_prefers_known_good(self):
        profile = version_tracker.build_recovery_profile(
            {
                "current": {"commit": "bbb", "describe": "v2"},
                "known_good": {"commit": "aaa", "describe": "v1"},
            }
        )

        self.assertTrue(profile["has_known_good"])
        self.assertEqual(profile["rollback_hint"]["target_commit"], "aaa")
        self.assertTrue(profile["rollback_hint"]["config_snapshot_first"])
