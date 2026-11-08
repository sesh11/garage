# mypy: disable-error-code="no-untyped-def"
"""The approval gate: new issues wait as proposed; GitHub, the dashboard and the CLI decide."""

import json
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
import pytest
from conftest import FakeAgent, FakeTracker, example_manifest
from fastapi.testclient import TestClient

from garage.ui import create_app
from garage.store import Store
from garage.github import GitHubIssues
from garage.report import (
    READY,
    FIXING,
    REQUEST,
    INCIDENT,
    OBSERVED,
    PROPOSED,
    DISPATCHED,
    RequestLogger,
    IncidentReporter,
)
from garage.tracker import IssueRecord
from garage_sdk.api import IssueLink, ItemState, ClientKind, SubmitWork
from garage.approval import Approvals, IssueWatcher, ApprovalError, ApprovalTarget, dispatch_hold, initial_state
from garage.dispatch import Dispatcher
from garage.requests import Request
from garage.settings import settings_from_env


REPO = "sesh11/ambient-home"
START = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def issue(
    number: int, labels: list[str] | None = None, *, state: str = "open", age: timedelta = timedelta(0), **extra
):
    created = START + timedelta(minutes=5) - age
    return IssueRecord(
        number=number,
        url=f"https://github.com/{REPO}/issues/{number}",
        state=state,
        title=f"Issue {number}",
        body="Please fix the thing.",
        labels=list(labels or []),
        created_at=created,
        updated_at=created,
        author="sesh11",
        **extra,
    )


def on_github(tracker: FakeTracker, number: int, **changes) -> None:
    """Simulate a change made by a person on github.com."""
    for index, existing in enumerate(tracker.issues):
        if existing.number == number:
            tracker.issues[index] = existing.model_copy(update={**changes, "updated_at": datetime.now(timezone.utc)})


@pytest.fixture
def setup(tmp_path):
    tracker = FakeTracker(REPO)
    store = Store(tmp_path / "garage.db")
    wakes: list[str] = []
    watcher = IssueWatcher(
        store,
        tracker,
        tmp_path / "intake.json",
        project="ambient-home",
        on_approved=lambda: wakes.append("wake"),
        clock=lambda: START,
    )
    approvals = Approvals(
        store, [ApprovalTarget("ambient-home", REPO, tracker, on_approved=lambda: wakes.append("wake"))]
    )
    return tracker, store, watcher, approvals, wakes


@pytest.mark.asyncio
async def test_new_human_issue_becomes_proposed_and_ignores_what_garage_does_not_own(setup) -> None:
    tracker, store, watcher, _, _ = setup
    tracker.issues = [
        issue(1),
        issue(2, pull_request=True),
        issue(3, state="closed"),
        issue(4, age=timedelta(days=2)),
        issue(5, [READY, DISPATCHED, FIXING]),
        issue(6, [INCIDENT, OBSERVED]),
    ]
    actions = await watcher.poll_once()
    assert len(actions) == 1 and actions[0].startswith("take in #1 Issue 1 as awaiting_approval")
    [item] = store.items()
    assert (item.state, item.project, item.issue.number, item.trust) == (
        ItemState.awaiting_approval,
        "ambient-home",
        1,
        ClientKind.public,
    )
    assert PROPOSED in tracker.issues[0].labels and READY not in tracker.issues[0].labels
    assert f"garage approve {item.id}" in tracker.comments[0][1]
    assert [issue.labels for issue in tracker.issues[1:]] == [
        [],
        [],
        [],
        [READY, DISPATCHED, FIXING],
        [INCIDENT, OBSERVED],
    ]
    assert await watcher.poll_once() == []
    assert len(tracker.comments) == 1
    assert json.loads((watcher.cursor_path).read_text())["baseline"].startswith("2026-11-08T12:00")


@pytest.mark.asyncio
async def test_github_label_approves_close_declines_and_reopen_asks_again(setup) -> None:
    tracker, store, watcher, _, wakes = setup
    tracker.issues = [issue(1), issue(2)]
    await watcher.poll_once()
    on_github(tracker, 1, labels=[*tracker.issues[0].labels, READY])
    on_github(tracker, 2, state="closed", state_reason="not_planned")
    actions = await watcher.poll_once()
    assert "#1 approved on GitHub: awaiting_approval -> approved" in actions
    assert "#2 closed on GitHub: awaiting_approval -> declined" in actions
    first, second = (store.by_issue(REPO, n) for n in (1, 2))
    assert (first.state, second.state) == (ItemState.approved, ItemState.declined)
    assert tracker.issues[0].labels == [READY] and wakes == ["wake"]
    on_github(tracker, 2, state="open", state_reason=None)
    assert "#2 reopened on GitHub: declined -> awaiting_approval" in await watcher.poll_once()
    assert PROPOSED in tracker.issues[1].labels
    on_github(tracker, 1, labels=[READY, DISPATCHED, FIXING])
    assert "#1 dispatched: approved -> running" in await watcher.poll_once()


