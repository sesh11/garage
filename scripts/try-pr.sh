#!/usr/bin/env bash
set -euo pipefail
unset PYTHONPATH PYTHONHOME

usage() {
  printf 'Usage: %s <PR_NUMBER> [--clean]\n' "$0" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
PR_NUMBER="$1"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || usage
CLEAN=false
if [[ $# -eq 2 ]]; then
  [[ "$2" == "--clean" ]] || usage
  CLEAN=true
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
TRY_BRANCH="try/pr-$PR_NUMBER"
FETCH_REF="refs/try-pr/pr-$PR_NUMBER"
WORKTREE_BASE="${GARAGE_TRY_DIR:-$HOME/dev/garage-test}"
WORKTREE="$WORKTREE_BASE/pr-$PR_NUMBER"

if [[ "$CLEAN" == true ]]; then
  if git worktree list --porcelain | awk -v target="$WORKTREE" '$1 == "worktree" && $2 == target { found = 1 } END { exit !found }'; then
    git worktree remove --force "$WORKTREE"
  elif [[ -e "$WORKTREE" ]]; then
    printf 'Refusing to remove unregistered path: %s\n' "$WORKTREE" >&2
    exit 1
  fi
  if git show-ref --verify --quiet "refs/heads/$TRY_BRANCH"; then
    ATTACHED_PATH="$(git worktree list --porcelain | awk -v branch="refs/heads/$TRY_BRANCH" '$1 == "worktree" { path = $2 } $1 == "branch" && $2 == branch { print path; exit }')"
    if [[ -n "$ATTACHED_PATH" ]]; then
      printf 'Refusing to delete %s; it is checked out at %s\n' "$TRY_BRANCH" "$ATTACHED_PATH" >&2
      exit 1
    fi
    git branch -D "$TRY_BRANCH"
  fi
  git update-ref -d "$FETCH_REF"
  printf 'Cleaned PR %s worktree and branch.\n' "$PR_NUMBER"
  exit 0
fi

git fetch origin "pull/$PR_NUMBER/head:$FETCH_REF" --force
mkdir -p "$WORKTREE_BASE"

ATTACHED_PATH="$(git worktree list --porcelain | awk -v branch="refs/heads/$TRY_BRANCH" '$1 == "worktree" { path = $2 } $1 == "branch" && $2 == branch { print path; exit }')"
if [[ "$ATTACHED_PATH" == "$WORKTREE" ]]; then
  git -C "$WORKTREE" reset --hard "$FETCH_REF"
elif [[ -n "$ATTACHED_PATH" ]]; then
  printf '%s is already checked out at %s\n' "$TRY_BRANCH" "$ATTACHED_PATH" >&2
  exit 1
elif [[ -e "$WORKTREE" ]]; then
  printf 'Refusing to reuse unregistered path: %s\n' "$WORKTREE" >&2
  exit 1
elif git show-ref --verify --quiet "refs/heads/$TRY_BRANCH"; then
  git branch -f "$TRY_BRANCH" "$FETCH_REF"
  git worktree add "$WORKTREE" "$TRY_BRANCH"
else
  git worktree add -b "$TRY_BRANCH" "$WORKTREE" "$FETCH_REF"
fi

cd "$WORKTREE"
DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/garage-pr-$PR_NUMBER.XXXXXX")"
trap 'rm -rf "$DATA_DIR"' EXIT
export GARAGE_DATA_DIR="$DATA_DIR" GARAGE_DISPATCH=0 GARAGE_REPORT=0

uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q

PORT="${GARAGE_TRY_PORT:-8772}"
SERVE_ARGS=(serve --port "$PORT")
if [[ -f "$HOME/.garage/projects.toml" ]]; then
  SERVE_ARGS+=(--projects "$HOME/.garage/projects.toml")
elif [[ -f "$WORKTREE/examples/projects.toml" ]]; then
  SERVE_ARGS+=(--projects "$WORKTREE/examples/projects.toml")
fi

printf 'Garage test instance: http://127.0.0.1:%s\nData: %s\n' "$PORT" "$DATA_DIR"
env -u GARAGE_PROJECTS -u GARAGE_MANIFEST uv run garage "${SERVE_ARGS[@]}"
