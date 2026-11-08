# Garage

Garage aims to be a control loop between a running app and coding agents: turn problems and requests into useful
work, get a human's approval, build and test a fix, release it, then check that it really worked. The intended loop is
**intake → triage → plan → human approve → build → test → release → confirm**, reopening work if the problem returns.
Triage means deciding what matters; a plan says how to address it.

Today Garage implements the first practical pieces: local monitoring, GitHub reporting, and optional dispatch
(sending an issue to a coding agent). It was split from ambient-home; its event library can be installed separately.
The full approval and release loop is a [draft vision](docs/vision.md), not today's automation.

```text
App -- garage-sdk events --> [ Garage: detect + report ] --> GitHub issues
                                  [ Garage: dispatch ] <---------+
                                            |
                                            v
                                       Coding agent --> PR --> Human merges
                                                                   |
App <----------------------------------- Release / confirm (future)-+
```

PR means pull request: a proposed code change for a human to review.

## Built today vs not yet

**Built:** a scrubbed event SDK (software development kit), rule-based incidents, GitHub reporting with daily
creation caps and deduplication by fingerprint (a stable identifier for a rule and its event group),
manifest-driven dispatch with a duplicate ledger (a local record of started sessions) and a per-session
ACU (Agent Compute Unit) limit, a local incidents UI, and a loopback request bridge for local clients.
A manifest is the app's `garage.toml` configuration file.

**Not yet:** triage and plan approval, release/deploy and confirmation, a test runner, multi-repository onboarding
and GitHub sign-in, hosted deployment, or hosted CI (continuous integration). Local checks exist.
Test-kit commands currently become prompt instructions, not tests Garage executes.
See [implemented versus future work](docs/implemented-vs-future.md).

## Issue labels and lifecycle

The names retain the `factory:` prefix for compatibility:

- `IncidentReporter` in `src/garage/report.py` creates `factory:incident` issues with `factory:observed` below
  the rule's threshold, or directly with `factory:ready-for-fix` at the threshold. On an existing issue,
  reaching the threshold adds ready-for-fix and removes observed. A human can add ready-for-fix sooner.
- `RequestLogger` in the same file creates voice/user request issues with `factory:request` and
  `factory:ready-for-fix`, skipping observed. There is no triage or plan approval gate today.
- `Dispatcher` in `src/garage/dispatch.py` starts sessions for open ready issues without `factory:dispatched`.
  It saves the session in its ledger before adding **both** `factory:dispatched` and `factory:fixing`.
  This path does not remove ready-for-fix. On session completion it comments with the outcome and removes fixing,
  leaving dispatched; removing dispatched allows another attempt.
- Separately, `POST /api/requests/{number}/fixing` calls `RequestLogger.mark_fixing` through the bridge:
  it adds `factory:fixing` and removes `factory:ready-for-fix`.
- The reporter reopens a closed incident when samples newer than its closure appear, resetting observed/ready/fixing
  based on the fresh count. It does not clear dispatched. This is recurrence reporting, not post-release
  confirmation: Garage does not merge, deploy, or close issues as confirmed fixed.

## Quick start

### Install from Git

Install just the SDK:

```sh
pip install "garage-sdk @ git+https://github.com/sesh11/garage.git@main#subdirectory=packages/garage-sdk"
```

Install the SDK and service:

```sh
pip install \
  "garage-sdk @ git+https://github.com/sesh11/garage.git@main#subdirectory=packages/garage-sdk" \
  "garage @ git+https://github.com/sesh11/garage.git@main"
```

From a checkout, adapt the example to your app's repository and event directory, then run:

```sh
garage serve --manifest examples/ambient-home/garage.toml
garage report --manifest examples/ambient-home/garage.toml --dry-run
garage dispatch --manifest examples/ambient-home/garage.toml --dry-run --show-prompt
garage report --manifest examples/ambient-home/garage.toml --offline
garage dispatch --manifest examples/ambient-home/garage.toml --offline
```

`--dry-run` reads existing GitHub issues to preview deduplication or ready dispatches, but does not mutate issues or
call Devin; public repositories can be read without a token. `--offline` implies dry-run and makes no network calls.
Offline previews do not check existing issues, so report may show already-filed incidents as new and dispatch has no
ready issues to list.

