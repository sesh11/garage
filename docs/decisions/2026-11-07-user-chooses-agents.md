---
status: accepted
date: 2026-11-07
decided_by: sesh11
scope: project
areas: [agents, runners, config]
pr: https://github.com/sesh11/garage/pull/6
supersedes: []
superseded_by: null
---
# Agents are Devin, Claude Code and Codex, chosen by the user

## Question
Which coding agents does Garage hand work to?

## Options considered
- Devin only.
- A set of agents behind one connector shape, picked by the user per project.

## Decision
Devin, Claude Code and Codex, configurable per project (`dispatch.agent` / `[[project]].agent`). Local Claude Code
and Codex runs landed in PR #6.

## Why
Garage is a product that offers options so each user configures their own setup, not one person's tooling.

## What this rules out / revisit when
Hard-coding one vendor. Add agents behind the same connector shape. See [agents-claude-codex.md](../agents-claude-codex.md).
