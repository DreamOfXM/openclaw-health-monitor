# Health Monitor as Reviver, Not Orchestrator

## Positioning

For Xiaoyi's private OpenClaw fork, the health monitor is no longer the place where task truth is invented.

The boundary is:

- OpenClaw owns task closure truth
- health monitor owns observation, revival, restart, and rollback guidance

That means the health monitor should act like a second-party observer and reviver:

- detect when the gateway or runtime is unhealthy
- restart the active runtime
- preserve config snapshots before risky changes
- record version metadata for the running codebase
- expose the last known good version for rollback guidance

It should not become a second workflow engine.

## Why version tracking still matters

If the runtime is remotely reconfigured into a bad state, a pure process watchdog is not enough.

The reviver needs to know:

- which code revision is currently running
- which revision last restarted successfully
- which config snapshot was restored most recently
- whether the private fork has drifted from upstream

That is why the health monitor records:

- current code revision
- revision history
- known good revision
- recovery profile

## Recovery model

The health monitor recovery model is intentionally conservative:

1. config snapshot restore comes first
2. runtime restart comes second
3. code rollback is guided, but still manual

This keeps the open-source monitor generic enough to observe official OpenClaw while still supporting stronger recovery workflows for private forks.

## Shared-state artifacts

The reviver publishes:

- `openclaw-version.json`
- `openclaw-recovery-profile.json`

These artifacts let dashboards, operators, and future maintenance agents answer:

- what code is running now?
- what was the last known good revision?
- should recovery use config restore, restart, or manual code rollback?

## Operational rule

The health monitor may restart and restore.

It may recommend rollback.

It should not silently rewrite the private fork's git history or perform code updates on its own.
