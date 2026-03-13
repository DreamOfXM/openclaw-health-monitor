# Dashboard V2 Maintenance Checklist

## Branching

- Treat `main` as the release-ready branch.
- Use short-lived follow-up branches for control-console work and merge them back quickly.
- Keep `dashboard_v2/` as the only primary UI surface.

## Code Boundaries

- Put frontend pages, JS, CSS, and route composition under `dashboard_v2/`.
- Keep backend compatibility/data aggregation in `dashboard_backend.py`.
- Keep runtime truth in `guardian.py`, `state_store.py`, `monitor_config.py`, and `data/shared-state` producers.
- Do not reintroduce a second standalone dashboard UI alongside `dashboard_v2/`.

## Runtime Data Rules

- Do not commit `.learnings/*.md`, `MEMORY.md`, `memory/*.md`.
- Do not commit `data/current-task-facts.json`, `data/task-registry-summary.json`, or `data/shared-state/*.json` except `data/shared-state/README.md`.
- Do not commit `data/*.db-shm` or `data/*.db-wal`.
- Treat local snapshots, logs, and change logs as machine state, not repository source.

## Validation

- Run `.venv/bin/python -m pytest dashboard_v2/tests tests -q` before merging.
- Run `./prepare_release.sh check` before release packaging.
- Confirm Dashboard V2 starts through `./start.sh` and responds on the dashboard port.
- Confirm environment switching, task views, learning views, and shared-state-backed panels still load real data.

## Release Notes

- Keep release messaging consistent: Dashboard V2 is the primary console.
- Mention `dashboard_backend.py` only as the compatibility data layer.
- Call out runtime artifact exclusion whenever release/repo hygiene is relevant.
