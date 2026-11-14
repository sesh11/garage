# mypy: disable-error-code="no-untyped-def"

from datetime import datetime, timezone

import httpx
import pytest
from conftest import FakeAgent, FakeTracker, FakeLocalAgent, example_manifest

from garage.github import GitHubIssues
from garage.report import READY, DISPATCHED
from garage.tracker import IssueRecord
from garage.dispatch import Dispatcher, DispatchLedger
from garage.projects import load_projects
from garage.settings import settings_from_env
from garage.dependencies import Dependency, parse_dependencies


REPO = "sesh11/ambient-home"


def issue(
    number: int,
    body: str = "",
    *,
    state: str = "open",
    reason: str | None = None,
    labels: list[str] | None = None,
    repo: str = REPO,
) -> IssueRecord:
    return IssueRecord(
        number=number,
        url=f"https://github.com/{repo}/issues/{number}",
        state=state,
        state_reason=reason,
        title=f"Issue {number}",
        body=body,
        labels=labels if labels is not None else [],
        created_at=datetime.now(timezone.utc),
    )


def close(tracker: FakeTracker, number: int, reason: str = "completed") -> None:
    found = next(item for item in tracker.issues if item.number == number)
    found.state = "closed"
    found.state_reason = reason


def dispatcher_for(tmp_path, tracker: FakeTracker, agent: FakeAgent | None = None, **options) -> Dispatcher:
    return Dispatcher(
        tracker,
        agent or FakeAgent(),
        tmp_path / "dispatch.json",
        manifest=settings_from_env(example_manifest()).manifest,
        interval_s=60,
        dry_run=False,
        **options,
    )


def test_parse_dependencies_forms() -> None:
    body = (
        "Intro text.\n"
        "Depends on #12\n"
        "depends on: #15, #16 and #12\n"
        "DEPENDS ON sesh11/other#3\n"
        "This also depends on https://github.com/sesh11/ambient-home/issues/20.\n"
        "Mentions #99 without the phrase.\n"
        "Depends on the network.\n"
    )
    assert parse_dependencies(body) == [
        Dependency(12),
        Dependency(15),
        Dependency(16),
        Dependency(3, "sesh11/other"),
        Dependency(20, "sesh11/ambient-home"),
    ]
    assert parse_dependencies("No order here. See #4.") == []


@pytest.mark.asyncio
async def test_open_dependency_holds_then_starts_after_it_closes(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2, "Depends on #1", labels=[READY])]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)

    assert await dispatcher.poll_once() == ["hold #2: waiting on #1 (still open)"]
    assert not agent.created
    assert tracker.comments == [
        (
            2,
            "Not starting yet: waiting on #1 (still open).\n\n"
            "Garage starts it once every issue named on a `Depends on` line is closed as completed.",
        )
    ]
    assert await dispatcher.poll_once() == ["hold #2: waiting on #1 (still open)"]
    assert len(tracker.comments) == 1

    close(tracker, 1)
    actions = await dispatcher.poll_once()
    assert actions and actions[0].startswith("started ")
    assert len(agent.created) == 1
    assert DISPATCHED in tracker.issues[1].labels
    assert DispatchLedger.load(tmp_path / "dispatch.json").dependency_holds == {}


@pytest.mark.asyncio
async def test_two_dependencies_hold_until_both_close(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2), issue(3, "Depends on: #1, #2", labels=[READY])]
    agent = FakeAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)

    assert await dispatcher.poll_once() == ["hold #3: waiting on #1, #2 (still open)"]
    close(tracker, 1)
    assert await dispatcher.poll_once() == ["hold #3: waiting on #2 (still open)"]
    assert len(tracker.comments) == 1
    close(tracker, 2)
    assert (await dispatcher.poll_once())[0].startswith("started ")
    assert len(agent.created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issues", "body", "reason"),
    [
        ([issue(1, state="closed", reason="not_planned")], "Depends on #1", "dependency #1 was closed as not planned"),
        ([], "Depends on #41", "dependency #41 does not exist"),
        ([], "Depends on sesh11/garage#1", "depends on sesh11/garage#1, but only issues in sesh11/ambient-home"),
        ([], "Depends on #9", "depends on itself"),
        ([issue(5, "Depends on #9")], "Depends on #5", "dependency cycle #9 -> #5 -> #9"),
        (
            [issue(5, "Depends on #6"), issue(6, "Depends on #9")],
            "Depends on #5",
            "dependency cycle #9 -> #5 -> #6 -> #9",
        ),
    ],
    ids=["declined", "unknown", "other-repo", "self", "cycle", "long-cycle"],
)
async def test_bad_dependencies_hold_with_a_reason(tmp_path, issues, body, reason) -> None:
    tracker = FakeTracker()
    tracker.issues += [*issues, issue(9, body, labels=[READY])]
    agent = FakeAgent()
    actions = await dispatcher_for(tmp_path, tracker, agent).poll_once()
    assert len(actions) == 1 and actions[0].startswith(f"hold #9: {reason}")
    assert not agent.created
    assert len(tracker.comments) == 1 and reason in tracker.comments[0][1]


