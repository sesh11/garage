---
status: accepted
date: 2026-11-01
decided_by: sesh11
scope: project
areas: [license, github, setup]
pr: https://github.com/sesh11/garage/pull/4
supersedes: []
superseded_by: null
---
# Open source from day one

## Question
Is Garage a personal tool or something others can install?

## Options considered
- Private tool for one maintainer.
- Open source from the start, under MIT or Apache-2.0.

## Decision
Open source from day one, Apache-2.0 (added in PR #4). Each user registers their own GitHub App through GitHub's
manifest flow, so no secrets are shared between users.

## Why
Another developer must be able to download Garage and use it on their repos with no help. Apache-2.0 adds an
explicit patent grant for a project others build on.

## What this rules out / revisit when
Personal rules and secrets in the repo. Garage's own decisions here are about the product, not one user's setup.
