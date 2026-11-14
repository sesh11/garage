"""Watch and retry using the shared offline trackers and agents."""

import json
from pathlib import Path

import httpx
import pytest
from conftest import FakeAgent, FakeTracker, FakeLocalAgent, example_manifest
from test_max_open import pr, issue, finish_devin, dispatcher_for

from garage.github import GitHubIssues
from garage.report import BLOCKED, DISPATCHED
from garage.tracker import (
    ATTEMPTS_END,
    ATTEMPTS_START,
    CheckRecord,
    PullRequestRecord,
    pull_request_repo,
    replace_attempt_block,
)
from garage.dispatch import (
    MAX_BODY_CHARS,
    Dispatcher,
    DispatchLedger,
    DispatchRecord,
    attempt_block,
    build_fix_prompt,
)
from garage.projects import load_projects
from garage.settings import settings_from_env
from garage.agents.local import LocalCapacityError


def records(path: Path) -> list[DispatchRecord]:
    return DispatchLedger.load(path / "dispatch.json").records


@pytest.mark.parametrize("suffix", ["", "/files", "?foo=1", "#issuecomment-1"])
def test_pull_request_repo_accepts_url_suffixes(suffix: str) -> None:
    assert pull_request_repo(f"https://github.com/o/r/pull/5{suffix}") == "o/r"


@pytest.mark.asyncio
async def test_partial_retry_write_does_not_reset_attempt_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    finish_devin(agent, "s1", None)
    original = tracker.remove_label

    async def partially_successful(number: int, label: str) -> None:
        await original(number, label)
        if label == DISPATCHED:
            raise RuntimeError("response lost after removing label")

    monkeypatch.setattr(tracker, "remove_label", partially_successful)
    await dispatcher.poll_once()
    assert records(tmp_path)[-1].attempt == 2
    assert len(agent.created) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_failed_superseded_pr_close_does_not_interrupt_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()
    if failed:
        tracker.checks[pr(10)] = [CheckRecord(name="test", completed=True, conclusion="failure")]
    else:
        tracker.issues[0].labels.remove(DISPATCHED)
    original = tracker.close_pull_request
    calls = 0

    async def flaky(url: str, comment: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("close failed")
        await original(url, comment)

    monkeypatch.setattr(tracker, "close_pull_request", flaky)
    await dispatcher.poll_once()
    old, new = records(tmp_path)
    assert old.outcome == ("failed" if failed else "superseded")
    assert old.pr_state == "open"
    assert new.labeled and DISPATCHED in tracker.issues[0].labels
    await dispatcher.poll_once()
    assert records(tmp_path)[0].pr_state == "closed"
    assert tracker.pull_requests[pr(10)].state == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_failed_manual_replacement_preserves_previous_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deferred: bool
) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()
    tracker.issues[0].labels.remove(DISPATCHED)

    async def broken(*args: object, **kwargs: object) -> None:
        raise LocalCapacityError("full") if deferred else RuntimeError("start failed")

    monkeypatch.setattr(agent, "create_session", broken)
    await dispatcher.poll_once()
    assert len(records(tmp_path)) == 1
    assert records(tmp_path)[0].outcome is None
    assert records(tmp_path)[0].in_flight
    assert tracker.pull_requests[pr(10)].state == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update_issue", "remove_label"])
async def test_failed_retry_write_is_retried_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    finish_devin(agent, "s1", None)
    await dispatcher._follow_up(dispatcher._load_ledger())
    original = getattr(tracker, operation)

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(tracker, operation, broken)
    await dispatcher.poll_once()
    assert records(tmp_path)[0].outcome is None
    assert DISPATCHED in tracker.issues[0].labels
    monkeypatch.setattr(tracker, operation, original)
    await dispatcher.poll_once()
    assert records(tmp_path)[0].outcome == "failed"
    assert records(tmp_path)[-1].attempt == 2
    assert tracker.issues[0].body.count(ATTEMPTS_START) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [False, True])
async def test_retry_blocking_and_manual_fresh_attempt(tmp_path: Path, local: bool) -> None:
    tracker = FakeTracker()
    tracker.issues.append(issue(1))
    agent = FakeLocalAgent() if local else FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent, max_attempts=2)
    await dispatcher.poll_once()
    for attempt in (1, 2):
        url = pr(10 + attempt)
        tracker.open_pull_request(url)
        tracker.checks[url] = [CheckRecord(name="lint", completed=True, conclusion="failure")]
        if isinstance(agent, FakeLocalAgent):
            agent.finish(f"s{attempt}", url)
        else:
            finish_devin(agent, f"s{attempt}", url)
        await dispatcher.poll_once()
        if attempt == 1:
            assert len(agent.created) == 2
            assert records(tmp_path)[0].outcome == "failed"
            assert tracker.pull_requests[url].state == "closed"
            assert ATTEMPTS_START in agent.created[1][0]
            assert "open a NEW pull request" in agent.created[1][0]
            assert "lint" in tracker.issues[0].body
    assert records(tmp_path)[1].outcome == "blocked"
    assert BLOCKED in tracker.issues[0].labels
    assert DISPATCHED not in tracker.issues[0].labels
    count = len(tracker.comments)
    for _ in range(3):
        await dispatcher.poll_once()
    assert len(tracker.comments) == count
    assert len(agent.created) == 2
    assert sum("Remove `garage:blocked`" in text for _, text in tracker.comments) == 1
    await tracker.remove_label(1, BLOCKED)
    await dispatcher.poll_once()
    assert len(agent.created) == 3
    assert records(tmp_path)[-1].attempt == 1
    assert tracker.pull_requests[pr(12)].state == "closed"


