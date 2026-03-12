# Single Active Environment Regression 2026-03-10

## Symptom

- The Dashboard showed `official` as the active environment.
- But both gateways were running at the same time:
  - primary on `127.0.0.1:18789`
  - official on `127.0.0.1:19021`
- New chats could still land on the primary environment even after switching to official.

## Expected Behavior

Per the environment-switching contract in the README, the two environments are mutually exclusive:

- switching to official must stop primary
- switching back to primary must stop official

Only one gateway should be running at a time.

## Root Cause

The regression was introduced in the desktop runtime startup path.

- [desktop_runtime.sh](/Users/hangzhou/openclaw-health-monitor/desktop_runtime.sh) had `start_all()` always call `start_gateway`
- that code path ignored `ACTIVE_OPENCLAW_ENV`
- so whenever the monitor stack was started or restarted, the primary gateway could be brought back even if `official` was the selected environment

The dashboard-side switch logic was already trying to enforce exclusivity, but `start all` bypassed that intent.

## Fix Applied

- `desktop_runtime.sh` now reads `ACTIVE_OPENCLAW_ENV`
- `start_all()` starts only the active gateway
- starting `official` explicitly stops primary first
- starting `primary` explicitly stops official first
- `stop_all()` now stops both gateway variants

## Verification

After the fix and restart:

- `18789` no longer had a listener
- `19021` was the only active gateway listener
- Dashboard status reported:
  - `active_environment = official`
  - `primary.running = false`
  - `official.running = true`

## Operational Lesson

Any future change to startup or restart flows must preserve the same invariant:

- `ACTIVE_OPENCLAW_ENV=primary` => only primary may listen
- `ACTIVE_OPENCLAW_ENV=official` => only official may listen

Checking only the dashboard state is not enough. Verify the actual listener ports too.
