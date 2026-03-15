# Internal Requirements

## Runtime Invariants

- OpenClaw must run in single-environment mode. At any moment, only the `primary` gateway may be listening.
- All start, stop, and restart actions must resolve the target environment from the runtime DB binding. UI actions must never bypass this selector.
- Restarting the active environment must stop the current gateway before starting `primary` again.
- Guardian auto-restart and Dashboard manual restart must share the same DB-based environment-selection rule.
- Validation for this behavior must live in automated tests. Regressions where non-primary listener ports are considered active must be treated as release blockers.
- OpenClaw should own internal `runtime-self-check` / heartbeat for silent-stage, no-final-reply, and `completed != delivered` detection.
- Health Monitor must not become the primary heartbeat owner for self-recovery decisions; it may only supervise whether OpenClaw self-check ran.

## Notes

- These rules are internal product/operations constraints, not README-level open source feature disclosures.