The server defaults to `http://127.0.0.1:8770`; `serve --host` and `--port` override it.
`report --since 6h` changes the default 24-hour lookback. Use `garage --help` and each subcommand's `--help`.
In a development checkout, prefix commands with `uv run`.

## Connecting an app

Use `garage-sdk` to write JSONL (one JSON event per line) locally:

```python
from pathlib import Path
from garage_sdk import EventLog
from garage_sdk.resource import Resource

events = EventLog(
    Path.home() / ".my-app/events",
    resource=Resource(app="my-app", version="0.1.0+abcdef0", install="local-example"),
)
events.emit("app.started", "main")
```

For real installations use `garage_sdk.resource.install_id(path)` for a stable random installation ID and
`version_stamp(version, commit)` for the version string. Resource stamps are optional; old unstamped events still parse.
The SDK also provides a logging handler, manifest models, and test-report models; it does not contact GitHub or agents.
Scrubbing is a safety net, not permission to log secrets or transcripts.

Start with [examples/ambient-home/garage.toml](examples/ambient-home/garage.toml) and its
[rules.toml](examples/ambient-home/rules.toml). Set `[app]` identity/repository, `[signals].path` to the SDK's event
directory, and `[detect].rules` to your rules file. Keep app-specific build checks, test-kit instructions and
dispatch prompt rules in the manifest. Manifest-relative paths resolve against its directory.
The example's robot test is instructions for an agent on a hardware machine, not a built-in Garage runner.

## Configuration

The CLI loads `.env`. Manifest selection is `--manifest`, then `GARAGE_MANIFEST`, then `./garage.toml`.

`GARAGE_MANIFEST` selects the default manifest. `GARAGE_REPO`, `GARAGE_EVENTS_DIR`, and `GARAGE_DATA_DIR` override
the repository and data paths. Reporting and dispatch are opt-in with `GARAGE_REPORT` and `GARAGE_DISPATCH`;
`GARAGE_GITHUB_TOKEN` enables issue mutations, while `DEVIN_API_KEY` and `DEVIN_ORG_ID` enable dispatch. Additional
settings include `GARAGE_MAX_NEW_ISSUES_PER_DAY`, `GARAGE_DISPATCH_INTERVAL`, `GARAGE_LOOKBACK`,
`GARAGE_POLL_INTERVAL`, `GARAGE_HOST`, `GARAGE_PORT`, `GARAGE_DEVIN_MAX_ACU`, `GARAGE_BRIDGE_TOKEN`, and
`GARAGE_LOG_LEVEL` (default `INFO`).
Manifest `[dispatch].base_branch` selects the branch fixes start from and pull requests target; it defaults to `main`.

`DEVIN_API_BASE_URL` defaults to `https://api.devin.ai`; `GARAGE_DEVIN_MAX_ACU` defaults to 5 per session.
`GARAGE_DATA_DIR` defaults to `~/.garage`, `GARAGE_LOOKBACK` to `24h`, `GARAGE_POLL_INTERVAL` to `30s`,
and `GARAGE_DISPATCH_INTERVAL` to the manifest interval (default `1h`). The issue creation cap defaults to 5;
incident and request reporters count their own categories separately. Booleans accept `1/true/yes/on` or
`0/false/no/off`; durations accept values such as `30s`, `15m`, `1h`.
Reporting and dispatch default off in the server. Live dispatch requires its enable flag and all three credentials;
live one-shot reporting requires a GitHub token, not `GARAGE_REPORT`.
Keep credentials private; never put them in events, issues, manifests, or commits.

## Request bridge

Bridge calls require a loopback client address, including status GETs; the optional bearer token also protects them.
If `GARAGE_BRIDGE_TOKEN` is set, send `Authorization: Bearer <token>`. This is not hosted authentication.
Submit an ID, request text, timezone-aware `created_at`, and a matching `repository` to `POST /api/requests`.
Duplicate IDs return the existing issue; disabled reporting, a non-target repository, or a daily cap returns
`{"issue": null}`. Titles start with `Request: `.

Bridge POSTs require a loopback `Host` (`127.0.0.1`, `localhost`, or `[::1]`, with any port). If `Origin` is present,
it must match that host using `http`; `Sec-Fetch-Site`, if present, must be `same-origin` or `none`. Calls must come
from a non-browser client without cross-origin metadata or from the same origin. `POST /api/requests` requires
`Content-Type: application/json` (optional parameters such as `charset=utf-8` are accepted).

