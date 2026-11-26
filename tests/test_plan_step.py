# mypy: disable-error-code="no-untyped-def"
"""The plan step: every request is planned first; trusted requests start without approval."""

from datetime import datetime, timezone

import pytest
from conftest import FakeAgent, FakeTracker, example_manifest

from garage.store import Store
from garage.report import READY, REQUEST, PLANNING, PROPOSED
from garage.planner import Planner
from garage.tracker import PLAN_START, IssueRecord
from garage_sdk.api import InboxItem, IssueLink, ItemState, ClientKind, SubmitWork
from garage.approval import Approvals, IssueWatcher, ApprovalError, ApprovalTarget, dispatch_hold
from garage.dispatch import Dispatcher
from garage.planning import PlanStep, planned, trusted, plan_text
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


@pytest.mark.asyncio
async def test_a_partly_published_plan_closes_its_task_issues_and_waits(env) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    create = tracker.create_issue

    async def second_fails(title: str, body: str, labels: list[str]) -> IssueRecord:
        if len(tracker.issues) == 2:
            raise RuntimeError("GitHub is down")
        filed: IssueRecord = await create(title, body, labels)
        return filed

    tracker.create_issue = second_fails
    assert await step.poll_once() == ["could not plan #1: GitHub is down"]
    assert len(tracker.issues) == 2 and tracker.issues[1].state == "closed"
    assert tracker.issues[1].state_reason == "not_planned"
    assert store.get(item_id).state == ItemState.awaiting_approval and PROPOSED in tracker.issues[0].labels


@pytest.mark.asyncio
async def test_task_issues_inherit_the_requests_own_dependencies(env) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    ask(store, tracker, ClientKind.owner)
    tracker.issues[0].body += "\n\nDepends on #9"
    await step.poll_once()
    _, first, second = tracker.issues
    assert "Depends on #9" in first.body and "Depends on #2, #9" in second.body


@pytest.mark.asyncio
async def test_a_plan_whose_comment_fails_is_taken_out_and_replanned_after_approval(env) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS, TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    comment = tracker.comment

    async def plan_comment_fails(number: int, body: str) -> None:
        if "planned it before any work starts" in body:
            raise RuntimeError("GitHub is down")
        await comment(number, body)

    tracker.comment = plan_comment_fails
    assert await step.poll_once() == ["could not plan #1: GitHub is down"]
    assert not planned(tracker.issues[0].body) and [issue.state for issue in tracker.issues[1:]] == ["closed"] * 2
    tracker.comment = comment
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=True)])
    await approvals.approve(approvals.find(item_id), "nithin")
    assert await step.poll_once() == ["planned #1 (2 tasks): filed #4, #5"]


@pytest.mark.asyncio
async def test_a_request_declined_while_its_tasks_are_filed_is_rolled_back(env) -> None:
    tracker, store, fake, step, wakes = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=True)])
    comment = tracker.comment

    async def decline_on_plan_comment(number: int, body: str) -> None:
        await comment(number, body)
        if "planned it before any work starts" in body:
            await approvals.decline(approvals.find(item_id), "nithin")

    tracker.comment = decline_on_plan_comment
    assert await step.poll_once() == ["dropped the plan for #1: it was declined or closed while planning"]
    parent, first, second = tracker.issues
    assert parent.state == "closed" and not planned(parent.body)
    assert [first.state, second.state] == ["closed", "closed"] and first.state_reason == "not_planned"
    assert store.get(item_id).state == ItemState.declined
    assert store.history(item_id)[-1]["action"] == "plan_dropped" and wakes == []


@pytest.mark.asyncio
async def test_a_rollback_keeps_going_when_one_step_fails(env) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    update = tracker.update_issue

    async def flaky_update(number: int, **changes) -> IssueRecord:
        if number == 1 and "body" in changes:
            raise RuntimeError("GitHub is down")
        if number == 2 and changes.get("state") == "closed":
            raise RuntimeError("cannot close #2")
        updated: IssueRecord = await update(number, **changes)
        return updated

    tracker.update_issue = flaky_update
    assert await step.poll_once() == ["could not plan #1: GitHub is down"]
    assert tracker.issues[1].state == "open" and tracker.issues[2].state == "closed"
    assert PROPOSED in tracker.issues[0].labels and PLANNING not in tracker.issues[0].labels
    assert "Rolled back. Closed the task issues it had filed (#2, #3)" in tracker.comments[-1][1]
    entry = store.history(item_id)[-2]
    assert entry["action"] == "plan_failed" and entry["detail"]["not_undone"] == ["close task issue #2"]


@pytest.mark.asyncio
async def test_no_task_is_ready_before_the_last_check_and_an_approval_meanwhile_is_kept(env, tmp_path) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    comment = tracker.comment
    seen: list[list[str]] = []

    async def approve_on_plan_comment(number: int, body: str) -> None:
        await comment(number, body)
        if "planned it before any work starts" in body:
            seen.extend(issue.labels for issue in tracker.issues[1:])
            item = store.get(item_id)
            item.state = ItemState.approved
            store.save(item, "github", "approve")

    tracker.comment = approve_on_plan_comment
    assert await step.poll_once() == ["planned #1 (2 tasks): filed #2, #3"]
    assert seen == [[], []] and [issue.labels for issue in tracker.issues[1:]] == [[READY], [READY]]
    assert store.get(item_id).state == ItemState.running


