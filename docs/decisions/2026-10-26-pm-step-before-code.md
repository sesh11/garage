---
status: accepted
date: 2026-10-26
decided_by: sesh11
scope: project
areas: [triage, plan, approvals, policy]
supersedes: []
superseded_by: null
---
# Triage and a plan come before code

## Question
Should Garage fix incidents and asks as they arrive, or decide first what is worth doing?

## Options considered
- Fix as it comes: every ready issue goes straight to a coding agent.
- A PM step: one inbox for incidents and asks, triage against a product brief, a plan before code.

## Decision
The factory includes a PM step ("rationalizing in this like a great PM"). The maintainer approves at triage, plan
and release (`[policy].needs_approval = ["triage", "plan", "release"]`).

## Why
Many small asks add up to one better feature; some should be declined. Agents build better from an approved plan.

## What this rules out / revisit when
Dispatching unapproved work. How approvals are given is configurable
([2026-11-01-approvals-configurable](2026-11-01-approvals-configurable.md)). Spec: [vision.md](../vision.md).
