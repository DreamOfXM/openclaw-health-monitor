# Shared State Model

## Goal

Make the health monitor's core runtime state readable by external agents and automation without requiring direct SQLite access.

## Directory

`data/shared-state/`

## Files

- `task-registry-snapshot.json`: task registry summary, current task, queue, session resolution
- `control-action-queue.json`: pending/sent/blocked control actions
- `runtime-health.json`: metrics, gateway health, recent runtime anomalies
- `learning-backlog.json`: learnings, suggestions, reflection runs
- `learning-runtime-status.json`: learning artifact readiness and freshness summary
- `reflection-freshness.json`: last daily-reflection / memory-maintenance / team-rollup status
- `memory-freshness.json`: `MEMORY.md` freshness and update status
- `reuse-evidence-summary.json`: promoted knowledge reuse evidence summary
- `control-plane-summary.json`: claim levels, next actor distribution, recoverable/blocked counts
- `learning-promotion-policy.json`: reflection interval, promotion threshold, promotion rules
- `context-lifecycle-baseline.json`: recommended long-session baseline template
- `README.md`: human-readable file index

## API

- `GET /api/shared-state`
- `GET /api/context-baseline`

Recommended aggregate learning supervision fields:

- `learning_runtime_status`
- `reflection_freshness`
- `memory_freshness`
- `reuse_evidence_summary`

## Consumer Rule

- Prefer shared-state exports over scraping UI text.
- Treat shared-state as the stable external contract.

## Learning Supervision Note

For learning-related data, prefer OpenClaw-owned artifacts as the primary source.

- `jsonl` artifacts are authoritative machine records
- SQLite learning rows are transitional compatibility inputs
- shared-state should expose whether the system is in `ready`, `partial`, `missing`, or `legacy_store_only` mode
