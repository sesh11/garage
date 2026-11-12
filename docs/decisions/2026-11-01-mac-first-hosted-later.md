---
status: accepted
date: 2026-11-01
decided_by: sesh11
scope: project
areas: [deployment, agents, runners]
supersedes: []
superseded_by: null
---
# Garage runs on the user's Mac first; hosted later

## Question
Where does Garage run in its first versions?

## Options considered
- On the user's own machine (Mac first).
- A hosted service from the start.

## Decision
Mac first. Local Claude Code and Codex hand-off comes first; cloud runners and a hosted Garage come later.

## Why
The first user works from a Mac with hardware attached (the robot), local agents need no new accounts, and a hosted
service needs auth, billing and credential handling that can wait.

## What this rules out / revisit when
Multi-tenant hosting for now. Revisit when a second user needs Garage while their machine is off.
Sources: draft PR #3 (`docs/workspace-and-interface.md`), [agents-claude-codex.md](../agents-claude-codex.md).
