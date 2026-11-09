#!/usr/bin/env bash
set -euo pipefail
unset PYTHONPATH PYTHONHOME

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is required. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh' >&2
  exit 1
fi

SOURCE="${GARAGE_INSTALL_SOURCE:-git+https://github.com/sesh11/garage}"
REF="${GARAGE_INSTALL_REF:-main}"
uv tool install --reinstall "$SOURCE@$REF"

mkdir -p "$HOME/.garage"

if [[ ! -f "$HOME/.garage/projects.toml" ]]; then
  cat > "$HOME/.garage/projects.toml" <<'PROJECTS'
# Edit project paths to match the checkouts on this machine.

[[project]]
name = "garage"
repo = "sesh11/garage"
path = "~/dev/garage"
agent = "devin"
base_branch = "main"
check = [
  "uv run ruff check .",
  "uv run ruff format --check .",
  "uv run mypy",
  "uv run pytest -q",
]
prompt_rules = [
  "Keep garage-sdk changes additive.",
  "If pushing to origin fails with 403, use `env -u GH_TOKEN git push https://github.com/{repo}.git <branch>`.",
  "This is a public repository: never put secrets, tokens, personal data, or transcripts in PRs, issues, or comments.",
]

# [[project]]
# name = "ambient-home"
# repo = "sesh11/ambient-home"
# manifest = "~/dev/ambient-home/garage.toml"
# path = "~/dev/ambient-home"
# agent = "devin"
# base_branch = "main"
PROJECTS
fi

if [[ ! -f "$HOME/.garage/.env" ]]; then
  cat > "$HOME/.garage/.env" <<'ENV'
# Add credentials only if you enable reporting or dispatch.
# GARAGE_GITHUB_TOKEN=
# DEVIN_API_KEY=
# DEVIN_ORG_ID=
# GARAGE_DISPATCH=0
# GARAGE_REPORT=0
ENV
  chmod 600 "$HOME/.garage/.env"
fi

case "$(uname -s)" in
  Darwin)
    GARAGE_BIN="$(uv tool dir --bin)/garage"
    "$GARAGE_BIN" service install
    ;;
  *)
    printf '%s\n' 'The LaunchAgent is macOS-only. Start Garage with: garage serve --projects ~/.garage/projects.toml'
    ;;
esac
