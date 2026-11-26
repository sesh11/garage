"""The plan step: every request is planned before work starts, and trusted requests start without approval.

A request (a GitHub issue, a ``garage ask``, an API or bridge item, or an incident) reaches this step in one of
two ways:

* Trusted (its client kind is in ``[policy].trusted``, or a GitHub issue opened by the repository owner or a
  ``[policy].trusted_authors`` login): planned right away, with no approval.
* Untrusted: it waits as ``garage:proposed`` and is planned once the owner approves it.

Planning labels the issue ``garage:planning``, runs the planner (:mod:`garage.planner`) on its title and body, writes
the plan into the issue body (so the coding agent sees it) and posts it as one comment. A one-task plan makes the
issue ``garage:ready-for-fix``; a larger plan files one ready issue per task, with ``Depends on`` lines for the
order, and closes this issue once every task issue is closed as completed. A plan that fails, or whose request is
declined or closed while it is planned, is rolled back in full (:meth:`PlanStep._rollback`); after a failure the issue
waits as ``garage:proposed`` again, and approving it plans it from scratch.
"""

from __future__ import annotations
import re
import asyncio
import logging
from collections.abc import Callable, Awaitable

from garage_sdk import emit, scrub_text
from garage.store import Store
from garage.report import READY, FIXING, REQUEST, INCIDENT, PLANNING, PROPOSED, DISPATCHED
from garage.planner import Planner, TaskGraph, render_plan
from garage.tracker import PLAN_END, PLAN_START, Tracker, IssueRecord, replace_plan_block
from garage_sdk.api import InboxItem, ItemState, ClientKind
from garage.dependencies import parse_dependencies
from garage_sdk.manifest import PolicySection


logger = logging.getLogger(__name__)

PLANNER_CLIENT = "garage-planner"
_PLAN_BLOCK = re.compile(re.escape(PLAN_START) + r".*?" + re.escape(PLAN_END), re.DOTALL)
_HIDDEN = re.compile(r"<!--.*?-->", re.DOTALL)


def planned(body: str) -> bool:
    """Whether an issue body already carries a Garage plan."""
    return PLAN_START in body