@pytest.mark.asyncio
async def test_merge_after_restart_closes_issue_and_frees_cap(tmp_path: Path) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues += [issue(1), issue(2)]
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()
    assert records(tmp_path)[0].outcome is None
    tracker.pull_requests[pr(10)].state = "merged"
    await dispatcher_for(tmp_path, tracker, agent).poll_once()
    assert records(tmp_path)[0].outcome == "merged"
    assert tracker.issues[0].state == "closed"
    assert tracker.issues[0].state_reason == "completed"
    assert any(pr(10) in text and "Closing" in text for _, text in tracker.comments)
    assert len(agent.created) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checks",
    [
        [],
        [CheckRecord(name="test", completed=False)],
        [CheckRecord(name="test", completed=True, conclusion="success")],
    ],
)
async def test_open_pr_keeps_watching_without_mutations(tmp_path: Path, checks: list[CheckRecord]) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    tracker.checks[pr(10)] = checks
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()
    before = tracker.mutations.copy()
    for _ in range(3):
        await dispatcher.poll_once()
    assert tracker.mutations == before
    assert records(tmp_path)[0].outcome is None
    tracker.pull_requests[pr(10)].state = "closed"
    await dispatcher.poll_once()
    assert records(tmp_path)[0].failure == "pull request closed without merging"
    assert len(agent.created) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_no_pr_fails_and_closed_issue_does_not_retry(tmp_path: Path, closed: bool) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    finish_devin(agent, "s1", None)
    if closed:
        tracker.issues[0].state = "closed"
    await dispatcher.poll_once()
    assert records(tmp_path)[0].failure == "no pull request"
    assert records(tmp_path)[0].outcome == "failed"
    assert len(agent.created) == (1 if closed else 2)


def test_block_replaced_bounded_and_survives_prompt_cut() -> None:
    record = DispatchLedger.model_validate(
        {
            "records": [
                {
                    "issue": 1,
                    "session_id": "s",
                    "session_url": "url",
                    "started_at": "2026-01-01T00:00:00Z",
                    "attempt": 1,
                    "failure": "lint",
                    "summary": "long " * 3000,
                }
            ]
        }
    ).records[0]
    block = attempt_block(record, 2)
    assert len(block) < 1500
    body = replace_attempt_block("original\n" * MAX_BODY_CHARS, block)
    record.summary = "replacement"
    body = replace_attempt_block(body, attempt_block(record, 2))
    assert body.count(ATTEMPTS_START) == body.count(ATTEMPTS_END) == 1
    assert body.startswith(ATTEMPTS_START)
    found = issue(1)
    found.body = body
    prompt = build_fix_prompt(found, "o/r", settings_from_env(example_manifest()).manifest)
    assert "replacement" in prompt and ATTEMPTS_END in prompt
    assert "[... truncated]" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("done", [False, True])
async def test_legacy_ledger_only_watches_previously_running_sessions(tmp_path: Path, done: bool) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    data = json.loads((tmp_path / "dispatch.json").read_text())
    for key in ("attempt", "outcome", "failure", "summary"):
        data["records"][0].pop(key)
    data["records"][0]["done"] = done
    (tmp_path / "dispatch.json").write_text(json.dumps(data))
    finish_devin(agent, "s1", None)
    await dispatcher.poll_once()
    assert records(tmp_path)[0].attempt == (0 if done else 1)
    assert records(tmp_path)[0].outcome == (None if done else "failed")
    assert len(agent.created) == (1 if done else 2)


@pytest.mark.asyncio
async def test_read_errors_do_not_judge_attempt(tmp_path: Path) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))

    async def broken(pull: PullRequestRecord) -> list[CheckRecord]:
        raise RuntimeError("unavailable")

    tracker.list_checks = broken  # type: ignore[method-assign]
    await dispatcher.poll_once()
    assert records(tmp_path)[0].outcome is None
    assert len(agent.created) == 1


