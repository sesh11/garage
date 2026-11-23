---
status: accepted
date: 2026-11-01
decided_by: sesh11
scope: project
areas: [routing, models, api]
supersedes: []
superseded_by: null
---
# Garage runs no LLM of its own for routing

## Question
How does Garage decide which repo an ask belongs to?

## Options considered
- Garage calls a model to classify the ask.
- The assistant the person is talking to (Alfred, Claude Code, Codex, Devin) picks from Garage's repo catalog.
- Always ask the human.

## Decision
The assistant the person is talking to picks the repo. Garage falls back to the source app's project, then asks the
human. Garage itself does not call a model to route.

## Why
That assistant already understands the ask. Garage stays free of model keys and model costs.

## What this rules out / revisit when
Garage-side classification. Revisit if most asks arrive with no assistant attached (e.g. raw GitHub issues).
Proposed extension to all model work: [2026-11-08-design-memory-in-repo](2026-11-08-design-memory-in-repo.md).
Source: draft PR #3.