Filed requests become GitHub issues labeled `factory:request` and `factory:ready-for-fix`, and Garage dispatches them
like any other ready issue. When dispatch is enabled, a newly filed request wakes Garage immediately rather than
waiting for the next dispatch interval. `GET /api/requests/{number}` reports dispatch state, for example:

```json
{
  "number": 42,
  "dispatch": true,
  "state": "working",
  "session_url": "https://app.devin.ai/sessions/example",
  "pr_url": null
}
```

## Local Claude Code and Codex

Install and sign in to `claude` or `codex` on the machine running Garage. No Devin credentials are needed for
these agents. Start one task without an issue or manifest:

```sh
garage handoff --agent codex --repo-path ~/src/project --base main --no-push "Fix the failing test"
garage handoff --agent claude-code --repo-path ~/src/project --detach "Fix the failing test"
garage handoff-status <run-id>
```

Each run gets a new `garage/<run-id>` branch and a worktree in `~/.garage/worktrees/<run-id>`; the source
checkout is left alone. Garage saves inputs, JSONL events, stderr and exit status in `~/.garage/runs/<run-id>`.
The detached supervisor survives Garage restarts and enforces the timeout, but cannot run while the machine
is asleep or off. Poll detached runs with `handoff-status` to collect the result and publish the PR.
Status includes the branch and CLI resume command once the session ID is available. Stop a running session
before resuming it manually; the Python runner exposes `cancel_session(run_id)`.

`--no-push` commits changes only in the run's local branch. Otherwise set `GARAGE_REPO=owner/repo` and
`GARAGE_GITHUB_TOKEN`; Garage pushes to `origin` and opens the PR itself. The repository needs a configured git
author. Runs and worktrees are retained for inspection; remove them with `git worktree remove` when done.

For issue dispatch, set `agent = "claude-code"` or `agent = "codex"` under `[dispatch]` in `garage.toml`
(`"devin"` remains the default). The manifest directory is the source checkout unless `GARAGE_LOCAL_REPO_PATH`
overrides it; `dispatch.base_branch` selects the starting branch. Set `GARAGE_DISPATCH=1` and the GitHub token.
Agent credentials come from the CLI's normal login/environment, not Garage's Devin settings.

Local settings: `GARAGE_DATA_DIR` (default `~/.garage`), `GARAGE_LOCAL_TIMEOUT` (default `1h`),
`GARAGE_LOCAL_MAX_CONCURRENT` (default `2`, shared across both CLIs), `GARAGE_CODEX_PATH`, `GARAGE_CLAUDE_PATH`,
`GARAGE_LOCAL_MODEL` (or handoff `--model`) and `GARAGE_CLAUDE_ALLOWED_TOOLS`
(default `Read,Edit,Write,Bash,Glob,Grep`). Claude denies unapproved tools without asking; allowing Bash permits
arbitrary commands as your user. A worktree isolates git changes, not filesystem access or secrets. Use trusted
tasks/repositories. Cloud execution and live question relays are deferred.

## Garage building Garage (macOS)

Garage can work on its own repository. Three pieces make that possible:

- **[garage.toml](garage.toml)** at the repository root is Garage's own manifest. It lives at the root, not in
  `examples/`, because it is this repo's real config: `garage` finds `./garage.toml` by default, and the manifest
  directory is the git checkout. It lists the checks agents must pass (`ruff check`, `ruff format --check`, `mypy`,
  `pytest`) and the push and public-repo rules. Garage does not record events about itself yet, so `[signals]` and
  `[detect]` are empty and incident detection is off; `garage serve` still runs the UI, inbox, request bridge and
  dispatch. `garage report` needs a signal source, so it errors with this manifest.
