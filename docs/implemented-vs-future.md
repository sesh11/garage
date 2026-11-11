# Implemented and future work

## Implemented

- A SQLite inbox, per-client hashed tokens, `/v1/items` submit/list/get/cancel endpoints,
  and SDK v1 models with committed JSON schemas. CLI clients and items commands read
  the same database. Legacy submissions and reported incidents also enter the inbox.

- The `garage-sdk` distribution provides scrubbed structured event logging, optional resource stamps, the logging bridge,
  app manifest models, and test-report models. Unstamped events retain the legacy JSONL representation.
- The Garage service provides manifest/environment configuration, rule-based incident monitoring, GitHub reporting,
  manifest-driven coding-agent dispatch, a local incidents UI, and an opt-in loopback request bridge.
- Reporting, dispatch, and bridge authentication are disabled unless configured. The example ambient-home manifest
  keeps its app-specific rules and prompt instructions outside generic Garage modules.
- Projects: a `[[project]]` projects file (name, repo, checkout path, agent, base branch, check commands, prompt
  rules, optional manifest). One `garage serve` monitors, reports, and dispatches for every project; inbox items,
  dispatch records, local agent runs, and session tags carry the project. The CLI (`projects`, `items --project`,
  `assign`, `--project` on `report`/`dispatch`) and dashboard filter by project. A single manifest is a one-project
  setup.
- Approval gate: new issues (requests, incidents at their threshold, and issues people open on GitHub) are labeled
  `garage:proposed` and wait in the inbox as `awaiting_approval`. Approve by adding `garage:ready-for-fix` on
  GitHub, the dashboard's Approve button, or `garage approve <id>`; decline by closing the issue, Decline, or
  `garage decline <id>`. All three update the same issue and inbox record. `garage ask --project <name> "..."` files
  a proposed request. The issue watcher (`GARAGE_INTAKE=1` in `garage serve`, or `garage intake`) polls each
  project's repository every 60 seconds with ETags and skips pull requests, closed issues, issues an agent already
  has, below-threshold incidents, and issues older than its start. Dispatch starts only approved issues and never
  one declined in Garage. `[policy].needs_approval` without `"triage"` turns the gate off.
- Decision log: every approve/decline is recorded in Garage with who (the GitHub login for label or close
  decisions), when, and where; `garage log <id>`, the dashboard Inbox, and `/api/items/<id>/history` show it.
- Label namespace: Garage writes only `garage:*` labels and still reads old `factory:*` labels as their `garage:`
  equivalents. `garage labels migrate --project <name>` lists open issues with old labels; `--apply` relabels them.
- Root Ruff, mypy, and pytest checks cover the service and SDK.

## Not implemented yet

- GitHub sign-in (GitHub App/OAuth) and a repository catalog; projects are listed by hand in a projects file.
- Automatic routing: Garage does not guess an ask's project. Asks with no project wait in `needs_repo` until
  `garage assign` (or `POST /v1/items/{id}/project`) names one.
- Projects spanning several repositories. Each project has one repository; the API's `repos` list leaves room.
- The PM/design (triage and plan) step. Approved items go straight to dispatch; `next_step()` in
  `src/garage/approval.py` is the hook where a plan step will slot in before dispatch.
- GitHub webhooks for instant intake; the watcher polls instead, since webhooks need Garage reachable from the
  internet.
- Approval for release (`"release"` in `[policy].needs_approval` is not used yet).
- There is no release or confirm workflow. `policy.auto_accept` is parsed but ignored.
- Test kits only add text to the dispatch prompt; there is no test runner.
- The robot kit now targets `ambient-reachy-hw-test` from ambient-devices against an isolated core worktree. It is prompt
  text only and is not hardware-verified by Garage.
- There is no hosted CI.

- Multi-app packaging and onboarding beyond the example manifest.
- Hosted or remote deployment; client tokens currently protect only the local inbox API.
- Additional detector, tracker, and coding-agent integrations.
