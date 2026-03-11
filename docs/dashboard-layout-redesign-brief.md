# Dashboard Layout Redesign Brief

## Goal

Redesign the health guardian dashboard as a true operations control plane, not a long pile of equally weighted cards.

The page must feel:

- high-tech
- modern
- calm and premium
- operationally clear
- visually intentional

This is a layout-first redesign, not a cosmetic polish.

## Core Product Question

The page must answer these questions in order:

1. Is the active environment healthy right now?
2. If not, what is the most important problem?
3. What should the operator do next?
4. What is the state of `official -> primary` promotion?
5. Where is the deeper evidence if the operator wants to inspect further?

## Current Problems

- Too many sections compete as primary content.
- Monitoring, task orchestration, incident triage, and environment promotion are mixed together.
- The page lacks one dominant operational narrative.
- The right rail is important, but the main column is also important, so attention is split.
- Dense technical evidence appears too early, before the UI establishes what matters now.
- Nearly all cards share the same visual weight, so the screen feels noisy and confusing.

## Required Information Architecture

### 1. Header

Compact but strong.

Must include:

- product identity
- active environment badge
- last refresh / live state
- operator actions

Do not let the header become oversized or decorative.

### 2. Hero Area

This must become the page's dominant zone.

The hero should combine:

- active environment state
- overall system verdict
- current incident or main attention point
- promotion readiness / current promotion status
- one clear recommended next action

This should be the first thing users understand.

### 3. KPI Strip

A compact horizontal strip directly under or inside the hero area.

Keep only decisive KPIs with strong hierarchy.

Good candidates:

- CPU / memory pressure
- session health
- gateway / guardian status
- promotion readiness

These should support the hero, not compete with it.

### 4. Current Operations Zone

This is the main work surface after the hero.

Must include:

- current task / task registry summary
- control-plane summary
- session resolution
- active agents

The goal is to show what the system is doing now and what is stuck.

### 5. Incident Timeline / Recent Events Zone

Recent anomalies, progress, and action-worthy changes should live in one clear timeline-style zone.

This area should help operators understand causality, not just show isolated cards.

### 6. Environment and Promotion Zone

This remains extremely important, but it should be cleanly structured as a release-control module, not a random side block.

Must clearly show:

- active env
- official env
- readiness to promote
- promotion stages
- upgrade action
- rollback meaning/status

This zone should look premium and highly trustworthy.

### 7. Deep Evidence Zone

Move dense technical inspection modules lower.

Include:

- memory attribution
- process tables
- slow sessions
- error logs

This is tertiary content.

### 8. Maintenance / Learning / Config

Low-priority operational support content stays later on the page.

Do not let it compete with the main monitoring and promotion flow.

## Visual Direction

The visual language should feel like:

- AI operations center
- premium mission control
- modern release cockpit

Avoid:

- generic admin dashboard look
- random glow everywhere
- too many equal-sized dark cards
- childish emoji-heavy presentation
- cluttered cyberpunk noise

Desired visual traits:

- deep navy / graphite base
- restrained luminous accents
- sharper hierarchy
- cleaner spacing rhythm
- clearer section separation
- stronger hero treatment
- fewer but better visual surfaces

## Layout Rules

- Stop stacking everything as similar sections.
- Create a clear top-to-bottom operational narrative.
- The hero must dominate the first screen.
- Secondary modules should support the hero.
- Tertiary data must look clearly secondary.
- Dangerous actions should be visible but visually contained.
- Dense tables must feel orderly, not exhausting.

## Functional Constraints

Must preserve:

- existing JS ids
- current rendering/data binding behavior
- backend APIs
- current actions and workflows

Allowed:

- restructure HTML layout
- add wrapper containers
- rename purely presentational classes
- significantly redesign CSS
- improve section ordering and grouping

## Acceptance Standard

The redesign passes only if:

- a first-time viewer can understand the page's main purpose in under 5 seconds
- the active environment and health status are immediately obvious
- the operator can clearly distinguish monitoring, action, promotion, and evidence
- the page feels modern and high-end rather than messy
- the environment/promotion module feels intentional and trustworthy
- the page is still usable on smaller screens

## Implementation Priority

Priority order:

1. page structure and hierarchy
2. hero area
3. environment and promotion module
4. operations zone
5. dense evidence readability
6. final visual polish

## Mandatory Structure For Next Iteration

The next iteration must follow this structure more strictly.

### Top Of Page

1. compact header
2. one dominant hero block
3. compact KPI strip directly attached to hero

Do not begin the page with multiple equal cards and separate sections.

### Hero Block Requirements

The hero block must combine the following into one visually dominant composition:

- active environment
- overall system verdict
- current main issue or current stable status
- promotion readiness or current promotion state
- one recommended next action

This hero should feel like the control tower of the page.

### Section Order After Hero

After the hero, the page should be ordered like this:

1. current operations and active agents
2. recent anomalies / incident timeline / progress
3. environment and promotion detail zone
4. deep evidence and diagnostics
5. maintenance / learning / config

### Explicit Prohibition

Do not keep `environment/promotion` as just another ordinary right-rail box.

It can still use a side column inside the hero composition or a structured split layout, but it must visually participate in the top narrative of the page.

### Desired Layout Pattern

Preferred layout pattern:

- Hero row: wide left command/status surface + right promotion/release surface
- KPI strip: directly below hero
- Main body: operations and anomalies first
- Lower body: evidence and maintenance later

### Review Heuristic

If a screenshot of the page is taken, the first thing the eye should understand must be:

- what environment is active
- whether the system is healthy
- whether a promotion is ready/running/blocked
- what the operator should do next

If the screenshot still reads like many same-weight cards, the redesign has failed.

## Wireframe Authority

Use `docs/dashboard-wireframe-v1.md` as the authoritative layout skeleton.

If the wireframe and any older layout idea conflict, follow the wireframe.
