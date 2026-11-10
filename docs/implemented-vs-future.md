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
- Root Ruff, mypy, and pytest checks cover the service and SDK.

## Not implemented yet

- GitHub sign-in (GitHub App/OAuth) and a repository catalog; projects are listed by hand in a projects file.
- Automatic routing: Garage does not guess an ask's project. Asks with no project wait in `needs_repo` until
  `garage assign` (or `POST /v1/items/{id}/project`) names one.
- Projects spanning several repositories. Each project has one repository; the API's `repos` list leaves room.
- Approval before dispatch: ready issues in any project still dispatch when dispatch is enabled.
- There is no triage, plan, release, or confirm workflow. `policy.auto_accept` is parsed but ignored.
- Test kits only add text to the dispatch prompt; there is no test runner.
- The robot kit now targets `ambient-reachy-hw-test` from ambient-devices against an isolated core worktree. It is prompt
  text only and is not hardware-verified by Garage.
- There is no hosted CI.

- Multi-app packaging and onboarding beyond the example manifest.
- Hosted or remote deployment; client tokens currently protect only the local inbox API.
- Additional detector, tracker, and coding-agent integrations.
