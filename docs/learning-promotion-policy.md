# Learning Promotion Policy

> Note
>
> This file describes the promotion policy surface.
> The ownership split is defined in
> `docs/learning-reflection-rearchitecture.md`:
>
> - `OpenClaw` owns learn / reflect / promote
> - `health-monitor` owns visibility / audit / verification

## Daily Rules

- learnings accumulate occurrences
- repeated learnings move from `pending` -> `reviewed` -> `promoted`
- promoted items keep evidence and target type
- a daily memory file is generated at `memory/YYYY-MM-DD.md`

## Runtime Inputs

- `REFLECTION_INTERVAL_SECONDS`
- `LEARNING_PROMOTION_THRESHOLD`

## Exports

- `data/shared-state/learning-promotion-policy.json`
- `.learnings/ERRORS.md`
- `.learnings/LEARNINGS.md`
- `.learnings/FEATURE_REQUESTS.md`
- `MEMORY.md`