@pytest.mark.asyncio
async def test_closed_with_no_reason_counts_as_completed(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1, state="closed"), issue(2, "Depends on #1", labels=[READY])]
    assert (await dispatcher_for(tmp_path, tracker).poll_once())[0].startswith("started ")


@pytest.mark.asyncio
async def test_no_depends_on_line_behaves_as_before(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues.append(issue(1, "The service is failing. See #4.", labels=[READY]))
    agent = FakeAgent()
    actions = await dispatcher_for(tmp_path, tracker, agent).poll_once()
    assert actions == ["started https://app.devin.ai/sessions/s1 for #1: Issue 1"]
    assert not [call for call in tracker.calls if call.startswith("get_issue")]
    assert len(tracker.comments) == 1 and "factory-session: s1" in tracker.comments[0][1]


@pytest.mark.asyncio
async def test_each_dependency_is_read_once_per_poll(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [
        issue(1, "Depends on #2"),
        issue(2),
        issue(3, "Depends on #1", labels=[READY]),
        issue(4, "Depends on #1, #2", labels=[READY]),
    ]
    actions = await dispatcher_for(tmp_path, tracker).poll_once()
    assert actions == ["hold #3: waiting on #1 (still open)", "hold #4: waiting on #1, #2 (still open)"]
    lookups = [call for call in tracker.calls if call.startswith("get_issue")]
    assert sorted(lookups) == ["get_issue:1", "get_issue:2"]


@pytest.mark.asyncio
async def test_dry_run_reports_hold_without_commenting(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2, "Depends on #1", labels=[READY])]
    dispatcher = dispatcher_for(tmp_path, tracker)
    dispatcher.dry_run = True
    assert await dispatcher.poll_once() == ["hold #2: waiting on #1 (still open)"]
    assert not tracker.mutations


@pytest.mark.asyncio
async def test_hold_comment_is_not_repeated_after_restart(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1), issue(2, "Depends on #1", labels=[READY])]
    await dispatcher_for(tmp_path, tracker).poll_once()
    await dispatcher_for(tmp_path, tracker).poll_once()
    assert len(tracker.comments) == 1


@pytest.mark.asyncio
async def test_order_works_with_a_local_agent(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.issues += [issue(1, labels=[READY]), issue(2, "Depends on #1", labels=[READY])]
    agent = FakeLocalAgent()
    dispatcher = dispatcher_for(tmp_path, tracker, agent)

    actions = await dispatcher.poll_once()
    assert actions[0] == "started file:///runs/s1 for #1: Issue 1"
    assert actions[1] == "hold #2: waiting on #1 (still open)"
    tracker.open_pull_request(f"https://github.com/{REPO}/pull/10")
    agent.finish("s1", f"https://github.com/{REPO}/pull/10")
    await dispatcher.poll_once()
    assert len(agent.created) == 1
    close(tracker, 1)
    tracker.pull_requests[f"https://github.com/{REPO}/pull/10"].state = "merged"
    assert (await dispatcher.poll_once())[0] == "started file:///runs/s2 for #2: Issue 2"


@pytest.mark.asyncio
async def test_projects_keep_their_own_order(tmp_path) -> None:
    projects = tmp_path / "projects.toml"
    projects.write_text(
        '[[project]]\nname = "garage"\nrepo = "sesh11/garage"\n\n'
        '[[project]]\nname = "ambient-home"\nrepo = "sesh11/ambient-home"\n',
        encoding="utf-8",
    )
    dispatchers = []
    trackers = []
    for settings in load_projects(projects):
        tracker = FakeTracker(settings.repo)
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
            )
        )
    garage, ambient = trackers
    garage.issues += [issue(1, repo=garage.repo), issue(2, "Depends on #1", labels=[READY], repo=garage.repo)]
    ambient.issues += [
        issue(1, state="closed", repo=ambient.repo),
        issue(2, "Depends on #1", labels=[READY], repo=ambient.repo),
    ]

    assert await dispatchers[0].poll_once() == ["hold #2: waiting on #1 (still open)"]
    assert (await dispatchers[1].poll_once())[0].startswith("started ")
    assert DispatchLedger.load(tmp_path / "garage" / "dispatch.json").dependency_holds == {
        2: "waiting on #1 (still open)"
    }
    assert DispatchLedger.load(tmp_path / "ambient-home" / "dispatch.json").dependency_holds == {}


@pytest.mark.asyncio
async def test_github_get_issue_reads_one_issue_or_none() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path.endswith("/issues/7"):
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "html_url": "https://github.com/o/r/issues/7",
                    "state": "closed",
                    "state_reason": "not_planned",
                    "title": "Seven",
                    "body": None,
                    "labels": [],
                    "created_at": "2026-11-01T00:00:00Z",
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        github = GitHubIssues("o/r", "fake", http=http)
        found = await github.get_issue(7)
        assert found is not None and found.state_reason == "not_planned"
        assert await github.get_issue(8) is None
