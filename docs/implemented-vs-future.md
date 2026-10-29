# Implemented and future work

## Implemented in phase A

- The `garage-sdk` distribution provides scrubbed structured event logging, optional resource stamps, a configurable logging bridge, app manifest models, and test-report models.
- SDK event logs remain readable by older ambient-home telemetry consumers; events without a resource stamp retain the legacy JSONL representation.
- `examples/ambient-home/garage.toml` records the ambient-home app configuration. Its incident rules are kept in the adjacent `rules.toml`.
- Root tooling is configured as a uv workspace. Service dependencies are declared, but service modules are excluded from the phase A Ruff and mypy checks.

## Planned for phase B

- Port the incident detector, settings, report and dispatch services to the Garage service package.
- Replace service imports of `ambient_home` with `garage` and `garage_sdk` APIs.
- Add the Garage CLI, service UI/API integration, and opt-in loopback request bridge.

The service source and tests currently retain their ambient-home imports and are intentionally unchanged in phase A.
