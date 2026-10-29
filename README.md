# Garage

Garage is a local incident-monitoring service with an optional request bridge and coding-agent dispatch. It is split
from ambient-home; the SDK and service can be installed independently.

## Install from Git

Install the SDK from the feature branch:

```sh
pip install "garage-sdk @ git+https://github.com/sesh11/garage.git@devin/1793294366-garage-split#subdirectory=packages/garage-sdk"
```

Install the SDK and service:

```sh
pip install \
  "garage-sdk @ git+https://github.com/sesh11/garage.git@devin/1793294366-garage-split#subdirectory=packages/garage-sdk" \
  "garage @ git+https://github.com/sesh11/garage.git@devin/1793294366-garage-split"
```

Configure an app with a manifest, then run:

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

`GARAGE_MANIFEST` selects the default manifest. `GARAGE_REPO`, `GARAGE_EVENTS_DIR`, and `GARAGE_DATA_DIR` override
the repository and data paths. Reporting and dispatch are opt-in with `GARAGE_REPORT` and `GARAGE_DISPATCH`;
`GARAGE_GITHUB_TOKEN` enables issue mutations, while `DEVIN_API_KEY` and `DEVIN_ORG_ID` enable dispatch. Additional
settings include `GARAGE_MAX_NEW_ISSUES_PER_DAY`, `GARAGE_DISPATCH_INTERVAL`, `GARAGE_LOOKBACK`,
`GARAGE_POLL_INTERVAL`, `GARAGE_HOST`, `GARAGE_PORT`, `GARAGE_DEVIN_MAX_ACU`, and `GARAGE_BRIDGE_TOKEN`.

Run the local checks with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --pretty --show-error-codes
uv run pytest -q
```

The service, loopback request bridge, and manifest-driven dispatch are implemented. Multi-app onboarding, hosted
deployment, and other future integrations are tracked in [implemented versus future work](docs/implemented-vs-future.md).
This feature branch's repository has no `main` branch yet, so there is no PR. See the [migration notes](docs/migration.md)
and [history evidence](docs/history-evidence.md).
