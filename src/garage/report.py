"""Turn detected incidents and voice requests into GitHub issues.

One incident fingerprint maps to one issue, found again after a restart through a hidden
``<!-- factory-fingerprint: ... -->`` marker in the issue body. Everything posted is built
from already-scrubbed events and scrubbed once more, because the repository is public.
"""

from __future__ import annotations
import asyncio
import logging
from typing import TYPE_CHECKING
from datetime import datetime, timezone, timedelta
from collections.abc import Callable, Sequence

from garage_sdk import DROPPED_KEYS, emit, scrub_text
from garage.tracker import Tracker, IssueRecord
from garage_sdk.events import Event
from garage.incidents.base import Incident


if TYPE_CHECKING:
    from garage.requests import Request


logger = logging.getLogger(__name__)

INCIDENT = "factory:incident"
REQUEST = "factory:request"
OBSERVED = "factory:observed"
READY = "factory:ready-for-fix"
FIXING = "factory:fixing"
DISPATCHED = "factory:dispatched"
LABELS: dict[str, tuple[str, str]] = {
    INCIDENT: ("5319e7", "Filed automatically from an incident the app detected"),
    REQUEST: ("1d76db", "Fix or feature request submitted through the bridge"),
    OBSERVED: ("fbca04", "Seen, but below the threshold for a fix"),
    READY: ("d93f0b", "Ready for a Devin fix session"),
    FIXING: ("0e8a16", "A Devin session is working on it"),
    DISPATCHED: ("c5def5", "A Devin fix session was started; remove to allow another"),
}
MAX_SAMPLES = 10
DEFAULT_ATTRIBUTION = "Filed automatically by garage (Report) from an incident the app detected."
DEFAULT_REQUEST_ATTRIBUTION = "Filed automatically by garage."


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _clean(text: str) -> str:
    return scrub_text(text).replace("```", "'''").replace("\n", " ").strip()


def _sample_line(event: Event) -> str:
    details = " ".join(
        f"{key}={_clean(str(value))}" for key, value in event.data.items() if key.lower() not in DROPPED_KEYS
    )
    parts = [
        event.ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        event.type,
        event.component,
        event.level,
    ]
    return _clean("  ".join(parts)) + (f"  {details}" if details else "")


def _window(seconds: float) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


def incident_title(incident: Incident) -> str:
    """Return the scrubbed, length-limited issue title for an incident."""
    return _clean(incident.title)[:120] or incident.rule


def render_incident_body(
    incident: Incident,
    count: int,
    since: datetime | None,
    attribution: str = DEFAULT_ATTRIBUTION,
) -> str:
    """Render a deterministic issue body (no "updated at" stamp, so unchanged incidents produce no edit)."""
    samples = [event for event in incident.samples if since is None or event.ts > since][-MAX_SAMPLES:]
    group = ", ".join(f"`{_clean(key)}={_clean(value)}`" for key, value in sorted(incident.group.items())) or "none"
    since_note = f" (since it was reopened {_utc(since)})" if since else ""
    lines = [f"<!-- factory-fingerprint: {incident.fingerprint} -->"]
    if since is not None:
        lines.append(f"<!-- factory-since: {since.astimezone(timezone.utc).isoformat()} -->")
    lines += [
        attribution,
        "",
        "| | |",
        "| --- | --- |",
        f"| Rule | `{_clean(incident.rule)}` |",
        f"| Severity | {incident.severity} |",
        f"| Count | {count} / {incident.threshold} in {_window(incident.window_s)}{since_note} |",
        f"| First seen | {_utc(samples[0].ts if samples else incident.first_seen)} |",
        f"| Last seen | {_utc(incident.last_seen)} |",
        f"| Group | {group} |",
        f"| Detector | `{_clean(incident.detector)}` |",
        f"| Fingerprint | `{incident.fingerprint}` |",
        "",
        f"### Latest {len(samples)} events (scrubbed)",
        "",
        "```",
        *(_sample_line(event) for event in samples),
        "```",
        "",
        f"`{OBSERVED}` until the count reaches the threshold, then `{READY}`. "
        f"Add `{READY}` by hand to ask for a fix sooner.",
    ]
    return "\n".join(lines) + "\n"


