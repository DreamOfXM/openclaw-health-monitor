# OpenClaw Health Monitor Architecture

This document describes the production-oriented control-plane architecture used by `openclaw-health-monitor`.

## 1. Control-Plane Overview

```mermaid
flowchart LR
    U[User / Feishu / Dashboard Operator]
    G[OpenClaw Gateway]
    A[OpenClaw Agents<br/>main / pm / dev / test / calculator / verifier / risk]
    H[Guardian]
    D[Dashboard]
    S[(monitor.db<br/>SQLite state store)]
    M[Managed Environments<br/>primary / official]

    U -->|messages / queries| G
    G -->|spawn / receipts / progress| A
    A -->|visible replies / runtime logs| G

    H -->|health check / log scan| G
    H -->|environment control| M
    H -->|persist tasks / incidents / runtime state| S
    D -->|read status / switch env / inspect tasks| S
    D -->|operator actions| H
    D -->|environment links| M
    M -->|active target| G
```

## 2. Managed Environment Model

```mermaid
flowchart TD
    P[primary<br/>current working OpenClaw]
    O[official<br/>isolated upstream validation]
    C[Environment Selector]
    H[Guardian]

    C -->|ACTIVE_OPENCLAW_ENV| P
    C -->|ACTIVE_OPENCLAW_ENV| O
    H -->|only guards active env| P
    H -->|only guards active env| O
```

Rules:

- only one OpenClaw environment is active at a time
- Guardian follows the active environment recorded in config and SQLite runtime state
- manual environment switching outside Health Monitor can temporarily desync the panel until the next explicit switch or resync

## 3. External Task Registry

The task registry is intentionally implemented outside OpenClaw itself.

Why:

- avoid patching OpenClaw core
- keep upstream upgrades feasible
- make task tracking consistent across single-agent and multi-agent setups

Core records:

- `managed_tasks`
- `task_events`
- runtime `kv_state`

## 4. Task Lifecycle

```mermaid
stateDiagram-v2
    [*] --> running: dispatch_started
    running --> running: stage_progress
    running --> blocked: receipt(action=blocked)
    running --> completed: visible_completion
    running --> no_reply: dispatch_complete without visible reply
    running --> background: newer active task in same session
    blocked --> running: new receipt / new stage progress
    blocked --> background: newer active task in same session
    background --> running: resumed by newer progress
    background --> completed: late completion
    completed --> [*]
    no_reply --> [*]
```

## 5. Evidence Model

The control plane treats these as strong runtime evidence:

- `dispatching to agent`
- `PIPELINE_PROGRESS`
- `PIPELINE_RECEIPT`
- visible completion messages
- `dispatch complete`

The control plane should not treat free-form model text as task truth when stronger evidence exists.

## 6. Operator Surfaces

Dashboard exposes:

- incident summary
- environment status and switching
- memory attribution
- task registry summary
- current active task
- recent task timeline

Guardian provides:

- anomaly detection
- silence-based follow-up
- blocked-task handling
- environment-aware recovery

## 7. Design Boundary

OpenClaw core is responsible for:

- execution
- agent orchestration primitives
- channel delivery

Health Monitor is responsible for:

- task tracking
- runtime diagnosis
- version/environment control
- recovery policy
- operator visibility

This separation is what allows Health Monitor to remain robust while OpenClaw itself continues to upgrade upstream.
