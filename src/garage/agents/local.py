"""Restart-safe local coding-agent CLI sessions in isolated git worktrees."""

import os
import sys
import json
import time
import uuid
import fcntl
import shlex
import base64
import signal
import asyncio
import subprocess
from typing import Literal
from pathlib import Path
from contextlib import contextmanager, asynccontextmanager
from collections.abc import Iterator, AsyncIterator

from pydantic import BaseModel, ConfigDict

from garage_sdk import scrub_text
from garage.github import GitHubIssues
from garage.agents.base import SessionState, SessionStatus, CreatedSession


BUILD_LEFTOVERS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "node_modules", ".DS_Store"}
)


class LocalCapacityError(RuntimeError):
    """The local queue has no free execution slot; retry on the next poll."""


class LocalResult(BaseModel):
    """The structured result required before publishing local work."""

    model_config = ConfigDict(strict=True)
    summary: str
    pr_url: str | None
    test_result: Literal["passed", "failed", "skipped"]
    risk: Literal["low", "medium", "high"]


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=env)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _identity(pid: int) -> str:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip()


def _publishable(name: str) -> bool:
    parts = Path(name).parts
    if any(part.startswith(".env") or part in ("credentials", "auth.json") for part in parts):
        return False
    return not name.endswith(".pyc") and not BUILD_LEFTOVERS.intersection(parts)


