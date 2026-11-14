"""Offline CLI contracts, durable sessions and local publication."""

import sys
import json
import shlex
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace

import httpx
import pytest
from conftest import FakeTracker, example_manifest

from garage import cli
from garage.github import GitHubIssues
from garage.report import READY
from garage.tracker import IssueRecord
from garage.dispatch import OUTPUT_SCHEMA, Dispatcher
from garage.settings import settings_from_env
from garage.agents.base import SessionState
from garage.agents.local import LocalAgent, LocalCapacityError


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "code.py").write_text("original\n")
    git(path, "add", "code.py")
    git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake-agent"
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import os, sys, json, time, signal
from pathlib import Path
args = sys.argv[1:]
Path('args.json').write_text(json.dumps(args))
codex = args[0] == 'exec'
if codex:
    assert Path(args[args.index('-C') + 1]) == Path.cwd()
    assert args[args.index('-s') + 1] == 'workspace-write'
    print(json.dumps({'type':'thread.started', 'thread_id':'thread-123'}), flush=True)
    print(json.dumps({'type':'turn.started'}), flush=True)
else:
    assert args[args.index('--permission-mode') + 1] == 'dontAsk'
    assert '--allowedTools' in args
    print(json.dumps({'type':'system', 'subtype':'init', 'session_id':'session-123'}), flush=True)
mode = os.getenv('FAKE_MODE', 'ok')
if mode == 'hang':
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
if mode == 'failed':
    print(json.dumps({'type':'turn.failed', 'error':{'message':'failure'}}), flush=True)
    sys.exit(0)
if mode == 'nonzero':
    print('boom: unexpected flag', file=sys.stderr)
    sys.exit(7)
if mode == 'retry':
    print(json.dumps({'type':'error', 'message':'Stream interrupted; reconnecting'}), flush=True)
if mode == 'claude_error':
    print(json.dumps({'type':'result', 'is_error':True, 'result':'API Error: 401 token expired'}), flush=True)
    sys.exit(0)
if mode == 'slow':
    time.sleep(0.5)
if mode == 'artifacts':
    assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'
    Path('__pycache__').mkdir()
    Path('__pycache__/code.cpython-311.pyc').write_bytes(b'cache')
    Path('.DS_Store').write_bytes(b'finder')
    Path('notes.txt').write_text('new file\\n')
Path('code.py').write_text('fixed\\n')
output = {'summary':'Fixed code', 'pr_url':None, 'test_result':'passed', 'risk':'low'}
if mode == 'invalid':
    output['risk'] = 'unknown'
if mode != 'empty':
    if codex:
        Path(args[args.index('-o') + 1]).write_text(json.dumps(output))
    else:
        print(json.dumps({'type':'result', 'session_id':'session-123', 'structured_output':output}), flush=True)