- **A LaunchAgent** (macOS's way of keeping a program running in the background and restarting it if it crashes),
  installed with `garage service install`.
- **`garage update`**, because a running Garage does not update itself after you merge a Garage PR.

The manifest uses `agent = "devin"`: each fix runs on a cloud VM, so no agent ever touches the Mac checkout that runs
Garage. To use a local CLI instead, set `agent = "claude-code"` or `"codex"` and set `GARAGE_LOCAL_REPO_PATH` to a
second clone (not the running checkout); local worktrees go in `GARAGE_DATA_DIR/worktrees`.

### Install

Use a dedicated clone that neither you nor agents edit:

```sh
git clone https://github.com/sesh11/garage.git ~/dev/garage/garage-live
cd ~/dev/garage/garage-live
uv sync --locked
```

Create `~/dev/garage/garage-live/.env` (git ignores it; never commit or print it):

```sh
GARAGE_DISPATCH=1
GARAGE_GITHUB_TOKEN=...   # can label and comment on sesh11/garage issues
DEVIN_API_KEY=...
DEVIN_ORG_ID=...
# GARAGE_REPORT=1         # also file request-bridge requests as issues
```

Then preview and install the LaunchAgent:

```sh
.venv/bin/garage service install --dry-run   # prints ~/Library/LaunchAgents/ai.garage.serve.plist
.venv/bin/garage service install
.venv/bin/garage service status
tail -f ~/Library/Logs/garage/serve.log
```

The agent runs `.venv/bin/garage serve --manifest garage.toml` from the checkout with `PYTHONPATH` and `PYTHONHOME`
removed, so a hub environment cannot make Garage import an older `garage_sdk`. It sets `GARAGE_PORT=8771` and
`GARAGE_DATA_DIR=~/.garage/garage` so it can run beside a Garage for another app on 8770 with `~/.garage`
(the dispatch ledger is keyed by issue number, so two repositories must not share a data directory). Override with
`--port`, `--data-dir`, `--manifest` or `--checkout`; values the plist sets win over `.env`.

To put Garage to work, add the `factory:ready-for-fix` label to an issue in `sesh11/garage`. Garage checks on start
and then every `[dispatch].interval` (1 hour); `garage service restart` checks right away.

### Update after merging

```sh
env -u PYTHONPATH ~/dev/garage/garage-live/.venv/bin/garage update
```

`garage update` refuses if the checkout has local changes, runs `git pull --ff-only`, then `uv sync --locked`, then
restarts the LaunchAgent (`launchctl kickstart -k`). If nothing changed it does nothing. `--no-restart` skips the
restart. Manual equivalent:

```sh
cd ~/dev/garage/garage-live && git pull --ff-only && uv sync --locked
launchctl kickstart -k gui/$(id -u)/ai.garage.serve
```

Remove the LaunchAgent with `garage service uninstall` (logs and data stay).

## Local checks

Run from a checkout:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --pretty --show-error-codes
uv run pytest -q
uv run pre-commit run --all-files
```

The service, loopback request bridge, and manifest-driven dispatch are implemented. Multi-app onboarding, hosted
deployment, and other future integrations are tracked in [implemented versus future work](docs/implemented-vs-future.md).
See the [migration notes](docs/migration.md)
and [history evidence](docs/history-evidence.md).

## Inbox and client tokens

The local inbox is stored in `GARAGE_DATA_DIR/garage.db` (default `~/.garage`).
Use the same manifest and data directory for the server and CLI. Reporting and
dispatch are still off by default.

```sh
garage client add me --kind owner --default-repo owner/repo
# Save the printed token now: it is shown once, and only its hash is stored.
garage client list
garage serve
```

With that token in `GARAGE_CLIENT_TOKEN`:

```sh
curl -H "Authorization: Bearer $GARAGE_CLIENT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"example-1","text":"Improve the settings page","repo":"owner/repo"}' \
  http://localhost:8765/v1/items
curl -H "Authorization: Bearer $GARAGE_CLIENT_TOKEN" http://localhost:8765/v1/items
garage items --json
garage client remove me
```

Repeating a submission ID for the same client returns the original item. Owners
can read all items; other clients can only read and cancel their own. Without a
repository, an item waits in `needs_repo`. Matching requests filed through the
existing reporter are marked `approved`: today they go straight to ready-for-fix.
Failed filing keeps the item so the same submission can retry. Cancellation only
changes the local record; it does not close GitHub issues or stop agents. An agent
finishing is not proof a fix shipped, so it does not automatically mark work done.
The legacy request bridge keeps its existing optional token and responses.

`garage schemas --out schemas/v1` writes the SDK's portable JSON contracts.

## License

Apache License 2.0. See [LICENSE](LICENSE).
