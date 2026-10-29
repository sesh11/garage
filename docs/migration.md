# Migration notes

`garage-sdk` is an independent distribution with the `garage_sdk` import package. The Garage service now uses that SDK
for events, manifests, and test reports; its app-specific behavior lives in the manifest rather than generic service code.

Replace telemetry imports such as:

```python
from ambient_home.telemetry import EventLog, emit, scrub_text
```

with:

```python
from garage_sdk import EventLog, emit, scrub_text
```

The logging handler is available as `garage_sdk.EventLoggingHandler`; resource, manifest, and report schemas are
available from `garage_sdk.resource`, `garage_sdk.manifest`, and `garage_sdk.report`.

Existing JSONL events without a `resource` field continue to parse. When an event log has no configured resource stamp, new event lines preserve the legacy serialized bytes. Resource-stamped lines add a `resource` object.

The app-specific manifest example is `examples/ambient-home/garage.toml`. Manifest-relative paths are resolved by the
service, not by the SDK.

## Service configuration

The service reads a manifest selected by `--manifest`, then `GARAGE_MANIFEST`, then `./garage.toml`. Manifest-relative
paths resolve against that manifest's directory. Ambient-home-only audio, OpenAI, microphone, and daemon settings remain
with the app and are not read by Garage.

| Previous ambient-home setting | Garage setting |
| --- | --- |
| `AMBIENT_FACTORY_REPO` | `GARAGE_REPO` |
| `AMBIENT_EVENTS_DIR` | `GARAGE_EVENTS_DIR` |
| `AMBIENT_DATA_DIR` | `GARAGE_DATA_DIR` |
| `AMBIENT_FACTORY_REPORT` | `GARAGE_REPORT` |
| `AMBIENT_GITHUB_TOKEN` | `GARAGE_GITHUB_TOKEN` |
| `AMBIENT_FACTORY_MAX_NEW_ISSUES_PER_DAY` | `GARAGE_MAX_NEW_ISSUES_PER_DAY` |
| `AMBIENT_FACTORY_DISPATCH` | `GARAGE_DISPATCH` |
| `AMBIENT_FACTORY_DISPATCH_INTERVAL` | `GARAGE_DISPATCH_INTERVAL` |
| `AMBIENT_DEVIN_MAX_ACU` | `GARAGE_DEVIN_MAX_ACU` |
| `AMBIENT_INCIDENT_LOOKBACK_S` | `GARAGE_LOOKBACK` |
| `AMBIENT_JOB_POLL_S` | `GARAGE_POLL_INTERVAL` |
| `AMBIENT_UI_HOST` | `GARAGE_HOST` |
| `AMBIENT_UI_PORT` | `GARAGE_PORT` |
| `AMBIENT_DETECTOR` | `[detect].detector` in the manifest |
| `AMBIENT_INCIDENT_RULES` | `[detect].rules` in the manifest |
| `AMBIENT_EVENT_RETENTION_DAYS` | `[signals].retention_days` in the manifest |
| `AMBIENT_FACTORY_OUTPOST` | `[dispatch].outpost` in the manifest |

`DEVIN_API_KEY`, `DEVIN_ORG_ID`, and `DEVIN_API_BASE_URL` keep their names. `GARAGE_DEVIN_MAX_ACU` replaces the old
`AMBIENT_DEVIN_MAX_ACU`; `GARAGE_BRIDGE_TOKEN` optionally authenticates local request submissions. Boolean values
accept `1/true/yes/on` and `0/false/no/off`; durations use values such as `30s`, `15m`, and `1h`. Garage does not fall
back to any `AMBIENT_*` variables.

## Reporting, dispatch, and bridge

The `factory:*` labels and `factory-fingerprint`, `factory-since`, and `factory-request` issue markers are preserved.
Dispatch migrates the legacy ledger once: if the Garage ledger is absent and the configured `legacy_ledger` exists,
it is read using the existing schema; the next save writes the new Garage ledger and never modifies the legacy file.
Fix prompts now take checks, test-kit instructions, and app-specific prompt rules from the manifest.

Request issues now use the title prefix `Request: ` (formerly `Voice request: `). The optional loopback bridge accepts
`POST /api/requests` with an ID, request text, aware `created_at`, and optional repository. If `GARAGE_BRIDGE_TOKEN` is
set, callers must send `Authorization: Bearer <token>`. Duplicate IDs return the indexed existing issue without a
second create; reporting-disabled, non-target, and capped requests return `{"issue": null}`.
