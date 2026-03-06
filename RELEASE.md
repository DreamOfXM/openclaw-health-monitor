# Release Draft

## Version

`v0.1.0`

## Title

OpenClaw Health Monitor v0.1.0: local guard, snapshot recovery, and release-ready foundation

## Summary

OpenClaw Health Monitor is now structured as an open-source-friendly local guard layer for OpenClaw Gateway. This release focuses on safer health checks, safer recovery, simple installation, and a clearer path for future diagnostics.

## Highlights

- real Gateway health probes instead of port-only checks
- SQLite-backed local state store
- config snapshot recovery as the default safe recovery path
- local dashboard with snapshot list, create, and restore actions
- bilingual documentation and basic contributor/security docs
- simple install and start scripts
- initial unit test coverage

## Included Files

- `guardian.py`
- `dashboard.py`
- `monitor_config.py`
- `state_store.py`
- `snapshot_manager.py`
- `install.sh`
- `start.sh`
- `prepare_release.sh`
- `README.md`
- `tests/`

## Known Limits

- current runtime artifacts should be excluded from the first public commit
- previously exposed webhook or token values must be rotated before publishing
- git history should be reviewed and cleaned if secrets were committed before
- local-model-assisted diagnosis is not part of this release

## Recommended Pre-Release Checklist

1. Rotate exposed webhook or token values.
2. Clean git history if any secret entered commits.
3. Run:

```bash
cd ~/openclaw-health-monitor
./prepare_release.sh
```

4. Confirm tracked files do not include runtime artifacts.
5. Confirm `config.conf` only contains safe placeholders.
6. Confirm the dashboard starts and loads on a clean machine.

## Suggested Tags

- `openclaw`
- `monitoring`
- `observability`
- `self-healing`
- `sqlite`
- `python`
