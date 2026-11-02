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
- Root Ruff, mypy, and pytest checks cover the service and SDK.

## Not implemented yet

- GitHub sign-in (GitHub App/OAuth) and choosing repositories; Garage currently works one repository from its manifest.
- There is no triage, plan, release, or confirm workflow. `policy.auto_accept` is parsed but ignored.
- Test kits only add text to the dispatch prompt; there is no test runner.
- The robot kit now targets `ambient-reachy-hw-test` from ambient-devices against an isolated core worktree. It is prompt
  text only and is not hardware-verified by Garage.
- There is no hosted CI.

- Multi-app packaging and onboarding beyond the example manifest.
- Hosted or remote deployment; client tokens currently protect only the local inbox API.
- Additional detector, tracker, and coding-agent integrations.
