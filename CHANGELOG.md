# Changelog

All notable changes to this project should be documented in this file.

## [0.1.0] - 2026-03-06

### Added

- bilingual project documentation in `README.md`
- layered config loader in `monitor_config.py`
- SQLite-backed local state store in `state_store.py`
- filesystem snapshot manager in `snapshot_manager.py`
- local installer in `install.sh`
- release preparation helper in `prepare_release.sh`
- initial unit tests under `tests/`
- community files: `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`

### Changed

- switched Gateway health checks from port-open checks to `openclaw gateway health`
- switched default recovery path from git rollback to config snapshot recovery
- moved secret handling to `config.local.conf` and UI-safe masking
- updated dashboard to support snapshot list, create, and targeted restore
- updated startup flow to use a local virtualenv

### Security

- removed tracked default webhook secrets from public config
- stopped exposing webhook values to the frontend
- disabled destructive recovery by default

### Notes

- rotate any previously exposed webhook or token before publishing
- clean git history before public release if secrets were committed in the past