def test_settings_projects_precedence_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "garage.toml"
    manifest.write_text(example_manifest().read_text().replace('interval = "1h"', 'interval = "1h"\nmax_attempts = 3'))
    projects = tmp_path / "projects.toml"
    projects.write_text(
        f'[[project]]\nname="a"\nrepo="o/a"\nmanifest="{manifest}"\nmax_attempts=1\n[[project]]\nname="b"\nrepo="o/b"\nmanifest="{manifest}"\n'
    )
    assert [item.max_attempts for item in load_projects(projects)] == [1, 3]
    assert settings_from_env(example_manifest()).max_attempts == 5
    monkeypatch.setenv("GARAGE_MAX_ATTEMPTS", "4")
    assert {item.max_attempts for item in load_projects(projects)} == {4}
    monkeypatch.setenv("GARAGE_MAX_ATTEMPTS", "0")
    with pytest.raises(ValueError, match="GARAGE_MAX_ATTEMPTS"):
        settings_from_env(example_manifest())
    monkeypatch.delenv("GARAGE_MAX_ATTEMPTS")
    with pytest.raises(ValueError, match="max_attempts"):
        dispatcher_for(tmp_path, FakeTracker(), FakeAgent(), max_attempts=0)


@pytest.mark.asyncio
async def test_projects_retry_limits_are_independent(tmp_path: Path) -> None:
    projects = tmp_path / "projects.toml"
    projects.write_text(
        '[[project]]\nname="a"\nrepo="o/a"\nmax_attempts=1\n[[project]]\nname="b"\nrepo="o/b"\nmax_attempts=3\n'
    )
    dispatchers = []
    for settings in load_projects(projects):
        tracker, agent = FakeTracker(settings.repo), FakeAgent()
        tracker.issues.append(issue(1, settings.repo))
        dispatcher = Dispatcher(
            tracker,
            agent,
            tmp_path / settings.project / "dispatch.json",
            manifest=settings.manifest,
            interval_s=60,
            dry_run=False,
            project=settings.project,
            max_attempts=settings.max_attempts,
        )
        dispatchers.append((dispatcher, tracker, agent))
        await dispatcher.poll_once()
        finish_devin(agent, "s1", None)
    for dispatcher, _, _ in dispatchers:
        await dispatcher.poll_once()
    assert BLOCKED in dispatchers[0][1].issues[0].labels
    assert len(dispatchers[0][2].created) == 1
    assert len(dispatchers[1][2].created) == 2
    assert records(tmp_path / "a")[0].outcome == "blocked"
    assert records(tmp_path / "b")[-1].attempt == 2


@pytest.mark.asyncio
async def test_github_checks_pagination_statuses_and_close() -> None:
    calls: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/check-runs"):
            if request.url.params.get("page") == "2":
                return httpx.Response(
                    200, json={"check_runs": [{"name": "skipped", "status": "completed", "conclusion": "skipped"}]}
                )
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"name": "test", "status": "completed", "conclusion": "timed_out"},
                        {"name": "lint", "status": "in_progress", "conclusion": None},
                    ]
                },
                headers={
                    "link": '<https://api.github.com/repos/other/repo/commits/abc/check-runs?page=2>; rel="next"'
                },
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "statuses": [
                        {"context": value, "state": value} for value in ("pending", "success", "failure", "error")
                    ]
                },
            )
        assert request.url.path in ("/repos/other/repo/issues/1/comments", "/repos/other/repo/pulls/1")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        github = GitHubIssues("o/r", "fake", http=http)
        pull = PullRequestRecord(url="https://github.com/other/repo/pull/1", number=1, state="open", head_sha="abc")
        checks = await github.list_checks(pull)
        assert [check.name for check in checks if check.failed] == ["test", "failure", "error"]
        assert not checks[1].completed and not checks[3].completed
        assert await github.list_checks(pull.model_copy(update={"head_sha": None})) == []
        with pytest.raises(ValueError, match="Invalid pull request URL"):
            await github.close_pull_request(pull.url, "Superseded.")
        github.repo = "OTHER/REPO"
        await github.close_pull_request(pull.url, "Superseded.")
        assert calls[-2:] == [("POST", "/repos/other/repo/issues/1/comments"), ("PATCH", "/repos/other/repo/pulls/1")]


@pytest.mark.parametrize(
    "conclusion", ["failure", "timed_out", "cancelled", "action_required", "startup_failure", "error"]
)
def test_failed_conclusions(conclusion: str) -> None:
    assert CheckRecord(name="test", completed=True, conclusion=conclusion).failed


@pytest.mark.asyncio
@pytest.mark.parametrize("same_repo", [False, True])
async def test_dispatch_only_closes_own_repository_prs(tmp_path: Path, same_repo: bool) -> None:
    tracker, agent = FakeTracker(), FakeAgent()
    tracker.issues.append(issue(1))
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    url = pr(10, tracker.repo.upper() if same_repo else "someone/else")
    tracker.open_pull_request(url)
    finish_devin(agent, "s1", url)
    await dispatcher.poll_once()
    tracker.issues[0].labels.remove(DISPATCHED)
    await dispatcher.poll_once()
    assert tracker.pull_requests[url].state == ("closed" if same_repo else "open")
    assert records(tmp_path)[0].pr_state == ("closed" if same_repo else "open")
