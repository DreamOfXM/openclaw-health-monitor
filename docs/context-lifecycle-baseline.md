# Context Lifecycle Baseline

## Recommended Baseline

```json
{
  "session": {
    "memoryFlush": {"enabled": true, "maxTurns": 120},
    "contextPruning": {"enabled": true, "tokenBudget": 180000},
    "dailyReset": {"enabled": true, "hour": 4},
    "idleReset": {"enabled": true, "seconds": 21600},
    "sessionMaintenance": {"enabled": true, "intervalSeconds": 1800}
  }
}
```

## Checks

- memory flush
- context pruning
- reset
- maintenance

## Product Rule

The dashboard must report whether the active environment satisfies this baseline, not just whether the keys exist.