@pytest.mark.asyncio
async def test_cli_and_dashboard_decisions_update_github_and_the_same_record(setup) -> None:
    tracker, store, watcher, approvals, wakes = setup
    tracker.issues = [issue(1), issue(2), issue(3)]
    await watcher.poll_once()
    one = approvals.find("1")
    approved = await approvals.approve(one, "cli", "terminal")
    assert approved.state == ItemState.approved and tracker.issues[0].labels == [READY]
    assert wakes == ["wake"]
    with pytest.raises(ApprovalError, match="not waiting for approval"):
        await approvals.approve(approved, "cli")
    declined = await approvals.decline(approvals.find(f"https://github.com/{REPO}/issues/2"), "cli")
    assert declined.state == ItemState.declined
    assert (tracker.issues[1].state, tracker.issues[1].state_reason) == ("closed", "not_planned")
    assert PROPOSED not in tracker.issues[1].labels
    assert await watcher.poll_once() == []
    assert wakes == ["wake", "wake"]  # the watcher re-wakes the dispatcher for work approved in another process

    app = create_app(store=store, projects=[], approvals=approvals)
    third = store.by_issue(REPO, 3)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.post(f"/api/items/{third.id}/approve", content="{}").status_code == 415
        assert (
            client.post(
                f"/api/items/{third.id}/approve",
                json={},
                headers={"Origin": "http://evil.example"},
            ).status_code
            == 403
        )
        response = client.post(f"/api/items/{third.id}/approve", json={})
        assert response.status_code == 200 and response.json()["state"] == "approved"
        assert client.post(f"/api/items/{third.id}/decline", json={}).json()["state"] == "declined"
        assert client.post(f"/api/items/{third.id}/approve", json={}).status_code == 409
        assert client.post("/api/items/item_missing/approve", json={}).status_code == 404
    with store.connection() as db:
        vias = [json.loads(row[0])["via"] for row in db.execute("SELECT detail FROM audit WHERE action='approve'")]
    assert vias == ["terminal", "dashboard"]
    assert (tracker.issues[2].state, tracker.issues[2].state_reason) == ("closed", "not_planned")


@pytest.mark.asyncio
async def test_nothing_dispatches_before_approval(setup, tmp_path) -> None:
    tracker, store, watcher, approvals, _ = setup
    settings = settings_from_env(example_manifest())
    agent = FakeAgent()
    dispatcher = Dispatcher(
        tracker,
        agent,
        tmp_path / "dispatch.json",
        manifest=settings.manifest,
        interval_s=60,
        dry_run=False,
        hold=dispatch_hold(store, REPO),
    )
    tracker.issues = [issue(1)]
    await watcher.poll_once()
    assert await dispatcher.poll_once() == [] and not agent.created
    await approvals.approve(approvals.find("1"), "cli")
    assert "started" in (await dispatcher.poll_once())[0]
    assert len(agent.created) == 1


@pytest.mark.asyncio
async def test_dispatch_holds_a_ready_issue_declined_in_garage(setup, tmp_path) -> None:
    tracker, store, watcher, approvals, _ = setup
    tracker.issues = [issue(1)]
    await watcher.poll_once()
    item = store.by_issue(REPO, 1)
    item.state = ItemState.declined
    store.save(item, "test", "decline")
    on_github(tracker, 1, labels=[READY])
    agent = FakeAgent()
    dispatcher = Dispatcher(
        tracker,
        agent,
        tmp_path / "dispatch.json",
        manifest=settings_from_env(example_manifest()).manifest,
        interval_s=60,
        dry_run=False,
        hold=dispatch_hold(store, REPO),
    )
    assert (await dispatcher.poll_once())[0].startswith("hold #1:")
    assert not agent.created


@pytest.mark.asyncio
async def test_requests_and_incidents_are_proposed_and_linked_once(setup) -> None:
    tracker, store, watcher, _, _ = setup
    work = SubmitWork(id="ask-1", text="Add dark mode", repo=REPO)
    item, _ = store.submit("cli", ClientKind.owner, work, REPO, "ambient-home")
    filed = await RequestLogger(tracker).file(
        Request(id=item.id, request=item.text, created_at=START + timedelta(minutes=1), repository=REPO)
    )
    assert filed is not None and filed.labels == [REQUEST, PROPOSED]
    assert "garage approve" in filed.body
    tracker.issues[0].created_at = tracker.issues[0].updated_at = START + timedelta(minutes=1)
    await watcher.poll_once()
    [linked] = store.items()
    assert linked.id == item.id and linked.issue is not None and linked.issue.number == 1
    assert not tracker.comments  # the request issue already explains how to approve


@pytest.mark.asyncio
async def test_incident_reaching_threshold_is_proposed_not_ready() -> None:
    from test_report import sample_incident

    tracker = FakeTracker()
    await IncidentReporter(tracker).sync([sample_incident()])
    assert tracker.issues[0].labels == [INCIDENT, PROPOSED]
    legacy = FakeTracker()
    await IncidentReporter(legacy, needs_approval=False).sync([sample_incident()])
    assert legacy.issues[0].labels == [INCIDENT, READY]