def with_plan(body: str, plan: str) -> str:
    """Return ``body`` with its plan block replaced by ``plan``."""
    return replace_plan_block(body, f"{PLAN_START}\n{plan.strip()}\n{PLAN_END}")


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
    """Render the issue body for one task of a plan; every task also waits on the parent's own dependencies."""
    task = next(task for task in graph.tasks if task.id == task_id)
    lines = [
        f"<!-- factory-plan-task: {parent.number}:{task.id} -->",
        f"Task `{task.id}` of the plan on #{parent.number}. Filed automatically by garage.",
        "",
        scrub_text(task.body).strip(),
    ]
    inherited = [
        f"{dependency.repo}#{dependency.number}" if dependency.repo else f"#{dependency.number}"
        for dependency in parse_dependencies(_PLAN_BLOCK.sub("", parent.body))
    ]
    refs = [f"#{numbers[dependency]}" for dependency in task.depends_on] + inherited
    if refs:
        lines += ["", "Depends on " + ", ".join(dict.fromkeys(refs))]
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
            if item.state == ItemState.planning:
                # Garage stopped after writing the plan but before saving it: start over cleanly.
                return await self._rollback(item, issue.number, [], RuntimeError("Garage stopped while publishing"))
            self._planned.add(item.id)
            return None
        is_trusted = trusted(item, issue, self.policy, self.tracker.repo)
        if item.state == ItemState.awaiting_approval and (not is_trusted or self._tried(item)):
            return None
        return await self._plan(item, issue, is_trusted)

    async def _plan(self, item: InboxItem, issue: IssueRecord, is_trusted: bool) -> str:
        """Plan one request; any failure, or a decline or close meanwhile, goes through :meth:`_rollback`."""
        number = issue.number
        approved = item.state == ItemState.approved
        await self.tracker.add_labels(number, [PLANNING])
        for label in (PROPOSED, READY):
            if label in issue.labels:
                await self.tracker.remove_label(number, label)
        item.state = ItemState.planning
        self.store.save(item, PLANNER_CLIENT, "plan_started", {"trusted": is_trusted, "approved": approved})
        emit("factory.plan_started", "factory", item=item.id, issue=number, trusted=is_trusted)
        why = "Approved" if approved else ("Trusted request" if is_trusted else "Request")
        created: list[int] = []
        try:
            if self._planner is None:
                self._planner = self._planner_factory()
            graph = await self._planner.plan(plan_text(issue))
            latest = await self._still_wanted(item, number)
            if latest is None:
                return await self._rollback(item, number, created, None)
            result, state, detail = await self._publish(latest, graph, why, created)
            if await self._still_wanted(item, number) is None:
                return await self._rollback(item, number, created, None)
            for ready in created or [number]:
                await self.tracker.add_labels(ready, [READY])
            await self.tracker.remove_label(number, PLANNING)
        except Exception as exc:
            return await self._rollback(item, number, created, exc)
        item.state = state
        self.store.save(item, PLANNER_CLIENT, "planned", detail)
        self._planned.discard(item.id)
        emit("factory.planned", "factory", item=item.id, issue=number, tasks=len(graph.tasks))
        if self.on_ready is not None:
            self.on_ready()
        return result

    async def _still_wanted(self, item: InboxItem, number: int) -> IssueRecord | None:
        """Return the open issue while its item is still wanted; None once it was declined, cancelled or closed."""
        current = self.store.get(item.id)
        if current is None or current.state in {ItemState.declined, ItemState.cancelled, ItemState.done}:
            return None
        latest = await self.tracker.get_issue(number)
        return latest if latest is not None and latest.state == "open" else None

    async def _publish(
        self, issue: IssueRecord, graph: TaskGraph, why: str, created: list[int]
    ) -> tuple[str, ItemState, dict[str, object]]:
        """Post the plan and file its task issues, appending each to ``created`` as it is filed.

        Nothing is labeled ``garage:ready-for-fix`` here, so no agent can start before the caller checks once more
        that the request is still wanted. Returns what was done, and the item's new state and decision-log detail.
        """
        number = issue.number
        rendered = scrub_text(render_plan(graph)).strip()
        ordered = graph.order()
        if len(ordered) == 1:
            await self.tracker.update_issue(number, body=with_plan(issue.body, f"### Plan\n\n{rendered}"))
            await self.tracker.comment(
                number,
                f"{why}: planned it before any work starts.\n\n{rendered}\n\nLabeled `{READY}`. A coding agent "
                "starts on the next dispatch poll.",
            )
            return f"planned #{number} (1 task): ready", ItemState.approved, {"tasks": 1, "issues": []}
        numbers: dict[str, int] = {}
        for task in ordered:
            filed = await self.tracker.create_issue(
                scrub_text(task.title)[:120], task_body(graph, task.id, issue, numbers), []
            )
            numbers[task.id] = filed.number
            created.append(filed.number)
        listing = "\n".join(f"- `{task.id}` #{numbers[task.id]}: {scrub_text(task.title)}" for task in ordered)
        await self.tracker.update_issue(
            number, body=with_plan(issue.body, f"### Plan\n\n{rendered}\n\nTask issues:\n{listing}")
        )
        await self.tracker.comment(
            number,
            f"{why}: planned it before any work starts.\n\n{rendered}\n\nFiled one `{READY}` issue per task:\n"
            f"{listing}\n\nEach starts once the tasks it depends on are closed as completed. This issue closes "
            "when every task issue is.",
        )
        refs = ", ".join(f"#{value}" for value in numbers.values())
        detail: dict[str, object] = {"tasks": len(ordered), "issues": list(numbers.values())}
        return f"planned #{number} ({len(ordered)} tasks): filed {refs}", ItemState.running, detail

    async def _rollback(self, item: InboxItem, number: int, created: list[int], error: Exception | None) -> str:
        """Undo a plan that did not finish, so nothing it filed can start and planning can begin again cleanly.

        Every step is attempted even if an earlier one fails: close the task issues already filed, take the plan out
        of the issue body and drop ``garage:planning`` and ``garage:ready-for-fix``. After an ``error`` the request
        goes back to ``garage:proposed`` to be approved again; with no error it was declined or closed meanwhile,
        which stands.
        """
        undone: list[str] = []

        async def attempt(step: str, action: Awaitable[object]) -> None:
            try:
                await action
            except Exception as exc:
                logger.warning("Rollback of #%d could not %s: %s", number, step, exc)
                undone.append(step)

        for task_issue in created:
            await attempt(
                f"close task issue #{task_issue}",
                self.tracker.update_issue(task_issue, state="closed", state_reason="not_planned"),
            )
        issue: IssueRecord | None = None
        try:
            issue = await self.tracker.get_issue(number)
        except Exception as exc:
            logger.warning("Rollback of #%d could not read the issue: %s", number, exc)
            undone.append("read the issue")
        if issue is not None and planned(issue.body):
            body = _PLAN_BLOCK.sub("", issue.body).rstrip() + "\n"
            await attempt("remove the plan", self.tracker.update_issue(number, body=body))
        labels = issue.labels if issue is not None else [PLANNING, READY]
        for label in (PLANNING, READY):
            if label in labels:
                await attempt(f"remove {label}", self.tracker.remove_label(number, label))
        detail: dict[str, object] = {"closed": created}
        if undone:
            detail["not_undone"] = undone
        if error is None:
            self.store.save(self.store.get(item.id) or item, PLANNER_CLIENT, "plan_dropped", detail)
            emit("factory.plan_dropped", "factory", item=item.id, issue=number)
            return f"dropped the plan for #{number}: it was declined or closed while planning"
        reason = scrub_text(str(error) or type(error).__name__)[:500]
        logger.warning("Could not plan #%d: %s", number, reason)
        await attempt(f"add {PROPOSED}", self.tracker.add_labels(number, [PROPOSED]))
        closed = f" Closed the task issues it had filed ({', '.join(f'#{n}' for n in created)})." if created else ""
        await attempt(
            "comment",
            self.tracker.comment(
                number,
                f"Garage could not plan this: {reason}\n\nRolled back.{closed} It waits as `{PROPOSED}`. Add "
                f"`{READY}` to plan it again from scratch, or close this issue to decline.",
            ),
        )
        item.state = ItemState.awaiting_approval
        self.store.save(item, PLANNER_CLIENT, "plan_failed", {"error": reason, **detail})
        emit("factory.plan_failed", "factory", "warning", item=item.id, issue=number, error=reason)
        return f"could not plan #{number}: {reason}"

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
