# Release Draft

## Version

`v0.1.0`

## Title

OpenClaw Health Monitor v0.1.0: local guard, Dashboard V2 console, and macOS desktop prototype

## Summary

OpenClaw Health Monitor is an open-source-friendly local guard layer for OpenClaw Gateway.
This release focuses on three things:

- stable local monitoring and recovery
- operator-facing Dashboard V2 console and diagnostics
- a macOS desktop wrapper prototype distributed as `.dmg` and `.app.zip`

## Highlights

- real Gateway health probes instead of port-only checks
- SQLite-backed local state store
- config snapshot recovery as the default safe recovery path
- Dashboard V2 support for snapshot list, create, and restore actions
- runtime anomaly detection for no-reply, stuck stage, and gateway disconnect patterns
- memory attribution view that separates process Top 15 and system memory
- local operator scripts: `preflight`, `start`, `status`, `verify`, `stop`
- Pake-based macOS wrapper prototype with armored lobster icon
- GitHub Actions workflow to build and attach desktop release artifacts

## Release Artifacts

- `openclaw-health-monitor-0.1.0-macos-arm64.dmg`
- `openclaw-health-monitor-0.1.0-macos-arm64.app.zip`

## Known Limits

- the desktop prototype wraps the local Dashboard V2 console; it is not a fully self-contained app bundle
- users still need the local Dashboard V2 runtime available when using the current prototype flow
- webhook or token values should not be present in tracked files or release artifacts
- git history should be reviewed if secrets may have entered commits previously

## Recommended Pre-Release Checklist

1. Confirm webhook or token values are not present in tracked files or release artifacts.
2. Clean git history if any secret entered commits.
3. Run:

```bash
cd ~/openclaw-health-monitor
.venv/bin/python -m pytest dashboard_v2/tests tests -q
make pake
make release
```

4. Confirm tracked files do not include runtime artifacts.
5. Confirm `config.conf` only contains safe placeholders.
6. Confirm Dashboard V2 starts and loads on a clean machine.
7. Confirm the generated `.dmg` and `.app.zip` open correctly on macOS.

## Migration Notes

- `dashboard_v2/` is the primary console frontend for this release line.
- `dashboard_backend.py` is the compatibility data layer behind the console.
- local runtime artifacts such as `.learnings/*.md`, `MEMORY.md`, `memory/*.md`, `data/shared-state/*.json`, `data/current-task-facts.json`, `data/task-registry-summary.json`, `data/*.db-shm`, and `data/*.db-wal` should remain outside git history.

## Suggested Tags

- `openclaw`
- `monitoring`
- `observability`
- `macos`
- `desktop-app`
- `python`
