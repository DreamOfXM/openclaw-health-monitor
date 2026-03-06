#!/usr/bin/env python3
"""Filesystem snapshot helpers for OpenClaw config recovery."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path


class SnapshotManager:
    def __init__(self, base_dir: Path, openclaw_home: Path):
        self.base_dir = base_dir
        self.openclaw_home = openclaw_home
        self.snapshot_root = base_dir / "snapshots"
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    def discover_targets(self) -> list[Path]:
        targets: list[Path] = []
        direct_files = [
            self.openclaw_home / "openclaw.json",
            self.openclaw_home / "gateway.json",
            self.openclaw_home / "AGENTS.md",
            self.openclaw_home / "SOUL.md",
        ]
        for path in direct_files:
            if path.exists() and path.is_file():
                targets.append(path)

        for workspace_dir in sorted(self.openclaw_home.glob("workspace-*")):
            if not workspace_dir.is_dir():
                continue
            for filename in ("AGENTS.md", "SOUL.md"):
                path = workspace_dir / filename
                if path.exists() and path.is_file():
                    targets.append(path)
        return targets

    def create_snapshot(self, label: str = "manual", paths: list[Path] | None = None) -> Path | None:
        targets = paths or self.discover_targets()
        if not targets:
            return None

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "snapshot"
        snapshot_dir = self.snapshot_root / f"{timestamp}-{safe_label}"
        files_dir = snapshot_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, object] = {
            "created_at": datetime.now().isoformat(),
            "label": label,
            "openclaw_home": str(self.openclaw_home),
            "files": [],
        }

        copied = 0
        for source in targets:
            if not source.exists() or not source.is_file():
                continue
            relative = source.relative_to(self.openclaw_home)
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1
            manifest["files"].append(
                {
                    "source": str(source),
                    "relative_path": str(relative),
                    "size": source.stat().st_size,
                }
            )

        if copied == 0:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            return None

        with open(snapshot_dir / "manifest.json", "w") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
        return snapshot_dir

    def list_snapshots(self) -> list[Path]:
        return sorted(
            [path for path in self.snapshot_root.iterdir() if path.is_dir()],
            reverse=True,
        )

    def restore_latest_snapshot(self) -> Path | None:
        snapshots = self.list_snapshots()
        if not snapshots:
            return None
        self.restore_snapshot(snapshots[0])
        return snapshots[0]

    def restore_snapshot(self, snapshot_dir: Path) -> None:
        manifest_file = snapshot_dir / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"Missing manifest: {manifest_file}")

        with open(manifest_file) as handle:
            manifest = json.load(handle)

        files = manifest.get("files", [])
        for entry in files:
            relative = Path(entry["relative_path"])
            source = snapshot_dir / "files" / relative
            destination = self.openclaw_home / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def prune(self, keep: int) -> None:
        if keep <= 0:
            return
        for snapshot_dir in self.list_snapshots()[keep:]:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