print(json.dumps({'type':'turn.completed'}), flush=True)
"""
    )
    path.chmod(0o755)
    return path


def agent(repo: Path, tmp_path: Path, fake_cli: Path, name: str = "codex", **kwargs: object) -> LocalAgent:
    options = {"cli_path": str(fake_cli), "no_push": True, "grace_s": 0.05}
    options.update(kwargs)
    return LocalAgent(name, repo, tmp_path / "data", **options)  # type: ignore[arg-type]


async def start(runner: LocalAgent) -> str:
    return (
        await runner.create_session("fix code", title="Fix", tags=[], structured_output_schema=OUTPUT_SCHEMA)
    ).session_id


async def finish(runner: LocalAgent, run_id: str) -> SessionState:
    for _ in range(200):
        state = await runner.get_session(run_id)
        if state.status != "working":
            return state
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


@pytest.mark.parametrize("name", ["codex", "claude-code"])
async def test_worktree_result_and_restart(repo: Path, tmp_path: Path, fake_cli: Path, name: str) -> None:
    original = git(repo, "rev-parse", "HEAD")
    runner = agent(repo, tmp_path, fake_cli, name)
    run_id = await start(runner)
    recovered = agent(repo, tmp_path, fake_cli, name)
    state = await finish(recovered, run_id)
    assert state.status == "finished", state.status_detail
    assert state.structured_output == {"summary": "Fixed code", "pr_url": None, "test_result": "passed", "risk": "low"}
    assert state.pull_requests == []
    assert (repo / "code.py").read_text() == "original\n"
    assert git(repo, "rev-parse", "HEAD") == original
    assert git(repo, "status", "--porcelain") == ""
    worktree = tmp_path / "data" / "worktrees" / run_id
    assert (worktree / "code.py").read_text() == "fixed\n"
    assert git(worktree, "status", "--porcelain") == ""
    config = json.loads((tmp_path / "data" / "runs" / run_id / "run.json").read_text())
    assert config["branch"] == f"garage/{run_id}"
    assert config["agent_session_id"] == ("thread-123" if name == "codex" else "session-123")
    flag = "resume" if name == "codex" else "--resume"
    assert f"{shlex.quote(str(fake_cli))} {flag}" in config["takeover_command"]
    assert await recovered.get_session(run_id) == state


@pytest.mark.parametrize("name", ["codex", "claude-code"])
@pytest.mark.parametrize("mode", ["failed", "nonzero", "invalid", "empty"])
async def test_failed_results(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, name: str, mode: str
) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    runner = agent(repo, tmp_path, fake_cli, name)
    state = await finish(runner, await start(runner))
    assert state.status == "failed"
    assert state.structured_output is None
    assert git(repo, "status", "--porcelain") == ""
    if mode == "nonzero":
        assert "stderr: boom: unexpected flag" in (state.status_detail or "")


async def test_codex_nonfatal_error(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "retry")
    runner = agent(repo, tmp_path, fake_cli)
    state = await finish(runner, await start(runner))
    assert state.status == "finished"
    assert state.structured_output is not None
    assert "Stream interrupted; reconnecting" in (state.status_detail or "")


async def test_claude_result_error(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "claude_error")
    runner = agent(repo, tmp_path, fake_cli, "claude-code")
    state = await finish(runner, await start(runner))
    assert state.status == "failed"
    assert "API Error: 401 token expired" in (state.status_detail or "")


async def test_build_leftovers_not_committed(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "artifacts")
    runner = agent(repo, tmp_path, fake_cli)
    run_id = await start(runner)
    assert (await finish(runner, run_id)).status == "finished"
    committed = git(tmp_path / "data" / "worktrees" / run_id, "ls-files").splitlines()
    assert "notes.txt" in committed
    assert not [name for name in committed if "__pycache__" in name or name.endswith((".pyc", ".DS_Store"))]


async def test_timeout_enforced_without_polling(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    runner = agent(repo, tmp_path, fake_cli, timeout_s=0.2)
    run_id = await start(runner)
    await asyncio.sleep(0.6)
    assert (tmp_path / "data" / "runs" / run_id / "expired").exists()
    assert (await finish(agent(repo, tmp_path, fake_cli), run_id)).status == "expired"


async def test_cancel_and_capacity(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    runner = agent(repo, tmp_path, fake_cli, max_concurrent=1)
    run_id = await start(runner)
    recovered = agent(repo, tmp_path, fake_cli, "claude-code", max_concurrent=1)
    with pytest.raises(LocalCapacityError):
        await start(recovered)
    assert len(list((tmp_path / "data" / "worktrees").iterdir())) == 1
    await recovered.cancel_session(run_id)
    assert (await recovered.get_session(run_id)).status == "failed"
    config = json.loads((tmp_path / "data" / "runs" / run_id / "run.json").read_text())
    # No running process survives escalation (a reparented zombie is harmless).
    ps = subprocess.run(["ps", "-g", str(config["pid"]), "-o", "stat="], capture_output=True, text=True)
    assert all(line.strip().startswith("Z") for line in ps.stdout.splitlines())
    monkeypatch.setenv("FAKE_MODE", "ok")
    assert (await finish(recovered, await start(recovered))).status == "finished"


async def test_missing_cli(repo: Path, tmp_path: Path) -> None:
    runner = agent(repo, tmp_path, tmp_path / "missing")
    assert (await finish(runner, await start(runner))).status == "failed"


async def test_non_default_base(repo: Path, tmp_path: Path, fake_cli: Path) -> None:
    git(repo, "branch", "other")
    (repo / "code.py").write_text("uncommitted main\n")
    runner = agent(repo, tmp_path, fake_cli, base_branch="other")
    assert (await finish(runner, await start(runner))).status == "finished"
    assert (repo / "code.py").read_text() == "uncommitted main\n"


@pytest.mark.parametrize("name", ["codex", "claude-code", "devin"])
def test_dispatcher_selection(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GARAGE_GITHUB_TOKEN", "fake")
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("DEVIN_ORG_ID", raising=False)
    settings = replace(settings_from_env(example_manifest()), dispatch=True, data_dir=tmp_path)
    settings.manifest.dispatch.agent = name
    dispatcher = cli.build_dispatcher(settings)
    if name == "devin":
        assert dispatcher is None
    else:
        assert dispatcher is not None
        assert isinstance(dispatcher.agent, LocalAgent)
        assert dispatcher.agent.agent == name


async def test_dispatch_capacity_leaves_issue_queued(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    runner = agent(repo, tmp_path, fake_cli, max_concurrent=1)
    run_id = await start(runner)
    tracker = FakeTracker()
    tracker.issues.append(
        IssueRecord(
            number=1,
            url="https://example.com/1",
            state="open",
            title="Fix",
            body="",
            labels=[READY],
            created_at=datetime.now(timezone.utc),
        )
    )
    dispatcher = Dispatcher(
        tracker,
        runner,
        tmp_path / "ledger.json",
        manifest=settings_from_env(example_manifest()).manifest,
        interval_s=60,
        dry_run=False,
    )
    try:
        assert await dispatcher.poll_once() == [
            "deferred #1: local execution slots are full; will retry on the next poll"
        ]
        assert "Deferred #1: local execution slots are full" in caplog.text
        assert tracker.issues[0].labels == [READY]
        assert not (tmp_path / "ledger.json").exists()
    finally:
        await runner.cancel_session(run_id)


@pytest.mark.parametrize("name", ["codex", "claude-code"])
async def test_dispatch_comments_hide_local_paths(repo: Path, tmp_path: Path, fake_cli: Path, name: str) -> None:
    runner = agent(repo, tmp_path, fake_cli, name)
    tracker = FakeTracker()
    tracker.issues.append(
        IssueRecord(
            number=1,
            url="https://example.com/1",
            state="open",
            title="Fix",
            body="",
            labels=[READY],
            created_at=datetime.now(timezone.utc),
        )
    )
    dispatcher = Dispatcher(
        tracker,
        runner,
        tmp_path / "ledger.json",
        manifest=settings_from_env(example_manifest()).manifest,
        interval_s=60,
        dry_run=False,
    )
    await dispatcher.poll_once()
    record = dispatcher._load_ledger().records[0]
    assert record.session_url.startswith("file://")
    assert (await finish(runner, record.session_id)).status == "finished"
    await dispatcher.poll_once()
    assert len(tracker.comments) == 4
    for _, body in tracker.comments[:2]:
        assert f"local run {record.session_id}, branch garage/{record.session_id}" in body
        assert "file:" not in body
        assert str(tmp_path) not in body
    assert "Attempt 1 failed: no pull request" in tracker.comments[2][1]
    assert "Started a coding-agent fix session: local run" in tracker.comments[3][1]


async def test_publish_commits_pushes_and_reuses_pr(repo: Path, tmp_path: Path, fake_cli: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.check_call(["git", "init", "--bare", str(remote)])
    git(repo, "remote", "add", "origin", str(remote))
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"html_url": "https://github.com/example/repo/pull/1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        github = GitHubIssues("example/repo", "fake", http=http)
        runner = agent(repo, tmp_path, fake_cli, github=github, no_push=False)
        run_id = await start(runner)
        state = await finish(runner, run_id)
        assert state.status == "finished", state.status_detail
        assert state.structured_output is not None
        assert state.structured_output["pr_url"] == "https://github.com/example/repo/pull/1"
        assert state.pull_requests[0].pr_url == state.structured_output["pr_url"]
        assert git(remote, "rev-parse", f"refs/heads/garage/{run_id}")
        assert await runner.get_session(run_id) == state
        assert len(requests) == 2


def test_direct_handoff_without_manifest(
    repo: Path, tmp_path: Path, fake_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARAGE_CODEX_PATH", str(fake_cli))
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path / "direct"))
    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    assert cli.main(["handoff", "--agent", "codex", "--repo-path", str(repo), "--no-push", "fix code"]) == 0
    output = capsys.readouterr().out
    assert "Status: finished" in output
    assert (tmp_path / "direct" / "runs").as_uri() in output
    assert f"{shlex.quote(str(fake_cli))} resume thread-123" in output
    assert '"test_result": "passed"' in output


async def test_detached_cli_survives_parent_exit(
    repo: Path,
    tmp_path: Path,
    fake_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARAGE_CODEX_PATH", str(fake_cli))
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FAKE_MODE", "slow")
    parent = subprocess.run(
        [
            sys.executable,
            "-m",
            "garage.cli",
            "handoff",
            "--agent",
            "codex",
            "--repo-path",
            str(repo),
            "--no-push",
            "--detach",
            "fix code",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert parent.returncode == 0, parent.stderr
    run_id = parent.stdout.split("Run: ")[1].splitlines()[0]
    assert "Status: working" in parent.stdout
    assert (await finish(agent(repo, tmp_path, fake_cli), run_id)).status == "finished"


@pytest.mark.parametrize("existing", [True, False])
async def test_pr_api_existing_or_error(existing: bool) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if existing:
            return httpx.Response(200, json=[{"html_url": "https://example.com/pr/1"}])
        return httpx.Response(403, json={"message": "Forbidden"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        github = GitHubIssues("example/repo", "fake", http=http)
        if existing:
            assert (
                await github.create_pull_request("Fix", "Summary", head="garage/1", base="main")
                == "https://example.com/pr/1"
            )
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await github.create_pull_request("Fix", "Summary", head="garage/1", base="main")


async def test_push_failure_is_not_a_success(repo: Path, tmp_path: Path, fake_cli: Path) -> None:
    runner = agent(repo, tmp_path, fake_cli, no_push=False, github=GitHubIssues("example/repo", "fake"))
    state = await finish(runner, await start(runner))
    assert state.status == "failed"
    assert "publication failed: git push failed" in (state.status_detail or "")
    assert state.structured_output is None
    assert state.pull_requests == []
