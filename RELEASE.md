# Release Draft

## Version

`v0.1.0`

## Title

OpenClaw Health Monitor v0.1.0: local guard, dashboard, and macOS desktop prototype

## Summary

OpenClaw Health Monitor is an open-source-friendly local guard layer for OpenClaw Gateway.
This release focuses on three things:

- stable local monitoring and recovery
- operator-facing dashboard and diagnostics
- a macOS desktop wrapper prototype distributed as `.dmg` and `.app.zip`

## Highlights

- real Gateway health probes instead of port-only checks
- SQLite-backed local state store
- config snapshot recovery as the default safe recovery path
- dashboard support for snapshot list, create, and restore actions
- runtime anomaly detection for no-reply, stuck stage, and gateway disconnect patterns
- memory attribution view that separates process Top 15 and system memory
- local operator scripts: `preflight`, `start`, `status`, `verify`, `stop`
- Pake-based macOS wrapper prototype with armored lobster icon
- GitHub Actions workflow to build and attach desktop release artifacts

## Release Artifacts

- `openclaw-health-monitor-0.1.0-macos-arm64.dmg`
- `openclaw-health-monitor-0.1.0-macos-arm64.app.zip`

## Known Limits

- the desktop prototype wraps the local dashboard; it is not a fully self-contained app bundle
- users still need the local Dashboard runtime available when using the current prototype flow
- webhook or token values should not be present in tracked files or release artifacts
- git history should be reviewed if secrets may have entered commits previously

## Recommended Pre-Release Checklist

1. Confirm webhook or token values are not present in tracked files or release artifacts.
2. Clean git history if any secret entered commits.
3. Run:

```bash
cd ~/openclaw-health-monitor
make test
make pake
make release
```

4. Confirm tracked files do not include runtime artifacts.
5. Confirm `config.conf` only contains safe placeholders.
6. Confirm the dashboard starts and loads on a clean machine.
7. Confirm the generated `.dmg` and `.app.zip` open correctly on macOS.

## Suggested Tags

- `openclaw`
- `monitoring`
- `observability`
- `macos`
- `desktop-app`
- `python`
