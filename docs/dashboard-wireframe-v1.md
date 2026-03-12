# Dashboard Wireframe V1

## Purpose

This wireframe fixes the page structure for the health guardian dashboard.

The frontend implementation must follow this structure instead of inventing a new layout.

## First-Screen Reading Goal

Within 5 seconds, the operator must understand:

1. which environment is active
2. whether the system is healthy
3. whether promotion is ready / running / blocked
4. what action should be taken next

## Page Structure

### A. Header

Single compact row.

Left:

- product name
- short subtitle

Right:

- refresh
- restart gateway
- emergency recover
- live / updated state

Do not make header tall.

### B. Hero Supergrid

This is the dominant area at the top of the dashboard.

Use a 2-column hero composition:

- left side = operational command surface
- right side = environment + promotion surface

#### B1. Left Hero Surface

Large feature panel.

Must contain:

- overall verdict headline
- current active environment
- current main issue or current stable state
- current recommended next action
- one short supporting line explaining why

This is the emotional and operational center of the page.

#### B2. Right Hero Surface

A stacked release-control module inside the hero zone.

Must contain:

- current active environment summary
- official environment summary
- promotion readiness summary
- promote action
- promotion stage strip / stage board

This is not a sidebar. It is part of the hero.

### C. KPI Strip

Directly below the hero.

One horizontal strip of compact KPI cards only.

Include existing major KPIs:

- CPU
- memory
- sessions
- gateway status

Visual rule:

- compact
- supportive
- lower emphasis than hero

### D. Operations Zone

First major section below the KPI strip.

2-column layout.

Left:

- active agents
- current task / task registry

Right:

- control-plane health
- session resolution
- control queue

This section explains what the system is doing now.

### E. Incident Timeline Zone

Full-width section below operations.

Must contain:

- recent anomalies / progress
- diagnosis / suggestions

This section should read like a timeline / incident stream, not unrelated cards.

### F. Promotion Detail Zone

Full-width section below incident timeline.

Must contain:

- environment workflow
- environment cards
- promotion summary
- promotion status board

This is where the operator inspects the full upgrade path after seeing the hero summary.

### G. Evidence Zone

Below promotion details.

Contains deeper inspection modules:

- memory attribution
- top processes
- error logs
- process monitor
- slow sessions

This zone is clearly tertiary.

### H. Maintenance Zone

Bottom of dashboard.

Contains:

- learning / self evolution
- config management

## Mapping To Existing Content

### Hero left should summarize from existing data

- `incident-summary`
- `control-plane-summary`
- `session-resolution`

### Hero right should embed these existing blocks or summaries of them

- `environment-summary`
- `promotion-summary`
- `promotion-status-board`

### Promotion detail zone keeps the fuller versions

- `environment-workflow`
- `environment-cards`
- `promotion-status-board` or a detailed instance of the same content

## Structural Rules

- No independent right rail on first screen.
- No first-screen stack of same-weight sections.
- Hero must be visually larger than KPI strip.
- KPI strip must be visually smaller than hero.
- Operations must come before deep evidence.
- Promotion detail must come before dense technical tables.

## Acceptance Check

If the top of the page still looks like:

- header
- stat cards
- ordinary left column
- ordinary right rail

then the implementation fails.
