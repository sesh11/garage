# Migration notes

The SDK extracted ambient-home's telemetry functionality into the `garage-sdk` distribution and the `garage_sdk` import package.

Replace telemetry imports such as:

```python
from ambient_home.telemetry import EventLog, emit, scrub_text
```

with:

```python
from garage_sdk import EventLog, emit, scrub_text
```

The logging handler is available as `garage_sdk.EventLoggingHandler`; resource, manifest, and report schemas are available from `garage_sdk.resource`, `garage_sdk.manifest`, and `garage_sdk.report`.

Existing JSONL events without a `resource` field continue to parse. When an event log has no configured resource stamp, new event lines preserve the legacy serialized bytes. Resource-stamped lines add a `resource` object.

The app-specific manifest example is `examples/ambient-home/garage.toml`. Manifest-relative paths are resolved by the service, not by the SDK.
