import tempfile
import unittest
from pathlib import Path

from snapshot_manager import SnapshotManager


class SnapshotManagerTests(unittest.TestCase):
    def test_create_and_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "monitor"
            home = Path(tmp) / ".openclaw"
            workspace = home / "workspace-xiaoyi"
            base.mkdir()
            workspace.mkdir(parents=True)

            config_file = home / "openclaw.json"
            agents_file = workspace / "AGENTS.md"
            config_file.write_text('{"name":"before"}', encoding="utf-8")
            agents_file.write_text("before", encoding="utf-8")

            manager = SnapshotManager(base, home)
            snapshot_dir = manager.create_snapshot("test")

            self.assertIsNotNone(snapshot_dir)
            config_file.write_text('{"name":"after"}', encoding="utf-8")
            agents_file.write_text("after", encoding="utf-8")

            manager.restore_latest_snapshot()

            self.assertEqual(config_file.read_text(encoding="utf-8"), '{"name":"before"}')
            self.assertEqual(agents_file.read_text(encoding="utf-8"), "before")

    def test_prune_keeps_latest_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "monitor"
            home = Path(tmp) / ".openclaw"
            base.mkdir()
            home.mkdir()
            (home / "openclaw.json").write_text("{}", encoding="utf-8")
            manager = SnapshotManager(base, home)

            for label in ("one", "two", "three"):
                manager.create_snapshot(label)

            manager.prune(keep=2)
            self.assertEqual(len(manager.list_snapshots()), 2)


if __name__ == "__main__":
    unittest.main()
