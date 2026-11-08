"""The approval gate: new work waits as ``factory:proposed`` until the owner approves or declines it.

One record per piece of work lives in the local inbox (SQLite). Three surfaces change it, and all three end in the
same place:

* GitHub (the phone): adding ``factory:ready-for-fix`` approves; closing the issue declines. The
  :class:`IssueWatcher` notices on its next poll and updates the inbox.
* The dashboard's "Waiting for you" list and the ``garage approve`` / ``garage decline`` commands call
  :class:`Approvals`, which changes the GitHub issue first and then the inbox, so the next poll sees nothing new.

Only an approved issue (one labeled ``factory:ready-for-fix``) is ever dispatched to a coding agent.
"""

from __future__ import annotations
import re
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from garage_sdk import emit
from garage.store import Store
from garage.report import READY, FIXING, LABELS, REQUEST, INCIDENT, OBSERVED, PROPOSED, DISPATCHED
from garage.tracker import Tracker, IssueFeed, IssueRecord
from garage_sdk.api import Source, ItemKind, InboxItem, IssueLink, ItemState, ClientKind, SubmitWork


logger = logging.getLogger(__name__)

BEFORE_APPROVAL = frozenset(
    {ItemState.new, ItemState.triage, ItemState.needs_info, ItemState.planning, ItemState.awaiting_approval}
)
FINISHED = frozenset({ItemState.done, ItemState.declined, ItemState.cancelled})
GITHUB_CLIENT = "github"
MONITOR_CLIENT = "garage-monitor"


class ApprovalError(Exception):
    """An approve or decline that cannot happen, with a message for the person who asked."""


def initial_state(labels: Sequence[str], needs_approval: bool) -> ItemState:
    """Return the inbox state an issue with these labels starts in."""
    if FIXING in labels or DISPATCHED in labels:
        return ItemState.running
    if READY in labels:
        return ItemState.approved
    if PROPOSED in labels:
        return ItemState.awaiting_approval
    if OBSERVED in labels:
        return ItemState.new
    return ItemState.awaiting_approval if needs_approval else ItemState.approved


def next_step(item: InboxItem) -> str:
    """Return what happens to an approved item next.

    Today that is always ``"dispatch"``: the dispatcher starts a coding agent on the approved issue. This is where a
    future ``"plan"`` step (triage and a design posted on the issue, approved on its own) slots in before dispatch.
    """
    return "dispatch"


def dispatch_hold(store: Store, repo: str) -> Callable[[IssueRecord], str | None]:
    """Return the dispatcher's last check: a reason to hold a ready issue back, or None to dispatch it."""

    def hold(issue: IssueRecord) -> str | None:
        item = store.by_issue(repo, issue.number)
        if item is None:
            return None
        if item.state in {ItemState.declined, ItemState.cancelled}:
            return f"{item.id} was {item.state.value} in Garage"
        step = next_step(item)
        return None if step == "dispatch" else f"waiting for the {step} step"

    return hold


@dataclass
class ApprovalTarget:
    """One project's GitHub repository, a client that can write to it, and its dispatcher wake-up."""

    name: str | None
    repo: str
    tracker: Tracker | None
    on_approved: Callable[[], None] | None = None


