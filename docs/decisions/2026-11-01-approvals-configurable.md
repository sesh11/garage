---
status: accepted
date: 2026-11-01
decided_by: sesh11
scope: project
areas: [approvals, policy, clients]
supersedes: []
superseded_by: null
---
# Approval mode is configurable per step

## Question
Who may approve at each step (repo choice, triage, plan, release), and through what?

## Options considered
- Only the owner, only on GitHub or the dashboard.
- One mechanism with a mode per step: manual, relay (a device such as Alfred passes on the owner's yes), delegate
  (a trusted client decides within limits), auto (a policy rule).

## Decision
A mode per step, set by whoever sets up Garage. Every step defaults to manual. A repo's manifest may make a step
stricter, never looser.

## Why
Approving by voice or phone keeps the loop moving without weakening the steps that matter.

## What this rules out / revisit when
Auto release, until there are real proof checks. Plan and release still require a human in the current schema.
Source: draft PR #3, section 5a.