@pytest.mark.asyncio
async def test_without_triage_approval_new_issues_are_approved_as_before(setup, tmp_path) -> None:
    tracker, store, _, _, _ = setup
    watcher = IssueWatcher(
        store, tracker, tmp_path / "other.json", project=None, needs_approval=False, clock=lambda: START
    )
    tracker.issues = [issue(1)]
    await watcher.poll_once()
    assert store.by_issue(REPO, 1).state == ItemState.approved
    assert tracker.issues[0].labels == [READY]
    filed = await RequestLogger(FakeTracker(), needs_approval=False).file(
        Request(id="r", request="x", created_at=START, repository=REPO)
    )
    assert filed is not None and filed.labels == [REQUEST, READY]


def test_initial_state_and_ambiguous_issue_numbers(tmp_path) -> None:
    assert initial_state([READY, PROPOSED], True) == ItemState.approved
    assert initial_state([INCIDENT, OBSERVED], True) == ItemState.new
    assert initial_state([], True) == ItemState.awaiting_approval
    store = Store(tmp_path / "garage.db")
    approvals = Approvals(store, [ApprovalTarget("a", "o/a", None), ApprovalTarget("b", "o/b", None)])
    for project, repo in (("a", "o/a"), ("b", "o/b")):
        item, _ = store.submit("cli", ClientKind.owner, SubmitWork(id=project, text="x", repo=repo), repo, project)
        item.issue = IssueLink(number=7, url=f"https://github.com/{repo}/issues/7")
        store.save(item, "cli", "file_success")
    with pytest.raises(ApprovalError, match="several projects"):
        approvals.find("#7")
    assert approvals.find("7", "b").project == "b"
    with pytest.raises(ApprovalError, match="no GitHub token"):
        asyncio.run(approvals.approve(approvals.find("7", "a"), "cli"))


@pytest.mark.asyncio
async def test_github_changed_since_uses_etag_pages_and_flags_pull_requests() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        payload = {
            "number": 1 if "page=2" not in str(request.url) else 2,
            "html_url": "https://github.com/o/r/issues/1",
            "state": "open",
            "title": "t",
            "body": None,
            "labels": [{"name": PROPOSED}],
            "created_at": "2026-11-08T12:00:00Z",
            "updated_at": "2026-11-08T12:01:00Z",
            "user": {"login": "sesh11"},
        }
        if "page=2" in str(request.url):
            payload["pull_request"] = {}
            return httpx.Response(200, json=[payload])
        return httpx.Response(
            200, json=[payload], headers={"etag": '"v1"', "link": '<https://api.github.com/x?page=2>; rel="next"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        github = GitHubIssues("o/r", "token", http=http)
        found, etag, unchanged = await github.changed_since(START)
        assert [(i.number, i.pull_request, i.author) for i in found] == [(1, False, "sesh11"), (2, True, "sesh11")]
        assert etag == '"v1"' and not unchanged
        assert "since=2026-11-08T12:00:00Z" in str(seen[0].url) and "sort=updated" in str(seen[0].url)
        assert await github.changed_since(START, etag) == ([], '"v1"', True)


def test_cli_ask_approve_and_decline(tmp_path, monkeypatch, capsys) -> None:
    from garage import cli

    tracker = FakeTracker(REPO)
    monkeypatch.setattr(cli, "GitHubIssues", lambda repo, token: tracker)
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARAGE_GITHUB_TOKEN", "fake")
    manifest = str(example_manifest())
    assert cli.main(["ask", "--manifest", manifest, "Add a night mode"]) == 0
    out = capsys.readouterr().out
    assert "(awaiting_approval)" in out and "garage approve item_" in out
    assert tracker.issues[0].labels == [REQUEST, PROPOSED]
    assert cli.main(["approve", "--manifest", manifest, "1"]) == 0
    assert "is now approved" in capsys.readouterr().out
    assert tracker.issues[0].labels == [REQUEST, READY]
    assert cli.main(["ask", "--manifest", manifest, "Something else"]) == 0
    assert cli.main(["decline", "--manifest", manifest, "#2"]) == 0
    assert tracker.issues[1].state == "closed"
    assert cli.main(["approve", "--manifest", manifest, "#2"]) == 1
    assert "not waiting for approval" in capsys.readouterr().err
    tracker.issues.append(issue(3))
    tracker.issues[-1].created_at = tracker.issues[-1].updated_at = datetime.now(timezone.utc)
    assert cli.main(["intake", "--manifest", manifest, "--dry-run", "--since", "1h"]) == 0
    assert "[dry run] take in #3" in capsys.readouterr().out
    assert PROPOSED not in tracker.issues[-1].labels
    assert cli.main(["intake", "--manifest", manifest, "--since", "1h"]) == 0
    assert PROPOSED in tracker.issues[-1].labels
