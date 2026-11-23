# Signal sources: OTLP, Sentry, Datadog and later tools

Status: **design draft for review. Nothing here is built yet.** It expands contract 1 (Signals) in
[vision.md](vision.md) and answers its open question 1 ("when should OTLP or other signal sources be added?").

## 1. The problem in one paragraph

Today Garage learns about problems from one place: the JSONL files an app writes on the same machine with
`garage-sdk` (`[signals] source = "jsonl"`). Real users already send their errors somewhere else:
OpenTelemetry pipelines, Sentry, Datadog. This doc adds a **signal source** slot: a small plug-in per tool
that brings that tool's problems into Garage's inbox as incidents, with a link back to the evidence and the
version information Garage needs later to confirm a fix. Each user picks their sources in config and brings
their own credentials.

### Words used here

- **Signal source** (or **source**): a plug-in that brings problems from one tool into Garage.
- **OpenTelemetry (OTel)**: the open standard for app telemetry (logs, traces, metrics).
  **OTLP** is its wire protocol: apps or a **Collector** (OTel's forwarding agent) POST data to an endpoint.
- **Push / pull**: push means the tool sends to Garage (OTLP, webhooks); pull means Garage asks the tool's API.
- **Cursor**: a bookmark ("I've seen everything up to here") saved between pulls.
- **Webhook**: the tool calls a URL of ours when something happens. Needs a URL the tool can reach.
- **Native issue**: the tool's own grouped problem (a Sentry issue, a Datadog Error Tracking issue or monitor alert).
- **Evidence link**: a URL to the native issue or query, so a human or agent can see the full data in the tool.
- **Baseline**: a small "before the fix" snapshot (e.g. "412 events on version 0.2.0 in the last 24 h").

## 2. Two kinds of source

| Kind | What arrives | Who groups events into incidents | Examples |
|---|---|---|---|
| **Event source** | raw events, one per thing that happened | Garage's detector (`rules.toml`), as today | local JSONL (today), OTLP receiver |
| **Issue source** | already-grouped problems with counts | the tool; Garage filters and maps them | Sentry, Datadog, later Grafana/Alertmanager, PagerDuty, Crashlytics |

Event sources feed the existing `EventLog` → `Detector` → `Incident` path unchanged. Issue sources skip the
detector (the tool already grouped the errors) and produce `Incident`s directly, after the user's filters
(level, environment, minimum count/users). Both end in the same place: `IncidentReporter` (GitHub issue) and the
SQLite inbox (`kind=incident`), then the PM step.

```
local JSONL ─┐
OTLP ────────┼─► EventLog (local buffer) ─► Detector ─┐
             │                                         ├─► Incident ─► inbox + GitHub issue ─► triage ...
Sentry ──────┼─► pull (cursor) ─► filter + map ───────┤
Datadog ─────┘                                         │
                         confirm after release ◄───────┘ (ask the same source: did it stop on the new version?)
```

## 3. The source interface

One small async interface in the Garage service (`src/garage/sources/`), not in `garage-sdk`, so apps never
install vendor code. Sketch (names may change in review):

```python
class IssueSource(Protocol):
    name: str            # "sentry", "datadog", ... (registry key, like DETECTORS today)

    async def pull(self, cursor: Cursor | None, now: datetime) -> PullResult:
        """Return native issues that are new or changed since the cursor, plus the next cursor."""

    def to_incident(self, item: NativeIssue) -> Incident | None:
        """Map one native issue to a scrubbed Incident, or None if the user's filters drop it."""

    async def baseline(self, incident: Incident, version: str) -> Baseline | None:
        """Count this problem on the version being fixed (taken when the fix is dispatched)."""

    async def confirm(self, incident: Incident, baseline: Baseline, new_version: str) -> Confirmation:
        """After release: 'held', 'came_back' (with new evidence) or 'not_enough_data'."""

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> list[NativeIssue]:
        """Optional, hosted only: verify the signature and turn the payload into the same NativeIssue."""
```

Event sources are simpler: they only *write* `Event`s into a local `EventLog`, and the existing
`IncidentMonitor` handles the rest. They still implement `baseline`/`confirm`, computed from the local buffer
(events with the new `resource.version` and no rule hit for `quiet_for`, as contract 5c already says).

Key rules:
- **Pull with a cursor, on a schedule** (default every 5 minutes per source, configurable). Works on a Mac with no
  public URL. Webhooks are an extra input to the same mapping code, added with hosted Garage.
- **Cursor = time watermark + recent ids, with overlap.** Vendor page cursors (Sentry `Link` header, Datadog
  `meta.page.after`) only continue one query; they are not durable change feeds. So each pull asks for
  "issues last seen since watermark − 10 min", dedupes by native id, and saves the newest `lastSeen` as the next watermark.
- **Stable fingerprints**: `sentry:<org>/<issue id>`, `datadog-et:<issue id>`, `datadog-mon:<monitor id>/<group>`.
  The fingerprint is what `IncidentReporter` already dedupes GitHub issues by, so a recurring Sentry issue updates
  one GitHub issue instead of filing new ones. The existing daily cap on new issues still applies.
- **Back off on rate limits.** Both vendors return 429 and say when to retry (Sentry `X-Sentry-Rate-Limit-Reset`
  is an epoch time; Datadog `X-RateLimit-Reset` is seconds from now). A source that fails keeps its cursor and
  retries later; it never blocks other sources.
- **Read-only.** Sources never resolve, assign or comment in the vendor tool in v1 (option for later).

### Changes to the `Incident` model (service only)

`Incident` (`src/garage/incidents/base.py`) gains optional fields; existing detectors keep working:

| Field | Meaning |
|---|---|
| `source` | `"jsonl"`, `"otlp"`, `"sentry"`, ... |
| `evidence` | list of `{label, url}` links (Sentry permalink, Datadog issue/monitor URL) |
| `versions` | first-seen / last-seen app versions when the tool knows them |
| `summary` | a few scrubbed lines (exception type, culprit, top in-app frames, counts, users affected) |

`samples` stays for event sources. Issue sources leave it empty, and the GitHub issue body shows `summary` + `evidence`.

## 4. Storage: what Garage keeps and what it doesn't

1. **Always stored (SQLite, `~/.garage/garage.db`)**: the decisions Garage makes (inbox items, triage, plans,
   approvals, work orders, runs, proof), each pointing back to its evidence link. New tables:
   - `source_cursors(source, app, watermark, recent_ids, updated_at, last_error)`
   - `source_issues(source, native_id, fingerprint, item_id, summary_json, last_count, last_seen, url)` — only the
     short scrubbed summary for issues that passed the filters, so Garage can tell "new" from "rising" from "unchanged".
   - `baselines(fingerprint, source, version, count, window_s, taken_at)` — the "before" snapshot for confirm.
2. **Ephemeral for tools that already store data (Sentry, Datadog)**: Garage queries their APIs, keeps the summary
   + link above, and drops the rest. Full stack traces stay in the tool; the agent gets the link (and, if the user
   allows it, a scrubbed excerpt in the work order).
3. **Small rolling buffer only for apps with no backend** (like ambient-home): the local JSONL folder today, and for
   OTLP a Garage-owned JSONL folder `~/.garage/signals/<app id>/` with the same day files and `retention_days`
   (default 7). Detectors count over it exactly as they do today.

No vendor history is copied. Confirm works from the baseline plus a fresh query, not stored history.

## 5. Scrubbing and privacy

Anything Garage keeps may end up in a **public** GitHub issue or an agent prompt. Rules for every source:

- Every string goes through `garage_sdk.scrub.scrub_text` (secrets, tokens, emails, phones, IPs, home paths,
  500-char cap) before it is stored or rendered. Keys in `DROPPED_KEYS` are dropped.
- **Allowlist, not blocklist** for vendor data. Kept: exception type, scrubbed message/title, culprit,
  up to 5 in-app frames as `module:function:line` (file paths scrubbed), counts, users-affected count, first/last
  seen, versions, environment, level, the native link. **Never kept**: request bodies, headers, cookies, local
  variables in frames, breadcrumbs, user objects (id/email/ip/username), tags beyond an allowlist, attachments, replays.
- OTLP: log `body` is treated like free text: dropped unless the app opts in per event name; attributes are
  flattened to scalars and scrubbed exactly like `EventLog.emit` does today.
- A per-source `public_detail = "summary" | "none"` option: `none` puts only the title, counts and link in the
  GitHub issue (for users whose repo is public but whose errors are sensitive).
- The evidence link itself is safe to publish: viewing it needs a login to the user's own Sentry/Datadog.

## 6. Configuration and credentials

Per app, in `garage.toml` (no secrets). The existing `[signals]` table keeps working as the local JSONL source.

```toml
[signals]                      # unchanged: local JSONL written by garage-sdk
path = "~/Library/Logs/ambient-home/events"

[[signals.source]]
kind = "otlp"                  # Garage listens on 127.0.0.1:4318 for this app's service.name
service_name = "ambient-home"

[[signals.source]]
kind = "sentry"
base_url = "https://sentry.io"     # or https://de.sentry.io, or a self-hosted URL
org = "my-org"
projects = ["ambient-home"]
environments = ["production"]
min_level = "error"
min_events = 5                 # ignore issues with fewer events in `window`
window = "24h"
credential = "sentry"          # name of a stored credential, never the token itself

[[signals.source]]
kind = "datadog"
site = "datadoghq.com"         # or us3/us5/eu/ap1/ap2/... as the user's account requires
services = ["ambient-home"]
error_tracking = true
monitors = { tags = ["team:home"] }
credential = "datadog"
```

Credentials: each user's own tokens, read-only scopes only.
- **Sentry**: an internal integration token (organization-bound, custom read scopes; works on self-hosted) or a
  personal token, with `event:read` (plus `project:read` to list projects during setup).
- **Datadog**: an API key + a scoped application key with `error_tracking_read`, `monitors_read` (and `events_read`
  if event search is used).
- **Where they live**: v1 reads environment variables (`GARAGE_SENTRY_TOKEN`, `GARAGE_DATADOG_API_KEY`,
  `GARAGE_DATADOG_APP_KEY`), matching how Garage reads `GARAGE_GITHUB_TOKEN` today. When the workspace config from
  [PR #3](https://github.com/sesh11/garage/pull/3) lands, `credential = "sentry"` resolves from the macOS Keychain or a `0600` file and
  `garage init` / `garage doctor` prompt for and test them. Secrets never go in `garage.toml`, logs or issues.

Because the `Manifest` model lives in `garage-sdk`, adding `[[signals.source]]` is an SDK change (minor bump) and
**ambient-home's garage-sdk pin needs bumping** to use it. Old manifests stay valid (the list defaults to empty).

## 7. The connectors

Facts below were checked against the vendors' official docs on 2026-11-08 (no authenticated calls yet).
**[V]** = verified in docs, **[A]** = assumption to test during the build.

### 7.1 OTLP receiver (event source)

- Garage serves OTLP/HTTP on the same local server: `POST /v1/logs` and `POST /v1/traces` **[V: default port 4318;
  paths `/v1/logs`, `/v1/traces`, `/v1/metrics`]**. Default bind `127.0.0.1:4318`, a separate port from the UI so
  OTel exporters work with their default settings. Optional bearer client token (the existing `Store` tokens) for
  anything not on loopback.
- Encodings: binary protobuf (`application/x-protobuf`) and JSON (`application/json`) **[V]**. JSON quirks handled:
  hex trace/span ids, 64-bit numbers as strings, lowerCamelCase keys **[V]**. Protobuf parsing uses the official
  `opentelemetry-proto` package (Apache-2.0) **[V]**, as an optional extra `garage[otlp]` so the base install stays small.
- Responses: `200` with an empty export response on success; partial success also `200` with
  `rejected_*` count + message, which clients must not retry **[V]**. Used for records Garage drops (unknown service, too big).
- Mapping to `Event` (mirrors the table in vision.md): `EventName` (or `event.name` attribute) → `type`;
  scope name → `component`; severity number → `info/warning/error`; flat attributes → `data` (scrubbed);
  `service.name` / `service.version` / `service.instance.id` → `resource` **[V: these three are Stable]**;
  `deployment.environment.name` **[V: Stable; old `deployment.environment` is deprecated]** → `data.env`.
- Exceptions: OTel is moving exceptions from span events to log records; span-event exceptions are deprecated but
  still the default for much existing instrumentation **[V]**. So Garage reads both: log records and span events with
  `exception.type` / `exception.message` / `exception.stacktrace` **[V]** become `type = "exception"` events with
  `exc_type`, scrubbed `message`, and `where` (top frame). Metrics are out of scope for v1 (respond `200`, ignore).
- Records whose `service.name` matches no configured app are rejected via partial success, so one Garage can serve
  several apps (fits the per-repo catalog in PR #3).
- **How Datadog/Grafana/Honeycomb users use it**: the OTel Collector's `otlp_http` exporter **[V: current name;
  `otlphttp` is a deprecated alias]** can send a copy of the app's data to Garage alongside the vendor. Datadog
  documents ingesting OTLP, not forwarding data *out* to a third party **[V for ingest; forwarding-out not found]**,
  so "Datadog via OTLP" means the app's Collector fans out to both, not Datadog sending to Garage.

### 7.2 Sentry (issue source)

- Auth: `Authorization: Bearer <token>` **[V]**; base URL configurable (`sentry.io`, `us.sentry.io`, `de.sentry.io`,
  self-hosted) **[V for region hosts; self-hosted endpoint parity A]**.
- Pull: `GET /api/0/organizations/{org}/issues/?project=…&environment=…&query=is:unresolved lastSeen:-1h&sort=date&limit=100`
  **[V: endpoint, `query`, `lastSeen:-Nd` style filters, sort options `date,new,freq,user,trends,recommended,inbox`,
  max limit 100]**. Follow the `Link` header `rel="next"; results="true"` within one pull **[V]**.
  **[A]** exact relative-time granularity accepted (`-1h` vs `-2d`); fallback is `start`/`end`.
- Fields used: `id`, `shortId`, `title`, `culprit`, `permalink`, `count` (a string), `userCount`, `firstSeen`,
  `lastSeen`, `level`, `status` **[V]**. Severity maps from `level` (`fatal`→critical, `error`→high, `warning`→medium).
- Detail for the summary: `GET /api/0/organizations/{org}/issues/{id}/events/recommended/` → `entries[type=exception]
  .data.values[].stacktrace.frames[]` **[V]**; only in-app frames, allowlisted fields (section 5).
- Versions and confirm: issue `firstRelease.version` **[V]**; per-release counts from
  `GET /api/0/organizations/{org}/issues/{id}/tags/release/values/` (value + count) **[V]**. Baseline = count on the
  version being fixed; confirm = no (or near-zero) events with `release:<new version>` after `quiet_for` while the new
  version has traffic **[A: "has traffic" needs a second query, e.g. sessions or any event on that release]**.
  Works only if the app sets Sentry's `release` to the same version string Garage uses (`0.2.0+8bc8ef5`).
- Rate limits: per token and endpoint, read `X-Sentry-Rate-Limit-*` headers **[V]**; one list call + a few detail calls
  per 5-minute pull is expected to stay well under them **[A]**.
- Webhooks later: internal integration `issue.created/resolved/unresolved/...` with `Sentry-Hook-Signature` =
  HMAC-SHA256 of the raw body with the client secret **[V]**.

### 7.3 Datadog (issue source)

Two native kinds, both optional per app:
- **Error Tracking issues**: `POST /api/v2/error-tracking/issues/search` with `query` (e.g. `service:ambient-home env:prod`),
  `from`/`to` in epoch ms, `track` or `persona`, `order_by` **[V]**; up to 100 results, **no paging documented** **[V]**,
  so Garage narrows the window instead of paging. Fields: `error_type`, `error_message`, `file_path`, `function_name`,
  `first_seen`, `last_seen`, `first_seen_version`, `last_seen_version`, `service`, `state`, `is_crash`, plus
  `total_count` / `impacted_users` **[V]**. API maturity (GA vs preview) not stated **[A]**.
- **Monitor alerts**: `GET /api/v1/monitor/groups/search?query=group_status:alert …` or
  `GET /api/v1/monitor?group_states=alert,warn` **[V]**; states Alert / Warn / No Data / OK **[V]**. One incident per
  alerting monitor group; it clears when the group returns to OK.
- Auth: `DD-API-KEY` + `DD-APPLICATION-KEY` headers, scoped app key **[V]**; per-site API base URL **[V]**.
- Rate limits: 429 + `X-RateLimit-*` headers, per endpoint **[V]**.
- Confirm: Error Tracking `last_seen_version` plus a count query scoped to `version:<new>` **[A: exact count query
  to be found during the build, likely via the issue's events or a logs/spans search]**. Monitors: "held" = group OK
  for `quiet_for` after release; Datadog monitors don't know versions, so this is weaker evidence.
- Webhooks later: Datadog's webhook integration with a custom JSON payload (`$ALERT_TRANSITION`, `$EVENT_TITLE`, ...)
  and a custom auth header **[V]**.

### 7.4 Later sources (same interface)

| Source | Kind | Notes |
|---|---|---|
| Grafana alerting / Prometheus Alertmanager | issue | webhook contact point JSON (`status`, `alerts[]` with labels, fingerprint) **[V Grafana]**; pull via Alertmanager `GET /api/v2/alerts` **[A, not checked]** |
| PagerDuty | issue | `GET /incidents?since&until`, `Authorization: Token token=…` **[V]**; v3 webhooks **[V]**. Usually a downstream copy of Datadog/Sentry alerts, so dedupe by linked source |
| Opsgenie | — | skip: Datadog's docs say Opsgenie reaches end of support on 2027-04-05, moving to Jira Service Management **[V via Datadog docs]** |
| Firebase Crashlytics | issue | REST API is `v1alpha` with issue get / event list but no issue list **[V]**; BigQuery export is the stable path **[V]**. Revisit when an app needs mobile crash data |
| Honeycomb, Grafana Cloud, New Relic, ... | event | covered by the OTLP receiver via the user's Collector |

## 8. How it fits the rest of Garage

- **Inbox / PM step**: each new incident becomes an inbox item (`kind=incident`, `source.client="sentry"`), exactly as
  `_incident_sink` does for detector incidents today. Triage sees signals from every source side by side with asks.
- **Ready threshold**: for issue sources, `ready` = passed the user's `min_events`/`min_users` filters within `window`.
- **Work order**: includes the evidence link and the scrubbed summary; the agent is told it may open the link
  only if it has the user's credentials (it usually won't).
- **Confirm** (contract 5c): when a fix is released, Garage calls the same source's `confirm`. Not built yet anywhere,
  so phase 5 below only adds baselines and a CLI preview.
- **UI**: the incidents page shows each incident's source and evidence link; a new "Sources" panel shows last pull,
  cursor age and last error per source.
- **CLI**: `garage sources list`, `garage sources pull --dry-run` (prints what would become incidents, no writes),
  `garage sources check` (tests credentials with one read call).

## 9. Build order (one PR each)

1. **Source slot + storage**: `IssueSource` / event-source interfaces and registry, `source_cursors` /
   `source_issues` / `baselines` tables, optional `Incident` fields, `[[signals.source]]` manifest model (SDK minor
   bump), `garage sources list/pull --dry-run`, and a fake source used by tests. No behavior change for JSONL.
2. **OTLP receiver**: `/v1/logs` + `/v1/traces` on `127.0.0.1:4318`, protobuf + JSON, mapping + scrubbing, buffer into
   `~/.garage/signals/<app>/`, existing rules detect over it. Real test: the OTel Python SDK exporter and an
   `otelcol` Collector sending to Garage, a rule firing, an issue filed in a throwaway repo.
3. **Sentry source**: pull, filter, map, summary, evidence link, report + inbox. Real test: a real Sentry project
   (free plan is enough), a script that raises errors with `release` set, Garage files the issue in a throwaway repo.
4. **Datadog source**: Error Tracking + monitor groups. Real test needs a Datadog account (14-day trial) with an app
   sending errors.
5. **Baselines + confirm preview**: take a baseline when a fix is dispatched; `garage sources confirm <issue>
   --version X` reports held / came back / not enough data for JSONL, OTLP and Sentry (Datadog where possible).
   The release step that calls it automatically belongs to the release/confirm work, not here.
6. **Later**: webhooks (with hosted Garage or a tunnel), Alertmanager/Grafana, PagerDuty, Crashlytics.

## 10. Open decisions

1. **Where source config lives**: per app in `garage.toml` (proposed; needs the SDK bump and ambient-home pin bump)
   or only in Garage's workspace config (no SDK change, but splits an app's setup across two files).
2. **Credentials in v1**: environment variables now, Keychain once PR #3's workspace config lands (proposed), or
   build the Keychain store first.
3. **Default filters for vendor issues**: proposed `min_level=error`, `min_events=5` in `24h`, production
   environment only, unresolved only. Too noisy or too quiet?
4. **OTLP dependency**: optional `garage[otlp]` extra with `opentelemetry-proto` (proposed) vs JSON-only with no new dependency.
5. **Test accounts**: real end-to-end tests need a Sentry org token (free plan) and, for phase 4, a Datadog trial.
   Whose accounts, and is a throwaway GitHub repo for filed issues OK?
6. **Does ambient-home move to OTLP?** Proposed: not now. It keeps local JSONL; OTLP is for other users' apps.