def _since(issue: IssueRecord) -> datetime | None:
    value = issue.marker("since")
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class IncidentReporter:
    """Keep exactly one GitHub issue per incident fingerprint up to date."""

    def __init__(
        self,
        issues: Tracker,
        *,
        max_new_per_day: int = 5,
        min_update_interval: timedelta = timedelta(minutes=10),
        dry_run: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        attribution: str = DEFAULT_ATTRIBUTION,
    ) -> None:
        """Configure the GitHub client, safety caps, and dry-run mode."""
        self.issues = issues
        self.max_new_per_day = max_new_per_day
        self.min_update_interval = min_update_interval
        self.dry_run = dry_run
        self.clock = clock
        self.attribution = attribution
        self.actions: list[str] = []
        self._index: dict[str, IssueRecord] | None = None
        self._labels_ready = False
        self._last_edit: dict[int, datetime] = {}
        self._capped_day: str | None = None
        self._latest: list[Incident] = []
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def issue_url(self, fingerprint: str) -> str | None:
        """Return the issue URL already linked to a fingerprint, if known."""
        issue = (self._index or {}).get(fingerprint)
        return issue.url if issue else None

    def notify(self, incidents: list[Incident]) -> None:
        """Receive the latest detector output; the run loop posts it."""
        self._latest = list(incidents)
        if self._wake is not None and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    async def run(self, retry_s: float = 900.0) -> None:
        """Post new detector output as it arrives, retrying periodically after failures."""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=retry_s)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.sync(self._latest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Factory report failed")
                emit("factory.report_failed", "factory", "error", exc_type=type(exc).__name__, error=str(exc))

    async def _prepare(self) -> dict[str, IssueRecord]:
        if not self._labels_ready and not self.dry_run:
            await self.issues.ensure_labels(LABELS)
            self._labels_ready = True
        if self._index is None:
            index: dict[str, IssueRecord] = {}
            for issue in await self.issues.list_issues(INCIDENT):
                fingerprint = issue.marker("fingerprint")
                if fingerprint and (fingerprint not in index or issue.number > index[fingerprint].number):
                    index[fingerprint] = issue
            self._index = index
        return self._index

    async def sync(self, incidents: Sequence[Incident]) -> list[str]:
        """Create, update, relabel, or reopen issues for the given incidents; return what was done."""
        index = await self._prepare()
        done: list[str] = []
        for incident in incidents:
            try:
                action = await self._sync_one(index, incident)
            except Exception as exc:
                logger.exception("Could not report incident %s", incident.fingerprint)
                emit(
                    "factory.report_failed",
                    "factory",
                    "error",
                    fingerprint=incident.fingerprint,
                    exc_type=type(exc).__name__,
                    error=str(exc),
                )
                continue
            if action:
                done.append(action)
        self.actions.extend(done)
        return done

    def _opened_today(self, index: dict[str, IssueRecord], now: datetime) -> int:
        today = now.astimezone(timezone.utc).date()
        return sum(1 for issue in index.values() if issue.created_at.astimezone(timezone.utc).date() == today)

    async def _sync_one(self, index: dict[str, IssueRecord], incident: Incident) -> str | None:
        now = self.clock()
        issue = index.get(incident.fingerprint)
        title = incident_title(incident)
        if issue is None:
            return await self._open(index, incident, title, now)
        if issue.state == "closed":
            return await self._reopen(index, issue, incident, now)
        since = _since(issue)
        if since is not None and incident.last_seen <= since:
            return None
        count = sum(1 for event in incident.samples if event.ts > since) if since else incident.count
        body = render_incident_body(incident, count, since, self.attribution)
        became_ready = count >= incident.threshold and READY not in issue.labels
        if self.dry_run:
            if became_ready:
                return f"label #{issue.number} {READY}: {title}"
            return f"update #{issue.number}: {title}" if body != issue.body else None
        actions: list[str] = []
        if became_ready:
            await self.issues.add_labels(issue.number, [READY])
            await self.issues.remove_label(issue.number, OBSERVED)
            await self.issues.comment(
                issue.number,
                f"Reached the threshold: {count} / {incident.threshold} in {_window(incident.window_s)}. "
                f"Labeled `{READY}`.",
            )
            issue.labels = [label for label in issue.labels if label != OBSERVED] + [READY]
            emit("factory.issue_ready", "factory", issue=issue.number, fingerprint=incident.fingerprint)
            actions.append(f"label #{issue.number} {READY}")
        last_edit = self._last_edit.get(issue.number)
        if body != issue.body and (became_ready or last_edit is None or now - last_edit >= self.min_update_interval):
            updated = await self.issues.update_issue(issue.number, body=body)
            issue.body = updated.body
            self._last_edit[issue.number] = now
            emit("factory.issue_updated", "factory", issue=issue.number, count=count)
            actions.append(f"update #{issue.number}")
        return f"{', '.join(actions)}: {title}" if actions else None

    async def _open(self, index: dict[str, IssueRecord], incident: Incident, title: str, now: datetime) -> str | None:
        if self._opened_today(index, now) >= self.max_new_per_day:
            day = now.astimezone(timezone.utc).date().isoformat()
            if self._capped_day != day:
                self._capped_day = day
                logger.warning(
                    "Factory daily cap of %d new issues reached; not filing more today", self.max_new_per_day
                )
                emit("factory.report_capped", "factory", "warning", cap=self.max_new_per_day)
            return f"skip (daily cap {self.max_new_per_day}): {title}"
        labels = [INCIDENT, READY if incident.ready else OBSERVED]
        body = render_incident_body(incident, incident.count, None, self.attribution)
        if self.dry_run:
            return f"open [{', '.join(labels)}]: {title}"
        issue = await self.issues.create_issue(title, body, labels)
        index[incident.fingerprint] = issue
        self._last_edit[issue.number] = now
        emit(
            "factory.issue_opened", "factory", issue=issue.number, fingerprint=incident.fingerprint, rule=incident.rule
        )
        return f"open #{issue.number} [{', '.join(labels)}]: {title}"

    async def _reopen(
        self, index: dict[str, IssueRecord], issue: IssueRecord, incident: Incident, now: datetime
    ) -> str | None:
        closed_at = issue.closed_at or now
        fresh = [event for event in incident.samples if event.ts > closed_at]
        if not fresh:
            return None
        title = incident_title(incident)
        ready = len(fresh) >= incident.threshold
        if self.dry_run:
            return f"reopen #{issue.number}: {title}"
        body = render_incident_body(incident, len(fresh), closed_at, self.attribution)
        updated = await self.issues.update_issue(issue.number, body=body, state="open")
        for stale in (READY, FIXING, OBSERVED):
            if stale in issue.labels:
                await self.issues.remove_label(issue.number, stale)
        await self.issues.add_labels(issue.number, [READY if ready else OBSERVED])
        await self.issues.comment(
            issue.number,
            f"Seen again after this issue was closed on {_utc(closed_at)}: {len(fresh)} time(s) since. Reopened.",
        )
        updated.labels = [label for label in updated.labels if label not in (READY, FIXING, OBSERVED)]
        updated.labels.append(READY if ready else OBSERVED)
        index[incident.fingerprint] = updated
        self._last_edit[issue.number] = now
        emit("factory.issue_reopened", "factory", issue=issue.number, fingerprint=incident.fingerprint)
        return f"reopen #{issue.number}: {title}"


def render_request_body(request: Request, attribution: str = DEFAULT_REQUEST_ATTRIBUTION) -> str:
    """Render the issue body for a submitted request."""
    text = scrub_text(request.request).strip()
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines()) or "> (empty)"
    return (
        f"<!-- factory-request: {request.id} -->\n"
        f"Requested on {_utc(request.created_at)}. {attribution}\n\n"
        f"{quoted}\n\n"
        f"Request `{request.id}`. Requests skip `{OBSERVED}` and go straight to `{READY}`; "
        f"the label changes to `{FIXING}` once a Devin session starts on it.\n"
    )


