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
    M[Managed Environment<br/>primary]

    U -->|messages / queries| G
    G -->|spawn / receipts / progress| A
    A -->|visible replies / runtime logs| G

    H -->|health check / log scan| G
    H -->|environment control| M
    H -->|persist tasks / incidents / runtime state| S
    D -->|read status / inspect tasks| S
    D -->|operator actions| H
    D -->|environment links| M
    M -->|active target| G
```

## 2. Managed Environment Model

Health Monitor operates in **single environment mode**:

- Only one OpenClaw environment (`primary`) is managed
- Guardian guards the active environment
- No environment switching or promotion needed

Rules:

- Guardian follows the active environment recorded in config and SQLite runtime state
- All operations target the `primary` environment

## 3. External Task Registry

The task registry is intentionally implemented outside OpenClaw itself.

Why:

- avoid patching OpenClaw core
- keep upstream upgrades feasible
- make task tracking consistent across single-agent and multi-agent setups

Core records:

- `managed_tasks`
- `task_events`
- `task_contracts`
- `task_control_actions`
- runtime `kv_state`

## 4. Task Contracts and ACK Gate

```mermaid
flowchart LR
    Q[Incoming user task]
    R[Guardian task classifier]
    C[(task_contracts.json)]
    T[(task_contracts table)]
    X[Expected receipts<br/>pm/dev/test or calculator/verifier]
    G[Guardian ACK gate]

    Q --> R
    C --> R
    R --> T
    T --> X
    X --> G
```

Task contracts are external, configurable, and intentionally non-invasive.
They are operator-facing observation templates, not authoritative definitions of OpenClaw internal pipeline truth:

- `delivery_pipeline`
  - expects `pm -> dev -> test` receipts
- `quant_guarded`
  - expects `calculator -> verifier` receipts
- `single_agent`
  - no strict contract

Guardian does not trust free-form agent text for pipeline truth. It should treat contracts as derived evidence templates and avoid actively督办 OpenClaw internal handoffs by default.

## 5. Task Lifecycle

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

## 6. Control Actions Queue

```mermaid
flowchart LR
    S[Derived control state]
    A[(task_control_actions)]
    F[Guardian ops worker]
    R[Native runtime evidence]
    U[Approved operator summary]

    S --> A
    A --> F
    F -->|observe / alert / recover runtime| R
    R --> S
    S --> U
```

Principles:

- the registry is an operator-facing control-plane view, not a second orchestrator
- each control action is persisted in SQLite with attempts, last error, status, and truth level
- Guardian should consume those actions as ops guidance:
  - highlight missing evidence
  - suggest recovery
  - mark the task blocked when outer evidence is unsafe
- dashboard should expose native vs derived state explicitly, and user-facing progress should not depend on free-form agent text

## 7. Evidence Model

The control plane treats these as strong runtime evidence:

- `dispatching to agent`
- `PIPELINE_PROGRESS`
- `PIPELINE_RECEIPT`
- visible completion messages
- `dispatch complete`

The control plane should not treat free-form model text as task truth when stronger evidence exists.

## 8. Control States

Examples:

- `received_only`
  - task was accepted, but no required contract receipts arrived
- `planning_only`
  - planning evidence exists, but `dev` has not started
- `dev_running`
  - `dev` receipt exists
- `awaiting_test`
  - `dev` completed, `test` not started
- `calculator_running`
  - calculator started, waiting for structured result
- `awaiting_verifier`
  - calculator completed, verifier not done
- `blocked_unverified`
  - Guardian escalated because the contract receipts never arrived

## 9. Operator Surfaces

Dashboard exposes:

- incident summary
- environment status and switching
- memory attribution
- task registry summary
- current active task
- recent task timeline
- control actions queue and missing receipts

Guardian provides:

- anomaly detection
- silence-based follow-up
- contract-aware task follow-up
- persisted control actions with retry / block lifecycle
- blocked-task handling
- environment-aware recovery

## 10. Design Boundary

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

## 11. Related Design Docs

- `docs/control-plane-boundary-matrix.md`
  - defines the boundary between OpenClaw (execution plane) and Health Monitor (control plane)
- `docs/subagent-communication-protocol.md`
  - communication protocol for subagent coordination
