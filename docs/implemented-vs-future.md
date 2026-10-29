# Implemented and future work

## Implemented

- The `garage-sdk` distribution provides scrubbed structured event logging, optional resource stamps, the logging bridge,
  app manifest models, and test-report models. Unstamped events retain the legacy JSONL representation.
- The Garage service provides manifest/environment configuration, rule-based incident monitoring, GitHub reporting,
  manifest-driven coding-agent dispatch, a local incidents UI, and an opt-in loopback request bridge.
- Reporting, dispatch, and bridge authentication are disabled unless configured. The example ambient-home manifest
  keeps its app-specific rules and prompt instructions outside generic Garage modules.
- Root Ruff, mypy, and pytest checks cover the service and SDK.

## Future

- Multi-app packaging and onboarding beyond the example manifest.
- Hosted or remote deployment and authentication beyond the loopback bridge's optional bearer token.
- Additional detector, tracker, and coding-agent integrations.