class Approvals:
    """Approve and decline inbox items, changing the GitHub issue and the local record together."""

    def __init__(self, store: Store, targets: Sequence[ApprovalTarget]) -> None:
        """Serve the given projects; a single unnamed target is a one-project garage."""
        self.store = store
        self.targets = list(targets)
        self._lock = asyncio.Lock()

    def target(self, item: InboxItem) -> ApprovalTarget | None:
        """Return the project an item belongs to, by name, else by repository."""
        for target in self.targets:
            if item.project is not None and target.name == item.project:
                return target
        repo = item.repos[0].full_name if item.repos else None
        return next((target for target in self.targets if repo and target.repo.lower() == repo), None)

    def waiting(self, project: str | None = None) -> list[InboxItem]:
        """Items waiting for the owner's approval, oldest first."""
        return list(reversed(self.store.items(ItemState.awaiting_approval, project)))

    def find(self, ref: str, project: str | None = None) -> InboxItem:
        """Find an item by Garage id (``item_...``), issue URL, or issue number (``42`` or ``#42``)."""
        ref = ref.strip()
        if ref.startswith("item_"):
            item = self.store.get(ref)
            if item is None:
                raise ApprovalError(f"no inbox item {ref}")
            return item
        url = re.fullmatch(r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)/?", ref)
        if url:
            item = self.store.by_issue(url.group(1), int(url.group(2)))
            if item is None:
                raise ApprovalError(f"no inbox item for {ref}")
            return item
        if not re.fullmatch(r"#?\d+", ref):
            raise ApprovalError(f"{ref!r} is not an item id, issue number or issue URL")
        number = int(ref.lstrip("#"))
        targets = [target for target in self.targets if project is None or target.name == project]
        found = [item for target in targets if (item := self.store.by_issue(target.repo, number)) is not None]
        if not found:
            raise ApprovalError(f"no inbox item for issue #{number}" + (f" in project {project}" if project else ""))
        if len(found) > 1:
            names = ", ".join(str(item.project) for item in found)
            raise ApprovalError(f"issue #{number} exists in several projects ({names}); add --project")
        return found[0]

    def _tracker(self, item: InboxItem) -> tuple[ApprovalTarget | None, Tracker | None]:
        target = self.target(item)
        if item.issue is not None and (target is None or target.tracker is None):
            raise ApprovalError(
                f"{item.id} is GitHub issue #{item.issue.number}, but Garage has no GitHub token for "
                f"{target.repo if target else 'its repository'}; set GARAGE_GITHUB_TOKEN"
            )
        return target, target.tracker if target is not None else None

    async def approve(self, item: InboxItem, by: str, via: str = "cli") -> InboxItem:
        """Approve: label the issue ``factory:ready-for-fix``, mark the item approved, and wake the dispatcher."""
        async with self._lock:
            item = self.store.get(item.id) or item
            if item.state not in BEFORE_APPROVAL:
                raise ApprovalError(f"{item.id} is {item.state.value}, not waiting for approval")
            if item.issue is None:
                raise ApprovalError(f"{item.id} has no GitHub issue yet; file it first (garage assign {item.id})")
            target, tracker = self._tracker(item)
            assert tracker is not None
            await tracker.add_labels(item.issue.number, [READY])
            await tracker.remove_label(item.issue.number, PROPOSED)
            await tracker.comment(item.issue.number, f"Approved by {by} from the Garage {via}. {_after(item)}")
            item.state = ItemState.approved
            self.store.save(item, by, "approve", {"via": via})
        emit("factory.approved", "factory", item=item.id, issue=item.issue.number, via=via)
        if next_step(item) == "dispatch" and target is not None and target.on_approved is not None:
            target.on_approved()
        return item

    async def decline(self, item: InboxItem, by: str, via: str = "cli") -> InboxItem:
        """Decline: close the issue as not planned and mark the item declined; no agent will start."""
        async with self._lock:
            item = self.store.get(item.id) or item
            if item.state not in BEFORE_APPROVAL | {ItemState.approved, ItemState.needs_repo}:
                raise ApprovalError(f"{item.id} is {item.state.value}; only work that has not started can be declined")
            if item.issue is not None:
                _, tracker = self._tracker(item)
                assert tracker is not None
                await tracker.comment(item.issue.number, f"Declined by {by} from the Garage {via}. Closing.")
                await tracker.remove_label(item.issue.number, PROPOSED)
                await tracker.update_issue(item.issue.number, state="closed", state_reason="not_planned")
            item.state = ItemState.declined
            self.store.save(item, by, "decline", {"via": via})
        emit("factory.declined", "factory", item=item.id, issue=item.issue.number if item.issue else None, via=via)
        return item


def _after(item: InboxItem) -> str:
    step = next_step(item)
    return "A coding agent starts on the next dispatch poll." if step == "dispatch" else f"Next: {step}."


class IntakeCursor(BaseModel):
    """How far the watcher has read one repository's issues."""

    baseline: datetime
    since: datetime | None = None
    etag: str | None = None


