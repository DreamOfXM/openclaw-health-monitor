# Validation Dashboard Auth Incident 2026-03-10

## Symptom

- Validation Dashboard on `http://127.0.0.1:19021` returned:
  - `unauthorized: gateway token mismatch`
  - then `unauthorized: too many failed authentication attempts (retry later)`

## Root Cause

The failure was not a single UI bug.

There were two layers:

1. Control UI navigation could drop `#token` / `#gatewayUrl`
   - Session links, cron links, sidebar links, and internal `pushState` / `replaceState`
     paths were not consistently preserving gateway auth hash parameters.
   - This caused the browser to reconnect without the current validation gateway token.

2. The validation gateway manager could print a new token while `19021` was still served by an old process
   - `manage_official_openclaw.sh` rotated the validation token in
     `~/.openclaw-official/openclaw.json`.
   - But the actual listener on port `19021` could remain an older gateway process.
   - Result: the generated Dashboard URL contained the new token, while the real gateway
     on `19021` still expected the old token.
   - Repeated attempts then tripped auth rate limiting and surfaced `retry later`.

## Why This Was Confusing

- Health checks still returned `200 OK`.
- The Dashboard URL looked correct because it was generated from the new config.
- The visible error looked like a frontend auth issue, but the port listener and config token
  were temporarily out of sync.

## Fix Applied

### Health monitor / validation manager

- `manage_official_openclaw.sh`
  - ensure validation Dashboard URL includes both `token` and `gatewayUrl`
  - stop/start logic now kills the real listener on `19021`, not only the launchd-managed PID
  - status/start output now aligns with the actual serving process

### Validation Control UI

- preserve auth hash across:
  - initial URL hydration
  - sidebar navigation
  - session links
  - cron run chat links
  - internal route updates via `pushState` / `replaceState`

## Operational Lesson

If validation Dashboard auth fails again, verify these in order:

1. Config token in `~/.openclaw-official/openclaw.json`
2. Actual listener PID on `19021`
3. Gateway startup log for the serving PID
4. Dashboard URL emitted by the health monitor
5. Whether the browser route still contains the expected `#token` and `#gatewayUrl`

If any of those disagree, treat it as a process/token desynchronization problem before assuming the gateway auth logic itself is broken.
