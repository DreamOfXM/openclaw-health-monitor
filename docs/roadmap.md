# OpenClaw Health Monitor Roadmap

This roadmap defines the staged plan for turning `openclaw-health-monitor` into a robust external control plane for OpenClaw.

The goal is not just monitoring. The goal is:

- stable OpenClaw hosting
- reliable multi-agent task progression
- operator-visible execution truth
- upgrade-friendly architecture without patching OpenClaw core

## Phase 1: Task Control Plane

Status: in progress / largely landed

Objective:

- stop trusting free-form agent text
- build an external source of truth for task state

Scope:

- task registry
- task contracts
- ACK / evidence gate
- control state derivation
- current-task facts export
- dashboard visibility for task truth

Delivered:

- SQLite-backed task registry
- `task_contracts.json`
- contract-aware control states
- `claim_level`
- `next_actor`
- `missing_receipts`
- `phase_statuses`
- persisted `task_control_actions`
- task facts JSON for guarded user-facing progress

Success criteria:

- the system can distinguish:
  - task received
  - phase verified
  - execution verified
  - blocked
- the dashboard shows contract truth instead of model optimism

## Phase 2: Reliable Task Bus

Status: not complete

Objective:

- make multi-agent progression reliable instead of best-effort

Scope:

- contract-driven action dispatch
- retry / cooldown / escalation lifecycle
- stricter action queue consumption
- stronger single-writer progress policy
- durable task handoff behavior across restarts

Planned capabilities:

- every task enters a control action queue immediately
- every missing receipt creates a concrete control action
- retries are persisted and rate-limited
- blocked tasks are explicit, not silent
- only approved progress is allowed to reach the user

Success criteria:

- a task does not silently disappear after a planning reply
- missing downstream ACKs become visible control actions
- the control plane can explain exactly why a task is blocked

## Phase 3: Delivery Reliability and Recovery

Status: not started

Objective:

- reduce loss, duplication, and cross-talk in real user conversations

Scope:

- stronger session-to-task binding
- latest active task resolution for ambiguous follow-ups
- stale result isolation
- late result reconciliation
- fallback delivery strategy when a chat-channel ACK path fails

Planned capabilities:

- follow-up questions like `?` or `到哪了` resolve against current task facts
- stale results from older tasks are separated from new requests
- late completions can be attached as background results instead of hijacking the latest reply
- delivery failures degrade predictably instead of creating ghost progress

Success criteria:

- new requests do not get overwritten by old task completions
- old tasks do not keep spamming after completion
- ambiguous follow-ups resolve to the correct active task more often than not

## Phase 4: Evolution Plane

Status: planned

Objective:

- turn repeated incidents and corrections into system improvement

Scope:

- learnings capture
- reflection jobs
- hypothesis tracking
- validation thresholds
- promotion into rules / skills / memory / contracts

Planned capabilities:

- record recurring failures and user corrections as pending learnings
- nightly or scheduled reflection jobs summarize candidate improvements
- hypotheses require repeated validation before promotion
- promoted items become:
  - hard rules
  - contracts
  - operator guidance
  - reusable skills

Success criteria:

- the system improves from repeated failures
- fixes are promoted by evidence, not just memory or manual patching

## Phase 5: Version Management as Product Surface

Status: partially landed

Objective:

- make OpenClaw operation understandable and safe

Scope:

- single environment operation
- update, start, validate workflow
- active environment truth
- dashboard action safety

Delivered:

- primary environment management
- active environment persistence
- guarded dashboard links

## Phase 6: Operator Console UX

Status: in progress

Objective:

- make the control plane usable without reading raw logs

Scope:

- live agent activity
- task registry
- environment workflow
- control queue visibility
- tighter layout and clearer state emphasis

Desired end state:

- operators can answer:
  - what is running
  - what is blocked
  - who should act next
  - whether a task is real progress or only received

## Design Principles

- do not patch OpenClaw core unless absolutely necessary
- prefer OpenClaw configuration and primitives first
- keep execution in OpenClaw, control in Health Monitor
- treat receipts and task evidence as truth
- treat free-form model language as advisory, not authoritative
- every "in progress" claim should be backed by evidence

## What "Done" Looks Like

The project should eventually provide:

- stable external control for OpenClaw
- durable multi-agent task tracking
- reliable progress truth
- visible blocked-task reasons
- version-safe upgrade workflow
- an evolution loop that learns from real failures

That is the target state for the "OpenClaw super steward".