@pytest.mark.asyncio
async def test_watcher_leaves_planning_items_and_unfinished_task_issues_to_the_plan_step(env, tmp_path) -> None:
    tracker, store, _, _, _ = env
    watcher = IssueWatcher(
        store, tracker, tmp_path / "intake.json", project="ambient-home", clock=lambda: START, plans=True
    )
    item_id = ask(store, tracker, ClientKind.owner)
    item = store.get(item_id)
    item.state = ItemState.planning
    store.save(item, "garage-planner", "plan_started")
    tracker.issues[0].labels = [REQUEST, PLANNING, READY]
    tracker.issues.append(github_issue(2, [], body="<!-- factory-plan-task: 1:t1 -->\nTask"))
    assert await watcher.poll_once() == []
    assert store.get(item_id).state == ItemState.planning and store.by_issue(REPO, 2) is None


@pytest.mark.asyncio
async def test_a_plan_left_half_published_by_a_restart_is_rolled_back(env) -> None:
    tracker, store, _, step, _ = env
    item_id = ask(store, tracker, ClientKind.owner)
    item = store.get(item_id)
    item.state = ItemState.planning
    store.save(item, "garage-planner", "plan_started")
    tracker.issues[0].labels = [REQUEST, PLANNING]
    tracker.issues[0].body = f"> Add a night mode\n\n{PLAN_START}\nplan\n<!-- garage-plan-end -->\n"
    assert await step.poll_once() == ["could not plan #1: Garage stopped while publishing"]
    assert not planned(tracker.issues[0].body) and tracker.issues[0].labels == [REQUEST, PROPOSED]
    assert store.get(item_id).state == ItemState.awaiting_approval


@pytest.mark.asyncio
async def test_task_issues_do_not_start_once_their_request_is_declined_or_cancelled(env, tmp_path) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    await step.poll_once()
    item = store.get(item_id)
    item.state = ItemState.cancelled
    store.save(item, "github", "github_closed")
    held = await dispatcher(tracker, store, tmp_path, FakeAgent()).poll_once()
    assert held == ["hold #2: its request #1 was cancelled", "hold #3: its request #1 was cancelled"]


@pytest.mark.asyncio
async def test_approving_during_planning_is_refused_and_a_split_request_is_never_ready(env, tmp_path) -> None:
    tracker, store, fake, step, _ = env
    fake.answers = [TWO_TASKS]
    item_id = ask(store, tracker, ClientKind.owner)
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=True)])
    comment = tracker.comment
    refused: list[str] = []

    async def approve_on_plan_comment(number: int, body: str) -> None:
        await comment(number, body)
        if "planned it before any work starts" in body:
            try:
                await approvals.approve(approvals.find(item_id), "nithin")
            except ApprovalError as exc:
                refused.append(str(exc))
            await tracker.add_labels(1, [READY])  # someone adds the label on GitHub meanwhile

    tracker.comment = approve_on_plan_comment
    assert await step.poll_once() == ["planned #1 (2 tasks): filed #2, #3"]
    assert refused and "is being planned" in refused[0]
    assert READY not in tracker.issues[0].labels
    agent = FakeAgent()
    await dispatcher(tracker, store, tmp_path, agent).poll_once()
    assert len(agent.created) == 1 and "Theme tokens" in str(agent.created[0][1]["title"])


@pytest.mark.asyncio
async def test_an_odd_plan_task_marker_does_not_stop_dispatch(env, tmp_path) -> None:
    tracker, store, _, _, _ = env
    tracker.issues = [
        github_issue(1, [READY], body="<!-- factory-plan-task: 99999999999999999999999:t1 -->"),
        github_issue(2, [READY]),
    ]
    agent = FakeAgent()
    assert (
        len(
            [action for action in await dispatcher(tracker, store, tmp_path, agent).poll_once() if "started" in action]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_a_request_left_planning_can_be_approved_once_planning_is_off(env) -> None:
    tracker, store, _, _, _ = env
    item_id = ask(store, tracker, ClientKind.owner)
    item = store.get(item_id)
    item.state = ItemState.planning
    store.save(item, "garage-planner", "plan_started")
    approvals = Approvals(store, [ApprovalTarget("ambient-home", REPO, tracker, plans=False)])
    assert (await approvals.approve(approvals.find(item_id), "nithin")).state == ItemState.approved


def test_terminal_approvals_know_when_planning_is_on(tmp_path, monkeypatch) -> None:
    from garage.cli import build_approvals

    monkeypatch.setenv("GARAGE_GITHUB_TOKEN", "fake")
    settings = settings_from_env(example_manifest())
    assert [target.plans for target in build_approvals([settings], Store(tmp_path / "garage.db")).targets] == [True]
    monkeypatch.setenv("GARAGE_PLAN_REQUESTS", "0")
    settings = settings_from_env(example_manifest())
    assert [target.plans for target in build_approvals([settings], Store(tmp_path / "garage.db")).targets] == [False]
