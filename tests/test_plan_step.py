# mypy: disable-error-code="no-untyped-def"
"""The plan step: every request is planned first; trusted requests start without approval."""

from datetime import datetime, timezone

import pytest
from conftest import FakeAgent, FakeTracker, example_manifest

from garage.store import Store
from garage.report import READY, REQUEST, PLANNING, PROPOSED
from garage.planner import Planner
from garage.tracker import IssueRecord
from garage_sdk.api import InboxItem, IssueLink, ItemState, ClientKind, SubmitWork
from garage.approval import Approvals, IssueWatcher, ApprovalTarget, dispatch_hold
from garage.dispatch import Dispatcher
from garage.planning import PLAN_START, PlanStep, planned, trusted, plan_text
from garage.settings import settings_from_env
from garage_sdk.manifest import PolicySection


REPO = "sesh11/ambient-home"
START = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
ONE_TASK = {
    "summary": "Add night mode.",
    "tasks": [{"id": "t1", "title": "Night mode", "body": "Do it.", "depends_on": []}],
}
TWO_TASKS = {
    "summary": "Two steps.",
    "tasks": [
        {"id": "t1", "title": "Theme tokens", "body": "Add tokens.", "depends_on": []},
        {"id": "t2", "title": "Night toggle", "body": "Add the toggle.", "depends_on": ["t1"]},
    ],
}


def github_issue(number: int, labels: list[str], author: str = "sesh11", body: str = "Please add night mode."):
    return IssueRecord(
        number=number,
        url=f"https://github.com/{REPO}/issues/{number}",
        state="open",
        title=f"Issue {number}",
        body=body,
        labels=labels,
        created_at=START,
        updated_at=START,
        author=author,
    )


class FakePlanner:
    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def run(self, prompt: str, schema: dict[str, object]) -> object:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def env(tmp_path):
    tracker = FakeTracker(REPO)
    store = Store(tmp_path / "garage.db")
    fake = FakePlanner()
    wakes: list[str] = []
    step = PlanStep(
        store,
        tracker,
        lambda: Planner(fake.run, repo=REPO),
        project="ambient-home",
        policy=PolicySection(),
        on_ready=lambda: wakes.append("ready"),
    )
    return tracker, store, fake, step, wakes


def ask(store: Store, tracker: FakeTracker, kind: ClientKind, text: str = "Add a night mode") -> str:
    """File a request the way `garage ask` or the bridge does: an inbox item linked to a proposed issue."""
    work = SubmitWork(id=f"ask-{len(tracker.issues) + 1}", text=text, repo=REPO)
    item, _ = store.submit("cli", kind, work, REPO, "ambient-home")
    number = len(tracker.issues) + 1
    tracker.issues.append(github_issue(number, [REQUEST, PROPOSED], body=f"> {text}"))
    item.issue = IssueLink(number=number, url=tracker.issues[-1].url)
    item.state = ItemState.awaiting_approval
    store.save(item, "cli", "file_success")
    return item.id


def dispatcher(tracker: FakeTracker, store: Store, tmp_path, agent: FakeAgent) -> Dispatcher:
    return Dispatcher(
        tracker,
        agent,
        tmp_path / "dispatch.json",
        manifest=settings_from_env(example_manifest()).manifest,
        interval_s=60,
        dry_run=False,
        hold=dispatch_hold(store, REPO, plans=True),
    )


@pytest.mark.asyncio
async def test_trusted_request_is_planned_and_dispatched_without_approval(env, tmp_path) -> None:
    tracker, store, fake, step, wakes = env
    fake.answers = [ONE_TASK]
    item_id = ask(store, tracker, ClientKind.owner)
    agent = FakeAgent()
    assert await dispatcher(tracker, store, tmp_path, agent).poll_once() == []
    assert await step.poll_once() == ["planned #1 (1 task): ready"]
    issue = tracker.issues[0]
    assert PLANNING not in issue.labels and PROPOSED not in issue.labels and READY in issue.labels
    assert planned(issue.body) and "Night mode" in issue.body and "> Add a night mode" in issue.body
    assert "Issue 1" in fake.prompts[0] and "Add a night mode" in fake.prompts[0]
    plan_comments = [body for number, body in tracker.comments if number == 1]
    assert len(plan_comments) == 1 and plan_comments[0].startswith("Trusted request: planned it")
    assert store.get(item_id).state == ItemState.approved
    assert wakes == ["ready"]
    actions = [entry["action"] for entry in store.history(item_id)]
    assert actions[-4:] == ["plan_started", "state_change", "planned", "state_change"]
    held = dispatcher(tracker, store, tmp_path, agent)
    assert "started" in (await held.poll_once())[0]
    assert "[t1] Night mode" in agent.created[0][0]
    assert await step.poll_once() == []


@pytest.mark.asyncio
async def test_untrusted_request_waits_for_approval_then_is_planned(env, tmp_path) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [ONE_TASK]
    item_id = ask(store, tracker, ClientKind.public)
    assert await step.poll_once() == [] and not fake.prompts
    assert tracker.issues[0].labels == [REQUEST, PROPOSED]
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=True)])
    await approvals.approve(approvals.find(item_id), "nithin")
    assert "Garage plans it next" in tracker.comments[-1][1]
    agent = FakeAgent()
    assert await dispatcher(tracker, store, tmp_path, agent).poll_once() == ["hold #1: waiting for the plan step"]
    assert await step.poll_once() == ["planned #1 (1 task): ready"]
    assert tracker.comments[-1][1].startswith("Approved: planned it")
    assert "started" in (await dispatcher(tracker, store, tmp_path, agent).poll_once())[0]