def _stderr_tail(directory: Path, limit: int = 500) -> str:
    try:
        text = (directory / "stderr.log").read_text(errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return scrub_text(" | ".join(lines[-3:]))[-limit:]


def _alive(config: dict[str, object]) -> bool:
    pid = int(str(config.get("pid", 0)))
    return pid > 0 and bool(config.get("identity")) and _identity(pid) == config["identity"]


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _save(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


@asynccontextmanager
async def _publish_lock(path: Path) -> AsyncIterator[None]:
    with path.open("a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield


class LocalAgent:
    """Shared local runner, with CLI-specific command and event adapters."""

    def __init__(
        self,
        agent: Literal["codex", "claude-code"],
        repo_path: Path,
        data_dir: Path,
        *,
        base_branch: str = "main",
        cli_path: str | None = None,
        model: str | None = None,
        allowed_tools: str = "Read,Edit,Write,Bash,Glob,Grep",
        timeout_s: float = 3600,
        max_concurrent: int = 2,
        grace_s: float = 2,
        github: GitHubIssues | None = None,
        no_push: bool = False,
    ) -> None:
        """Configure execution; credentials remain in the CLI's environment."""
        if timeout_s <= 0 or max_concurrent < 1 or grace_s <= 0:
            raise ValueError("timeout, concurrency and signal grace must be positive")
        self.agent = agent
        self.repo_path = repo_path.resolve()
        self.data_dir = data_dir.expanduser().resolve()
        self.base_branch = base_branch
        self.cli_path = cli_path or ("codex" if agent == "codex" else "claude")
        self.model = model
        self.allowed_tools = allowed_tools
        self.timeout_s = timeout_s
        self.max_concurrent = max_concurrent
        self.grace_s = grace_s
        self.github = github
        self.no_push = no_push

    def _directory(self, session_id: str) -> Path:
        if not session_id or Path(session_id).name != session_id or session_id in (".", ".."):
            raise ValueError("invalid run id")
        return self.data_dir / "runs" / session_id

    async def create_session(
        self,
        prompt: str,
        *,
        title: str,
        tags: list[str],
        structured_output_schema: dict[str, object],
        repos: list[str] | None = None,
        platform: str | None = None,
    ) -> CreatedSession:
        """Create a branch/worktree, persist inputs, and detach a supervisor."""
        if not self.no_push and self.github is None:
            raise ValueError("publishing requires a GitHub repository and token; use --no-push for local work")
        with _lock(self.data_dir / "local.lock"):
            active = 0
            for metadata in (self.data_dir / "runs").glob("*/run.json"):
                existing = json.loads(metadata.read_text())
                if not (metadata.parent / "exit_code").exists() and _alive(existing):
                    active += 1
            if active >= self.max_concurrent:
                raise LocalCapacityError("local execution slots are full")
            run_id = uuid.uuid4().hex
            directory = self._directory(run_id)
            worktree = self.data_dir / "worktrees" / run_id
            directory.mkdir(parents=True)
            worktree.parent.mkdir(parents=True, exist_ok=True)
            branch = f"garage/{run_id}"
            _git(self.repo_path, "worktree", "add", "-b", branch, str(worktree), self.base_branch)
            instruction = (
                f"\n\nWork only in {worktree} on the existing branch {branch}. "
                "Do not create branches/worktrees, push, or open a PR. Garage handles commits and publication. "
                "Return the requested structured result with pr_url null."
            )
            (directory / "prompt.txt").write_text(prompt + instruction)
            schema = dict(structured_output_schema)
            schema["additionalProperties"] = False
            (directory / "schema.json").write_text(json.dumps(schema))
            if self.agent == "codex":
                command = [
                    self.cli_path,
                    "exec",
                    "--json",
                    "-C",
                    str(worktree),
                    "-s",
                    "workspace-write",
                    "--output-schema",
                    str(directory / "schema.json"),
                    "-o",
                    str(directory / "result.json"),
                ]
                if self.model:
                    command += ["--model", self.model]
                command += [prompt + instruction]
            else:
                command = [
                    self.cli_path,
                    "-p",
                    prompt + instruction,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--json-schema",
                    json.dumps(schema),
                    "--permission-mode",
                    "dontAsk",
                    "--allowedTools",
                    self.allowed_tools,
                ]
                if self.model:
                    command += ["--model", self.model]
            config: dict[str, object] = {
                "agent": self.agent,
                "branch": branch,
                "worktree": str(worktree),
                "title": title,
                "project": next((tag.removeprefix("project:") for tag in tags if tag.startswith("project:")), None),
                "command": command,
                "started_at": time.time(),
                "timeout_s": self.timeout_s,
                "grace_s": self.grace_s,
                "no_push": self.no_push,
                "base_branch": self.base_branch,
                "github_repo": self.github.repo if self.github else None,
            }
            _save(directory / "run.json", config)
            with (directory / "worker.log").open("ab") as log:
                worker = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("_worker.py")), str(directory)],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                )
            config.update(pid=worker.pid, identity=_identity(worker.pid))
            _save(directory / "run.json", config)
        return CreatedSession(session_id=run_id, url=directory.as_uri(), status="working")

    def _events(self, directory: Path) -> tuple[str | None, bool, object, str]:
        own_id = None
        failed = False
        output: object = None
        errors: list[str] = []
        path = directory / "events.jsonl"
        for line in path.read_text().splitlines() if path.exists() else []:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                own_id = event.get("thread_id")
            elif isinstance(event.get("session_id"), str):
                own_id = event["session_id"]
            failed |= event.get("type") == "turn.failed"
            if event.get("type") == "error":
                message = event.get("message") or event.get("error")
                if message:
                    errors.append(message if isinstance(message, str) else json.dumps(message))
            if event.get("type") == "result":
                failed |= event.get("is_error") is True
                if event.get("is_error") is True and isinstance(event.get("result"), str):
                    errors.append(event["result"])
                output = event.get("structured_output")
        return own_id, failed, output, "; ".join(errors)

    async def cancel_session(self, session_id: str, *, expired: bool = False) -> None:
        """Stop this run's process group with bounded signal escalation."""
        directory = self._directory(session_id)
        config = json.loads((directory / "run.json").read_text())
        (directory / ("expired" if expired else "cancelled")).touch()
        if _alive(config):
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(config["pid"], sig)
                except (ProcessLookupError, PermissionError):
                    # macOS reports EPERM once the group has only a zombie left.
                    break
                await asyncio.sleep(float(config["grace_s"]))

    async def get_session(self, session_id: str) -> SessionState:
        """Recover state from disk, validate a result, and publish successful work."""
        directory = self._directory(session_id)
        config = json.loads((directory / "run.json").read_text())
        own_id, failed, output, error_detail = self._events(directory)
        takeover = ""
        if own_id:
            executable = shlex.quote(str(config["command"][0]))
            flag = "resume" if config["agent"] == "codex" else "--resume"
            takeover = f"cd {shlex.quote(config['worktree'])} && {executable} {flag} {shlex.quote(own_id)}"
        config["takeover_command"] = takeover
        config["agent_session_id"] = own_id
        detail = f"branch: {config['branch']}; takeover: {takeover or 'pending CLI session id'}"
        if error_detail:
            detail += f"; CLI notices: {scrub_text(error_detail)}"
        state = SessionState(session_id=session_id, status="working", status_detail=detail)
        if (directory / "expired").exists():
            state.status = "expired"
        elif (directory / "cancelled").exists():
            state.status = "failed"
            state.status_detail = f"cancelled; {detail}"
        elif not (directory / "exit_code").exists() and _alive(config):
            if time.time() - config["started_at"] > config["timeout_s"]:
                await self.cancel_session(session_id, expired=True)
                state.status = "expired"
        elif failed or not (directory / "exit_code").exists() or (directory / "exit_code").read_text() != "0":
            state.status = "failed"
            stderr = _stderr_tail(directory)
            if stderr:
                state.status_detail = f"{detail}; stderr: {stderr}"
        else:
            try:
                if config["agent"] == "codex":
                    output = json.loads((directory / "result.json").read_text())
                result = LocalResult.model_validate(output)
            except (ValueError, OSError) as exc:
                state.status = "failed"
                state.status_detail = f"invalid structured result: {exc}; {detail}"
            else:
                async with _publish_lock(directory / "publish.lock"):
                    published = directory / "published.json"
                    if published.exists():
                        state = SessionState.model_validate_json(published.read_text())
                    else:
                        try:
                            await self._finish(config, result)
                        except Exception as exc:
                            state.status = "failed"
                            state.status_detail = f"publication failed: {exc}; {detail}"
                        else:
                            state.status = "finished"
                            state.structured_output = result.model_dump()
                            if result.pr_url:
                                state.pull_requests = SessionState.model_validate(
                                    {
                                        "session_id": session_id,
                                        "status": "finished",
                                        "pull_requests": [{"pr_url": result.pr_url}],
                                    }
                                ).pull_requests
                            _save(published, state.model_dump())
        with _lock(self.data_dir / "local.lock"):
            _save(directory / "run.json", config)
        return state

    async def _finish(self, config: dict[str, object], result: LocalResult) -> None:
        worktree = Path(str(config["worktree"]))
        if _git(worktree, "branch", "--show-current") != config["branch"]:
            raise ValueError("agent changed the run branch; refusing to publish")
        _git(worktree, "add", "--update")
        for name in _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if name and _publishable(name):
                _git(worktree, "add", "--", name)
        if _git(worktree, "diff", "--cached", "--name-only"):
            _git(worktree, "commit", "-m", str(config["title"]))
        result.pr_url = None
        if config["no_push"]:
            return
        if self.github is None or not self.github.token:
            raise ValueError("GitHub token required for publication")
        if self.github.repo != config["github_repo"]:
            raise ValueError("GitHub repository differs from the run's target")
        env = dict(os.environ)
        credential = base64.b64encode(f"x-access-token:{self.github.token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {credential}",
            GIT_TERMINAL_PROMPT="0",
        )
        _git(worktree, "push", "origin", f"HEAD:refs/heads/{config['branch']}", env=env)
        result.pr_url = await self.github.create_pull_request(
            str(config["title"]),
            result.summary,
            head=str(config["branch"]),
            base=str(config["base_branch"]),
        )

    @staticmethod
    def map_status(status: str, status_detail: str) -> SessionStatus | None:
        """Local states already use the dispatcher's normalized vocabulary."""
        try:
            return SessionStatus(status)
        except ValueError:
            return None
