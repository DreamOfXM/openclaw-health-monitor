# Internal Requirements

## Runtime Invariants

- OpenClaw must run in single-active-environment mode. At any moment, only one gateway may be listening: `primary` or `official`.
- All start, stop, and restart actions must resolve the target environment from `ACTIVE_OPENCLAW_ENV`. UI actions must never bypass this selector.
- Restarting the active environment must first stop both gateway variants, then start only the active one.
- Guardian auto-restart and Dashboard manual restart must share the same environment-selection rule as environment switching.
- Validation for this behavior must live in automated tests. Regressions where `18789` and `19021` listen at the same time must be treated as release blockers.
- OpenClaw should own internal `runtime-self-check` / heartbeat for silent-stage, no-final-reply, and `completed != delivered` detection.
- Health Monitor must not become the primary heartbeat owner for self-recovery decisions; it may only supervise whether OpenClaw self-check ran.

## Notes

- These rules are internal product/operations constraints, not README-level open source feature disclosures.