class IssueWatcher:
    """Poll one project's repository for new and changed issues and keep the inbox in step with GitHub.

    New issues opened after the watcher first started (its *baseline*) are recorded and labeled
    ``factory:proposed``. Changes made on GitHub (approve by label, decline by closing, reopen) are copied into the
    inbox. Pull requests, closed issues, issues older than the baseline and issues an agent already has are skipped.
    """

    def __init__(
        self,
        store: Store,
        tracker: IssueFeed,
        cursor_path: Path,
        *,
        project: str | None,
        needs_approval: bool = True,
        interval_s: float = 60,
        dry_run: bool = False,
        backfill_s: float = 0,
        rescan_s: float | None = None,
        on_approved: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """Configure the watched repository.

        ``backfill_s`` also adopts issues opened that long before the first start; ``rescan_s`` re-reads that far
        back on this run (for ``garage intake --since``).
        """
        self.store = store
        self.tracker = tracker
        self.cursor_path = cursor_path
        self.project = project
        self.needs_approval = needs_approval
        self.interval_s = interval_s
        self.dry_run = dry_run
        self.backfill_s = backfill_s
        self.rescan_s = rescan_s
        self.on_approved = on_approved
        self.clock = clock
        self.actions: list[str] = []
        self._labels_ready = False

    def _load(self) -> IntakeCursor:
        try:
            return IntakeCursor.model_validate_json(self.cursor_path.read_text())
        except FileNotFoundError:
            return IntakeCursor(baseline=self.clock() - timedelta(seconds=self.backfill_s))

    def _save(self, cursor: IntakeCursor) -> None:
        if self.dry_run:
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursor.model_dump(mode="json"), indent=2))
        tmp.replace(self.cursor_path)

    async def run(self) -> None:
        """Poll forever; a failed poll is logged and retried on the next tick."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Issue intake poll failed for %s", self.tracker.repo)
                emit("factory.intake_failed", "factory", "error", repo=self.tracker.repo, error=str(exc))
            await asyncio.sleep(self.interval_s)

    async def poll_once(self) -> list[str]:
        """Read issues changed since the last poll and apply them; return what was done."""
        cursor = self._load()
        if not self.cursor_path.exists():
            self._save(cursor)
        if self.rescan_s is not None:
            cursor.baseline = min(cursor.baseline, self.clock() - timedelta(seconds=self.rescan_s))
            cursor.since, cursor.etag = None, None
        issues, etag, unchanged = await self.tracker.changed_since(cursor.since or cursor.baseline, cursor.etag)
        if unchanged:
            return []
        if issues and not self._labels_ready and not self.dry_run:
            await self.tracker.ensure_labels(LABELS)
            self._labels_ready = True
        done: list[str] = []
        failed = False
        for issue in issues:
            try:
                action = await self._apply(issue, cursor.baseline)
            except Exception as exc:
                logger.exception("Could not take in %s#%d", self.tracker.repo, issue.number)
                emit("factory.intake_failed", "factory", "error", issue=issue.number, error=str(exc))
                failed = True
                break
            if action:
                done.append(action)
            if issue.updated_at is not None:
                cursor.since = issue.updated_at
        cursor.etag = None if failed or done else etag
        self._save(cursor)
        self.actions.extend(done)
        return done

    def _linked(self, issue: IssueRecord) -> InboxItem | None:
        item = self.store.by_issue(self.tracker.repo, issue.number)
        if item is not None:
            return item
        request_id = issue.marker("request")
        candidate = self.store.get(request_id) if request_id else None
        if candidate is not None and candidate.issue is None:
            return self._link(candidate, issue)
        return None

    def _link(self, item: InboxItem, issue: IssueRecord) -> InboxItem:
        item.issue = IssueLink(number=issue.number, url=issue.url)
        if not self.dry_run:
            self.store.save(item, GITHUB_CLIENT, "link_issue")
        return item

    def owns(self, issue: IssueRecord, baseline: datetime) -> str | None:
        """Why Garage should not take in an issue it has no record of, or None when it should."""
        if issue.pull_request:
            return "pull request"
        if issue.state != "open":
            return "closed"
        if issue.created_at < baseline:
            return "opened before Garage started watching"
        if DISPATCHED in issue.labels or FIXING in issue.labels:
            return "already handed to an agent"
        if OBSERVED in issue.labels and PROPOSED not in issue.labels and READY not in issue.labels:
            return "incident below its threshold"
        return None

    async def _apply(self, issue: IssueRecord, baseline: datetime) -> str | None:
        if issue.pull_request:
            return None
        item = self._linked(issue)
        if item is None:
            if self.owns(issue, baseline) is not None:
                return None
            return await self._adopt(issue)
        return await self._reconcile(item, issue)

    async def _adopt(self, issue: IssueRecord) -> str:
        state = initial_state(issue.labels, self.needs_approval)
        label = f"#{issue.number} {issue.title}"
        if self.dry_run:
            return f"take in {label} as {state.value}"
        fingerprint = issue.marker("fingerprint") if INCIDENT in issue.labels else None
        kind = ItemKind.incident if fingerprint else ItemKind.ask
        client, trust = (MONITOR_CLIENT, ClientKind.agent) if fingerprint else (GITHUB_CLIENT, ClientKind.public)
        text = issue.title if fingerprint else f"{issue.title}\n\n{issue.body}".strip()
        work = SubmitWork(
            id=fingerprint or f"github-{self.tracker.repo.replace('/', '-')}-{issue.number}",
            kind=kind,
            text=text[:4000],
            repo=self.tracker.repo,
            created_at=issue.created_at,
            source=Source(client="github", channel=issue.author),
            context={"issue_title": issue.title, "author": issue.author},
        )
        item, _ = self.store.submit(client, trust, work, self.tracker.repo, self.project)
        item.issue = IssueLink(number=issue.number, url=issue.url)
        item.state = state
        self.store.save(item, GITHUB_CLIENT, "intake", {"labels": issue.labels})
        if state == ItemState.awaiting_approval and PROPOSED not in issue.labels:
            await self._propose(item, issue)
        elif state == ItemState.approved and READY not in issue.labels:
            await self.tracker.add_labels(issue.number, [READY])
        emit("factory.intake", "factory", issue=issue.number, item=item.id, state=state.value, project=self.project)
        return f"take in {label} as {state.value} ({item.id})"

    async def _propose(self, item: InboxItem, issue: IssueRecord) -> None:
        await self.tracker.add_labels(issue.number, [PROPOSED])
        if REQUEST in issue.labels or INCIDENT in issue.labels:
            return
        await self.tracker.comment(
            issue.number,
            f"Garage picked this up as `{item.id}` and labeled it `{PROPOSED}`. No coding agent starts until it is "
            f"approved.\n\n- Approve: add the `{READY}` label, click Approve under Waiting for you on the Garage "
            f"dashboard, or run `garage approve {item.id}`.\n- Decline: close this issue, click Decline, or run "
            f"`garage decline {item.id}`.",
        )

    async def _reconcile(self, item: InboxItem, issue: IssueRecord) -> str | None:
        before = item.state
        tag = f"#{issue.number}"
        if issue.state == "closed":
            if item.state in FINISHED:
                return None
            if item.state in BEFORE_APPROVAL | {ItemState.needs_repo, ItemState.approved}:
                item.state = ItemState.declined
            else:
                item.state = ItemState.cancelled if issue.state_reason == "not_planned" else ItemState.done
            if self.dry_run:
                return f"{tag} closed on GitHub: {before.value} -> {item.state.value}"
            if PROPOSED in issue.labels:
                await self.tracker.remove_label(issue.number, PROPOSED)
            self.store.save(item, GITHUB_CLIENT, "github_closed", {"state_reason": issue.state_reason})
            return f"{tag} closed on GitHub: {before.value} -> {item.state.value}"
        if item.state in {ItemState.declined, ItemState.done}:
            item.state = initial_state(issue.labels, self.needs_approval)
            if self.dry_run:
                return f"{tag} reopened on GitHub: {before.value} -> {item.state.value}"
            self.store.save(item, GITHUB_CLIENT, "github_reopened")
            if item.state == ItemState.awaiting_approval and PROPOSED not in issue.labels:
                await self._propose(item, issue)
            return f"{tag} reopened on GitHub: {before.value} -> {item.state.value}"
        if READY in issue.labels and item.state in BEFORE_APPROVAL:
            item.state = ItemState.approved
            if self.dry_run:
                return f"{tag} approved on GitHub: {before.value} -> approved"
            self.store.save(item, GITHUB_CLIENT, "approve", {"via": "github"})
            if PROPOSED in issue.labels:
                await self.tracker.remove_label(issue.number, PROPOSED)
                await self.tracker.comment(issue.number, f"Approved on GitHub. {_after(item)}")
            emit("factory.approved", "factory", item=item.id, issue=issue.number, via="github")
            if next_step(item) == "dispatch" and self.on_approved is not None:
                self.on_approved()
            return f"{tag} approved on GitHub: {before.value} -> approved"
        if READY in issue.labels and item.state == ItemState.approved and DISPATCHED not in issue.labels:
            # Approved elsewhere (another process, such as `garage approve`): make sure the dispatcher hears of it.
            if not self.dry_run and next_step(item) == "dispatch" and self.on_approved is not None:
                self.on_approved()
            return None
        if (FIXING in issue.labels or DISPATCHED in issue.labels) and item.state == ItemState.approved:
            item.state = ItemState.running
            if not self.dry_run:
                self.store.save(item, GITHUB_CLIENT, "github_dispatched")
            return f"{tag} dispatched: approved -> running"
        if PROPOSED in issue.labels and item.state == ItemState.new:
            item.state = ItemState.awaiting_approval
            if not self.dry_run:
                self.store.save(item, GITHUB_CLIENT, "github_proposed")
            return f"{tag} proposed: new -> awaiting_approval"
        if item.state == ItemState.awaiting_approval and PROPOSED not in issue.labels and READY not in issue.labels:
            if self.dry_run:
                return f"label {tag} {PROPOSED}"
            await self._propose(item, issue)
            return f"label {tag} {PROPOSED}"
        return None
