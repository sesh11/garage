"""The plan step: every request is planned before work starts, and trusted requests start without approval.

A request (a GitHub issue, a ``garage ask``, an API or bridge item, or an incident) reaches this step in one of
two ways:

* Trusted (its client kind is in ``[policy].trusted``, or a GitHub issue opened by the repository owner or a
  ``[policy].trusted_authors`` login): planned right away, with no approval.
* Untrusted: it waits as ``garage:proposed`` and is planned once the owner approves it.

Planning labels the issue ``garage:planning``, runs the planner (:mod:`garage.planner`) on its title and body, writes
the plan into the issue body (so the coding agent sees it) and posts it as one comment. A one-task plan makes the
issue ``garage:ready-for-fix``; a larger plan files one ready issue per task, with ``Depends on`` lines for the
order, and closes this issue once every task issue is closed as completed. A failed plan sends the issue back to
``garage:proposed``; approving it again retries.
"""

from __future__ import annotations
import re
import asyncio
import logging
from collections.abc import Callable

from garage_sdk import emit, scrub_text
from garage.store import Store
from garage.report import READY, FIXING, REQUEST, INCIDENT, PLANNING, PROPOSED, DISPATCHED
from garage.planner import Planner, TaskGraph, render_plan
from garage.tracker import Tracker, IssueRecord
from garage_sdk.api import InboxItem, ItemState, ClientKind
from garage_sdk.manifest import PolicySection


logger = logging.getLogger(__name__)

PLAN_START = "<!-- garage-plan-start -->"
PLAN_END = "<!-- garage-plan-end -->"
PLANNER_CLIENT = "garage-planner"
_PLAN_BLOCK = re.compile(re.escape(PLAN_START) + r".*?" + re.escape(PLAN_END), re.DOTALL)
_HIDDEN = re.compile(r"<!--.*?-->", re.DOTALL)


def planned(body: str) -> bool:
    """Whether an issue body already carries a Garage plan."""
    return PLAN_START in body


def with_plan(body: str, plan: str) -> str:
    """Return ``body`` with its plan block replaced by ``plan``."""
    base = _PLAN_BLOCK.sub("", body).rstrip()
    return f"{base}\n\n{PLAN_START}\n{plan.strip()}\n{PLAN_END}\n"


def plan_text(issue: IssueRecord) -> str:
    """Return what the planner reads: the issue's title and body, without Garage's hidden markers."""
    body = _HIDDEN.sub("", _PLAN_BLOCK.sub("", issue.body)).strip()
    return f"{issue.title}\n\n{body}".strip()


def trusted(item: InboxItem, issue: IssueRecord, policy: PolicySection, repo: str) -> bool:
    """Whether a request may be planned and started without the owner's approval."""
    if item.trust.value in policy.trusted:
        return True
    if item.trust != ClientKind.public or {REQUEST, INCIDENT} & set(issue.labels) or not issue.author:
        return False
    authors = {login.strip().lower() for login in policy.trusted_authors} | {repo.split("/")[0].lower()}
    return issue.author.lower() in authors


def task_body(graph: TaskGraph, task_id: str, parent: IssueRecord, numbers: dict[str, int]) -> str:
    """Render the issue body for one task of a plan."""
    task = next(task for task in graph.tasks if task.id == task_id)
    lines = [
        f"<!-- garage-plan-task: {parent.number}:{task.id} -->",
        f"Task `{task.id}` of the plan on #{parent.number}. Filed automatically by garage.",
        "",
        scrub_text(task.body).strip(),
    ]
    if task.depends_on:
        lines += ["", "Depends on " + ", ".join(f"#{numbers[dependency]}" for dependency in task.depends_on)]
    return with_plan("\n".join(lines), f"Planned as task `{task.id}` of #{parent.number}.")