@pytest.mark.asyncio
async def test_a_larger_plan_files_one_issue_per_task_and_closes_when_they_are_done(env) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    assert await step.poll_once() == ["planned #1 (2 tasks): filed #2, #3"]
    parent, first, second = tracker.issues
    assert READY not in parent.labels and PLANNING not in parent.labels
    assert "#2" in parent.body and "#3" in parent.body and planned(parent.body)
    assert first.title == "Theme tokens" and first.labels == [READY] and planned(first.body)
    assert "Depends on" not in first.body and "Depends on #2" in second.body
    assert "Task `t2` of the plan on #1" in second.body
    assert store.get(item_id).state == ItemState.running
    assert len([body for number, body in tracker.comments if number == 1]) == 1
    assert await step.poll_once() == []
    for number in (2, 3):
        await tracker.update_issue(number, state="closed", state_reason="completed")
    assert await step.poll_once() == ["closed #1: every task is done"]
    assert tracker.issues[0].state == "closed" and tracker.issues[0].state_reason == "completed"
    assert store.get(item_id).state == ItemState.done


@pytest.mark.asyncio
async def test_a_failed_plan_waits_for_approval_and_approving_retries(env) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [RuntimeError("planner CLI exited with 1"), ONE_TASK]
    item_id = ask(store, tracker, ClientKind.owner)
    assert await step.poll_once() == ["could not plan #1: planner CLI exited with 1"]
    assert PROPOSED in tracker.issues[0].labels and PLANNING not in tracker.issues[0].labels
    assert "could not plan this" in tracker.comments[-1][1]
    assert store.get(item_id).state == ItemState.awaiting_approval
    assert await step.poll_once() == []
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=True)])
    await approvals.approve(approvals.find(item_id), "nithin")
    assert await step.poll_once() == ["planned #1 (1 task): ready"]


@pytest.mark.asyncio
async def test_watcher_labels_a_trusted_authors_issue_planning_and_a_strangers_proposed(env, tmp_path) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [ONE_TASK]
    policy = PolicySection()
    watcher = IssueWatcher(
        store,
        tracker,
        tmp_path / "intake.json",
        project="ambient-home",
        clock=lambda: START,
        plans=True,
        trusts=lambda item, issue: trusted(item, issue, policy, REPO),
    )
    tracker.issues = [github_issue(1, []), github_issue(2, [], author="stranger")]
    await watcher.poll_once()
    assert tracker.issues[0].labels == [PLANNING] and tracker.issues[1].labels == [PROPOSED]
    assert not [body for number, body in tracker.comments if number == 1]
    assert await step.poll_once() == ["planned #1 (1 task): ready"]
    assert await watcher.poll_once() == []
    assert store.by_issue(REPO, 1).state == ItemState.approved


def test_who_is_trusted() -> None:
    policy = PolicySection(trusted_authors=["Friend"])

    def item(kind: ClientKind):
        return InboxItem.model_validate(
            {
                "id": "item_1",
                "submit_id": "x",
                "submitted_by": "github",
                "trust": kind,
                "kind": "ask",
                "text": "t",
                "state": "awaiting_approval",
                "repos": [],
                "context": {},
                "created_at": START,
                "updated_at": START,
            }
        )

    assert trusted(item(ClientKind.owner), github_issue(1, [], author="anyone"), policy, REPO)
    assert trusted(item(ClientKind.app), github_issue(1, [], author="anyone"), policy, REPO)
    assert not trusted(item(ClientKind.agent), github_issue(1, [], author="sesh11"), policy, REPO)
    assert trusted(item(ClientKind.public), github_issue(1, [], author="SESH11"), policy, REPO)
    assert trusted(item(ClientKind.public), github_issue(1, [], author="friend"), policy, REPO)
    assert not trusted(item(ClientKind.public), github_issue(1, [], author="stranger"), policy, REPO)
    assert not trusted(item(ClientKind.public), github_issue(1, [REQUEST], author="sesh11"), policy, REPO)
    assert not trusted(item(ClientKind.owner), github_issue(1, []), PolicySection(trusted=[]), REPO)


def test_plan_text_drops_hidden_markers() -> None:
    body = f"<!-- factory-request: item_1 -->\n> Add it\n\n{PLAN_START}\nold plan\n<!-- garage-plan-end -->\n"
    assert plan_text(github_issue(1, [], body=body)) == "Issue 1\n\n> Add it"


def test_plan_requests_setting(monkeypatch) -> None:
    assert settings_from_env(example_manifest()).plan_requests is True
    monkeypatch.setenv("GARAGE_PLAN_REQUESTS", "0")
    assert settings_from_env(example_manifest()).plan_requests is False


@pytest.mark.asyncio
async def test_a_request_declined_while_planning_publishes_nothing(env) -> None:
    tracker, store, fake, step, _ = env
    item_id = ask(store, tracker, ClientKind.owner)
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=True)])

    async def decline_meanwhile(prompt: str, schema: dict[str, object]) -> object:
        await approvals.decline(approvals.find(item_id), "nithin")
        return TWO_TASKS

    step._planner = Planner(decline_meanwhile, repo=REPO)
    assert await step.poll_once() == ["dropped the plan for #1: it was declined or closed while planning"]
    assert len(tracker.issues) == 1 and tracker.issues[0].state == "closed"
    assert PLANNING not in tracker.issues[0].labels and not planned(tracker.issues[0].body)
    assert store.get(item_id).state == ItemState.declined
