"""Per-project cap on issues in flight (``max_open``)."""

from typing import Any
from pathlib import Path
from datetime import datetime, timezone

import httpx
import pytest
from conftest import FakeAgent, FakeTracker, FakeLocalAgent, example_manifest

from garage.github import GitHubIssues
from garage.report import READY, DISPATCHED
from garage.tracker import IssueRecord
from garage.dispatch import Dispatcher, DispatchLedger
from garage.projects import load_projects
from garage.settings import GarageSettings, settings_from_env
from garage.agents.base import SessionState


REPO = "sesh11/ambient-home"


def issue(number: int, repo: str = REPO) -> IssueRecord:
    return IssueRecord(
        number=number,
        url=f"https://github.com/{repo}/issues/{number}",
        state="open",
        title=f"Issue {number}",
        body="",
        labels=[READY],
        created_at=datetime.now(timezone.utc),
    )


def pr(number: int, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def finish_devin(agent: FakeAgent, session_id: str, pr_url: str | None) -> None:
    agent.states[session_id] = SessionState.model_validate(
        {
            "session_id": session_id,
            "status": "exit",
            "structured_output": {"summary": "Done.", "pr_url": pr_url, "test_result": "passed", "risk": "low"},
            "pull_requests": [{"pr_url": pr_url}] if pr_url else [],
        }
    )


def dispatcher_for(tmp_path: Path, tracker: FakeTracker, agent: FakeAgent, **options: Any) -> Dispatcher:
    return Dispatcher(
        tracker,
        agent,
        tmp_path / "dispatch.json",
        manifest=settings_from_env(example_manifest()).manifest,
        interval_s=60,
        dry_run=False,
        **options,
    )


@pytest.mark.asyncio
async def test_cap_one_waits_for_the_session_then_the_pull_request(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(2), issue(1)]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)

    actions = await dispatcher.poll_once()
    assert actions[0] == "started https://app.devin.ai/sessions/s1 for #1: Issue 1"
    assert actions[1] == "hold #2: waiting for a free slot: 1 of 1 in flight (#1)"
    assert await dispatcher.poll_once() == ["hold #2: waiting for a free slot: 1 of 1 in flight (#1)"]

    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))
    actions = await dispatcher.poll_once()
    assert actions[-1] == "hold #2: waiting for a free slot: 1 of 1 in flight (#1)"
    assert DispatchLedger.load(tmp_path / "dispatch.json").records[0].pr_state == "open"
    assert len(agent.created) == 1

    tracker.pull_requests[pr(10)].state = "merged"
    assert await dispatcher.poll_once() == ["started https://app.devin.ai/sessions/s2 for #2: Issue 2"]
    assert DispatchLedger.load(tmp_path / "dispatch.json").records[0].pr_state == "merged"


@pytest.mark.asyncio
async def test_merged_pull_request_is_not_read_again(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues.append(issue(1))
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10)).state = "merged"
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()
    await dispatcher.poll_once()
    assert tracker.calls.count(f"get_pull_request:{pr(10)}") == 1


@pytest.mark.asyncio
async def test_earlier_attempt_with_an_open_pull_request_keeps_the_slot(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2)]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()

    tracker.issues[0].labels.remove(DISPATCHED)
    assert (await dispatcher.poll_once())[0] == "started https://app.devin.ai/sessions/s2 for #1: Issue 1"
    finish_devin(agent, "s2", None)
    assert (await dispatcher.poll_once())[-1] == "hold #2: waiting for a free slot: 1 of 1 in flight (#1)"
    tracker.pull_requests[pr(10)].state = "closed"
    assert (await dispatcher.poll_once())[-1] == "started https://app.devin.ai/sessions/s3 for #2: Issue 2"


@pytest.mark.asyncio
async def test_cap_two_starts_two_and_holds_the_third(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(3), issue(1), issue(2)]
    dispatcher = dispatcher_for(tmp_path, tracker, FakeAgent(), max_open=2)

    actions = await dispatcher.poll_once()
    assert [action.split(" for ")[-1] for action in actions[:2]] == ["#1: Issue 1", "#2: Issue 2"]
    assert actions[2] == "hold #3: waiting for a free slot: 2 of 2 in flight (#1, #2)"


@pytest.mark.asyncio
async def test_dry_run_counts_the_starts_it_plans(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2), issue(3)]
    dispatcher = dispatcher_for(tmp_path, tracker, FakeAgent(), max_open=2)
    dispatcher.dry_run = True
    assert await dispatcher.poll_once() == [
        "start session for #1: Issue 1",
        "start session for #2: Issue 2",
        "hold #3: waiting for a free slot: 2 of 2 in flight (#1, #2)",
    ]


@pytest.mark.asyncio
async def test_dry_run_reads_pull_requests_without_saving(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2)]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    tracker.open_pull_request(pr(10))
    finish_devin(agent, "s1", pr(10))
    await dispatcher.poll_once()
    ledger = tmp_path / "dispatch.json"
    before = ledger.read_bytes()

    tracker.pull_requests[pr(10)].state = "merged"
    dispatcher.dry_run = True
    assert await dispatcher.poll_once() == ["start session for #2: Issue 2"]
    assert ledger.read_bytes() == before
    assert DispatchLedger.load(ledger).records[0].pr_state == "open"


@pytest.mark.asyncio
async def test_session_without_a_pull_request_frees_its_slot(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2)]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    finish_devin(agent, "s1", None)
    assert (await dispatcher.poll_once())[-1] == "started https://app.devin.ai/sessions/s2 for #2: Issue 2"


