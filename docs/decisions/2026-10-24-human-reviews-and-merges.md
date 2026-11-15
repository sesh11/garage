---
status: accepted
date: 2026-10-24
decided_by: sesh11
scope: project
areas: [approvals, agents, merge]
supersedes: []
superseded_by: null
---
# Review and merge are always the maintainer's

## Question
How far may Garage and its agents take a change on their own?

## Options considered
- Agents open PRs; a human reviews and merges.
- Garage auto-merges when checks pass.

## Decision
Agents open PRs. The maintainer always reviews and merges. Garage and its agents never merge.

## Why
The maintainer stays accountable for what ships, and every change gets a human read before it lands.

## What this rules out / revisit when
Auto-merge, including for "low-risk" changes. Not expected to change.