class PlanStep:
    """Plan one project's requests before any coding agent starts on them."""

    def __init__(
        self,
        store: Store,
        tracker: Tracker,
        planner: Callable[[], Planner],
        *,
        project: str | None,
        policy: PolicySection,
        on_ready: Callable[[], None] | None = None,
        interval_s: float = 60,
    ) -> None:
        """Plan items for ``tracker.repo`` with the planner ``planner()`` returns (built on first use)."""
        self.store = store
        self.tracker = tracker
        self.project = project
        self.policy = policy
        self.on_ready = on_ready
        self.interval_s = interval_s
        self._planner_factory = planner
        self._planner: Planner | None = None
        self._planned: set[str] = set()
        self._wake = asyncio.Event()

    def poke(self) -> None:
        """Wake the loop, for example right after an approval."""
        self._wake.set()

    async def run(self) -> None:
        """Poll forever; a failed poll is logged and retried on the next tick."""
        while True:
            try:
                for action in await self.poll_once():
                    logger.info("plan %s: %s", self.tracker.repo, action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Plan step failed for %s", self.tracker.repo)
                emit("factory.plan_failed", "factory", "error", repo=self.tracker.repo, error=str(exc))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass
            self._wake.clear()

    def _mine(self, item: InboxItem) -> bool:
        if item.issue is None or not item.repos or item.repos[0].full_name != self.tracker.repo.lower():
            return False
        return self.project is None or item.project in (None, self.project)

    def _tried(self, item: InboxItem) -> bool:
        return any(entry["action"] == "plan_started" for entry in self.store.history(item.id))

    def _tasks(self, item: InboxItem) -> list[int]:
        for entry in reversed(self.store.history(item.id)):
            if entry["action"] == "planned":
                detail = entry["detail"] if isinstance(entry["detail"], dict) else {}
                tasks = detail.get("issues")
                return [int(number) for number in tasks] if isinstance(tasks, list) else []
        return []

    async def poll_once(self) -> list[str]:
        """Plan every item that is ready for it, and close plans whose tasks are all done; return what was done."""
        done: list[str] = []
        for item in reversed(self.store.items()):
            if not self._mine(item) or item.id in self._planned:
                continue
            assert item.issue is not None
            if item.state == ItemState.running:
                action = await self._finish(item)
            elif item.state in {ItemState.awaiting_approval, ItemState.approved, ItemState.planning}:
                action = await self._consider(item)
            else:
                continue
            if action:
                done.append(action)
        return done

    async def _consider(self, item: InboxItem) -> str | None:
        assert item.issue is not None
        issue = await self.tracker.get_issue(item.issue.number)
        if issue is None or issue.state != "open" or {DISPATCHED, FIXING} & set(issue.labels):
            return None
        if planned(issue.body):
            self._planned.add(item.id)
            return None
        is_trusted = trusted(item, issue, self.policy, self.tracker.repo)
        if item.state == ItemState.awaiting_approval and (not is_trusted or self._tried(item)):
            return None
        return await self._plan(item, issue, is_trusted)

    async def _plan(self, item: InboxItem, issue: IssueRecord, is_trusted: bool) -> str:
        number = issue.number
        approved = item.state == ItemState.approved
        await self.tracker.add_labels(number, [PLANNING])
        for label in (PROPOSED, READY):
            if label in issue.labels:
                await self.tracker.remove_label(number, label)
        item.state = ItemState.planning
        self.store.save(item, PLANNER_CLIENT, "plan_started", {"trusted": is_trusted, "approved": approved})
        emit("factory.plan_started", "factory", item=item.id, issue=number, trusted=is_trusted)
        try:
            if self._planner is None:
                self._planner = self._planner_factory()
            graph = await self._planner.plan(plan_text(issue))
        except Exception as exc:
            return await self._failed(item, number, exc)
        why = "Approved" if approved else ("Trusted request" if is_trusted else "Request")
        rendered = scrub_text(render_plan(graph)).strip()
        ordered = graph.order()
        if len(ordered) == 1:
            await self.tracker.update_issue(number, body=with_plan(issue.body, f"### Plan\n\n{rendered}"))
            await self.tracker.add_labels(number, [READY])
            await self.tracker.remove_label(number, PLANNING)
            await self.tracker.comment(
                number,
                f"{why}: planned it before any work starts.\n\n{rendered}\n\nLabeled `{READY}`. A coding agent "
                "starts on the next dispatch poll.",
            )
            item.state = ItemState.approved
            self.store.save(item, PLANNER_CLIENT, "planned", {"tasks": 1, "issues": []})
            result = f"planned #{number} (1 task): ready"
        else:
            numbers: dict[str, int] = {}
            for task in ordered:
                filed = await self.tracker.create_issue(
                    scrub_text(task.title)[:120], task_body(graph, task.id, issue, numbers), [READY]
                )
                numbers[task.id] = filed.number
            listing = "\n".join(f"- `{task.id}` #{numbers[task.id]}: {scrub_text(task.title)}" for task in ordered)
            await self.tracker.update_issue(
                number, body=with_plan(issue.body, f"### Plan\n\n{rendered}\n\nTask issues:\n{listing}")
            )
            await self.tracker.remove_label(number, PLANNING)
            await self.tracker.comment(
                number,
                f"{why}: planned it before any work starts.\n\n{rendered}\n\nFiled one `{READY}` issue per task:\n"
                f"{listing}\n\nEach starts once the tasks it depends on are closed as completed. This issue closes "
                "when every task issue is.",
            )
            item.state = ItemState.running
            self.store.save(item, PLANNER_CLIENT, "planned", {"tasks": len(ordered), "issues": list(numbers.values())})
            result = f"planned #{number} ({len(ordered)} tasks): filed " + ", ".join(
                f"#{value}" for value in numbers.values()
            )
        self._planned.discard(item.id)
        emit("factory.planned", "factory", item=item.id, issue=number, tasks=len(ordered))
        if self.on_ready is not None:
            self.on_ready()
        return result

    async def _failed(self, item: InboxItem, number: int, error: Exception) -> str:
        detail = scrub_text(str(error) or type(error).__name__)[:500]
        logger.warning("Could not plan #%d: %s", number, detail)
        await self.tracker.remove_label(number, PLANNING)
        await self.tracker.add_labels(number, [PROPOSED])
        await self.tracker.comment(
            number,
            f"Garage could not plan this: {detail}\n\nIt waits as `{PROPOSED}`. Add `{READY}` to plan it again, or "
            "close this issue to decline.",
        )
        item.state = ItemState.awaiting_approval
        self.store.save(item, PLANNER_CLIENT, "plan_failed", {"error": detail})
        emit("factory.plan_failed", "factory", "warning", item=item.id, issue=number, error=detail)
        return f"could not plan #{number}: {detail}"

    async def _finish(self, item: InboxItem) -> str | None:
        """Close a split plan's issue once every task issue is closed as completed."""
        assert item.issue is not None
        tasks = self._tasks(item)
        if not tasks:
            return None
        for task in tasks:
            found = await self.tracker.get_issue(task)
            if found is None or found.state != "closed" or found.state_reason not in (None, "completed"):
                return None
        issue = await self.tracker.get_issue(item.issue.number)
        if issue is not None and issue.state == "open":
            refs = ", ".join(f"#{task}" for task in tasks)
            await self.tracker.comment(item.issue.number, f"Every task is done ({refs}). Closing as completed.")
            await self.tracker.update_issue(item.issue.number, state="closed", state_reason="completed")
        item.state = ItemState.done
        self.store.save(item, PLANNER_CLIENT, "plan_done", {"issues": tasks})
        self._planned.add(item.id)
        return f"closed #{item.issue.number}: every task is done"
