---
status: proposed
date: 2026-11-08
decided_by: sesh11
scope: project
areas: [plan, memory, models, approvals]
supersedes: []
superseded_by: null
---
# Design memory lives in each project's repo; Garage keeps the index

## Question
Where do design decisions live so that future planning remembers them, and who writes designs?

## Options considered
- Garage's database only.
- The project's repo (`docs/product.md`, `docs/decisions/`), with Garage keeping an index and the in-between states.
- Designs written by a model Garage calls itself, by the assistant that filed the ask, or by a read-only planning
  run on the user's coding agent.

## Decision
Memory lives in the project's repo; Garage's database is an index that can be rebuilt from the repo and GitHub.
Designs come from read-only planning runs on the user's agents (or a draft attached by the filing assistant), are
posted on the issue and approved there; lasting decisions land as records in the build PR. Garage holds no model
keys and calls no model.

## Why
Versioned, reviewed in PRs, readable by any agent without Garage, and per project automatically. Keeps Garage
model-free, extending [2026-11-01-no-llm-routing](2026-11-01-no-llm-routing.md).

## What this rules out / revisit when
A Garage-hosted model. Flip to accepted when the design PR merges. Details: [design-step.md](../design-step.md).