@pytest.mark.asyncio
async def test_unreadable_pull_request_keeps_its_slot(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2)]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)
    await dispatcher.poll_once()
    finish_devin(agent, "s1", pr(10))

    async def broken(url: str) -> None:
        raise RuntimeError("GitHub is down")

    tracker.get_pull_request = broken  # type: ignore[method-assign]
    assert (await dispatcher.poll_once())[-1] == "hold #2: waiting for a free slot: 1 of 1 in flight (#1)"


@pytest.mark.asyncio
async def test_local_agent_pull_request_holds_until_closed(tmp_path: Path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2)]
    agent = FakeLocalAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)

    assert (await dispatcher.poll_once())[0] == "started file:///runs/s1 for #1: Issue 1"
    tracker.open_pull_request(pr(10))
    agent.finish("s1", pr(10))
    assert (await dispatcher.poll_once())[-1] == "hold #2: waiting for a free slot: 1 of 1 in flight (#1)"
    tracker.pull_requests[pr(10)].state = "closed"
    assert await dispatcher.poll_once() == ["started file:///runs/s2 for #2: Issue 2"]


def _projects_file(tmp_path: Path) -> list[GarageSettings]:
    manifest = tmp_path / "ambient.toml"
    manifest.write_text(
        example_manifest().read_text(encoding="utf-8").replace('interval = "1h"', 'interval = "1h"\nmax_open = 3'),
        encoding="utf-8",
    )
    projects = tmp_path / "projects.toml"
    projects.write_text(
        '[[project]]\nname = "garage"\nrepo = "sesh11/garage"\n\n'
        f'[[project]]\nname = "ambient-home"\nmanifest = "{manifest}"\nmax_open = 2\n\n'
        f'[[project]]\nname = "other"\nrepo = "sesh11/other"\nmanifest = "{manifest}"\n',
        encoding="utf-8",
    )
    return load_projects(projects)


def test_projects_file_max_open_wins_over_the_manifest(tmp_path: Path) -> None:
    garage, ambient, manifest_only = _projects_file(tmp_path)
    assert garage.max_open == 1
    assert ambient.max_open == 2
    assert manifest_only.max_open == 3


def test_max_open_env_override_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings_from_env(example_manifest()).max_open == 1
    monkeypatch.setenv("GARAGE_MAX_OPEN", "4")
    assert settings_from_env(example_manifest()).max_open == 4
    assert {settings.max_open for settings in _projects_file(tmp_path)} == {4}
    monkeypatch.setenv("GARAGE_MAX_OPEN", "0")
    with pytest.raises(ValueError, match="GARAGE_MAX_OPEN"):
        settings_from_env(example_manifest())


@pytest.mark.asyncio
async def test_projects_keep_their_own_caps(tmp_path: Path) -> None:
    dispatchers = []
    trackers = []
    for settings in _projects_file(tmp_path)[:2]:
        tracker = FakeTracker(settings.repo)
        tracker.issues += [issue(1, settings.repo), issue(2, settings.repo), issue(3, settings.repo)]
        trackers.append(tracker)
        dispatchers.append(
            Dispatcher(
                tracker,
                FakeAgent(),
                tmp_path / settings.project / "dispatch.json",
                manifest=settings.manifest,
                interval_s=60,
                dry_run=False,
                project=settings.project,
                max_open=settings.max_open,
            )
        )

    garage, ambient = [await dispatcher.poll_once() for dispatcher in dispatchers]
    assert garage[1:] == [
        "hold #2: waiting for a free slot: 1 of 1 in flight in project garage (#1)",
        "hold #3: waiting for a free slot: 1 of 1 in flight in project garage (#1)",
    ]
    assert ambient[2] == "hold #3: waiting for a free slot: 2 of 2 in flight in project ambient-home (#1, #2)"
    assert len(DispatchLedger.load(tmp_path / "garage" / "dispatch.json").records) == 1
    assert len(DispatchLedger.load(tmp_path / "ambient-home" / "dispatch.json").records) == 2


def test_max_open_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_open"):
        dispatcher_for(tmp_path, FakeTracker(), FakeAgent(), max_open=0)


@pytest.mark.asyncio
async def test_github_get_pull_request_reads_state_from_url() -> None:
    pulls = {
        "/repos/o/r/pulls/1": {"number": 1, "state": "open", "merged": False, "head": {"sha": "abc"}},
        "/repos/o/r/pulls/2": {"number": 2, "state": "closed", "merged": True, "merged_at": "2026-11-01T00:00:00Z"},
        "/repos/o/r/pulls/3": {"number": 3, "state": "closed", "merged": False, "merged_at": None},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        body = pulls.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={**body, "html_url": f"https://github.com/o/r/pull/{body['number']}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        github = GitHubIssues("o/r", "fake", http=http)
        opened = await github.get_pull_request("https://github.com/o/r/pull/1")
        assert opened is not None and (opened.state, opened.head_sha) == ("open", "abc")
        assert (await github.get_pull_request("https://github.com/o/r/pull/2")).state == "merged"  # type: ignore[union-attr]
        assert (await github.get_pull_request("https://github.com/o/r/pull/3/files")).state == "closed"  # type: ignore[union-attr]
        assert await github.get_pull_request("https://github.com/o/r/pull/4") is None
        assert await github.get_pull_request("file:///runs/s1") is None
