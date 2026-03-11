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
- `control-plane-summary.json`: claim levels, next actor distribution, recoverable/blocked counts
- `learning-promotion-policy.json`: reflection interval, promotion threshold, promotion rules
- `context-lifecycle-baseline.json`: recommended long-session baseline template
- `README.md`: human-readable file index

## API

- `GET /api/shared-state`
- `GET /api/context-baseline`

## Consumer Rule

- Prefer shared-state exports over scraping UI text.
- Treat shared-state as the stable external contract.
