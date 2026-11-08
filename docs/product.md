# Garage: product brief

What Garage is for, who it is for, and what it will not do. Planning agents read this before proposing a design,
and judge every ask against it. Changes to it go through a PR like code. Lines marked **TODO** need Nithin.

## What it is for

Garage is the missing middle between a running app and coding agents. It takes signals from running installs
(incidents) and asks from people (features, bugs, ideas), decides with the maintainer what is worth doing, gets a
design approved, hands the work to a coding agent, and checks the result: tests, release, and later, that the problem
actually stopped. See [vision.md](vision.md).

It behaves like a good product manager, not a ticket queue: it groups related asks, checks them against each
project's product brief, proposes options before any code, and remembers the decisions made along the way.

## Who it is for

- First: Nithin, maintaining ambient-home, its devices, and Garage itself from his Mac.
- Then: any developer who wants the same loop on their own repos. Garage is open source, so they install it, connect
  their own GitHub, and pick their own agents with no help from us.
- **TODO**: is a small team (several maintainers sharing one Garage) in scope for the first public release?

## Principles

- **The human decides.** The maintainer approves the ask, the design, and the release (approval mode is
  configurable per step), and always reviews and merges PRs. Garage never merges.
- **Vendor-neutral.** Agents (Devin, Claude Code, Codex), signal sources and trackers are plug-ins the user picks.
- **No model inside Garage for routing.** The assistant the person talks to picks the repo from Garage's catalog.
  (Proposed extension: Garage calls no model at all; see [design-step.md](design-step.md).)
- **Local first.** Runs on the user's Mac first; hosted later. No hidden telemetry: app events stay on the install's
  machine unless the user opts in.
- **Memory lives with each project.** Product brief and decision records sit in the project's repo, readable by any
  agent without Garage.
- **Plain records over clever automation.** Everything Garage decides can be read on GitHub.

## Not doing (non-goals)

- Replacing observability tools, coding agents, or CI. Garage uses them as plug-ins.
- Merging PRs or releasing without a human.
- Fixing everything as it comes in; asks are triaged and designed first.
- Collecting users' model or vendor credentials into a shared service.
- **TODO**: anything else you have already said no to.

## Current focus

- Approving new issues before anything runs (in progress).
- Projects: one Garage serving several repos (in progress).
- Garage building Garage: its own `garage.toml` and an always-on Mac service (in progress).
- The PM and design step, with design memory ([design-step.md](design-step.md)).
- **TODO**: confirm the order, and what comes after (proof and release? signal sources?).