class RequestLogger:
    """File requests targeting this repository as ready-to-work issues."""

    def __init__(
        self,
        issues: Tracker,
        *,
        max_per_day: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        attribution: str = DEFAULT_REQUEST_ATTRIBUTION,
    ) -> None:
        """Configure the GitHub client and a daily cap against accidental wakes."""
        self.issues = issues
        self.max_per_day = max_per_day
        self.clock = clock
        self.attribution = attribution
        self._labels_ready = False
        self._filed: list[datetime] | None = None
        self._request_index: dict[str, IssueRecord] | None = None

    def wants(self, repository: str | None) -> bool:
        """Return whether a request targets this repository."""
        if not repository:
            return False
        target = repository.strip().lower()
        if target.startswith("https://github.com/"):
            target = target.removeprefix("https://github.com/")
        target = target.removesuffix(".git").strip("/")
        repo = self.issues.repo.lower()
        return target in {repo, repo.split("/")[-1]}

    async def _prepare(self) -> None:
        if self._labels_ready is False:
            await self.issues.ensure_labels(LABELS)
            self._labels_ready = True
        if self._filed is None or self._request_index is None:
            existing = await self.issues.list_issues(REQUEST)
            self._filed = [issue.created_at for issue in existing]
            self._request_index = {
                request_id: issue for issue in existing if (request_id := issue.marker("request")) is not None
            }

    async def file(self, request: Request) -> IssueRecord | None:
        """Open an issue for a request, or return None when it is not eligible."""
        if not self.wants(request.repository):
            return None
        now = self.clock()
        await self._prepare()
        assert self._filed is not None
        assert self._request_index is not None
        existing = self._request_index.get(request.id)
        if existing is not None:
            return existing
        today = now.astimezone(timezone.utc).date()
        if sum(1 for created in self._filed if created.astimezone(timezone.utc).date() == today) >= self.max_per_day:
            logger.warning("Garage daily cap of %d request issues reached", self.max_per_day)
            emit("factory.request_capped", "factory", "warning", request_id=request.id, cap=self.max_per_day)
            return None
        summary = " ".join(scrub_text(request.request).split())
        title = f"Request: {summary[:80]}{'…' if len(summary) > 80 else ''}"
        issue = await self.issues.create_issue(
            title,
            render_request_body(request, self.attribution),
            [REQUEST, READY],
        )
        self._filed.append(issue.created_at)
        self._request_index[request.id] = issue
        emit("factory.request_filed", "factory", issue=issue.number, request_id=request.id)
        return issue

    async def mark_fixing(self, number: int) -> None:
        """Move a request issue from ready to fixing once its Devin session has started."""
        await self.issues.add_labels(number, [FIXING])
        await self.issues.remove_label(number, READY)
