"""Dispatch ready issues to coding-agent sessions."""

from __future__ import annotations
import os
import json
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Callable

import httpx
from pydantic import Field, BaseModel

from garage_sdk import emit, scrub_text
from garage.report import READY, FIXING, LABELS, BLOCKED, DISPATCHED
from garage.tracker import (
    ATTEMPTS_END,
    ATTEMPTS_START,
    Tracker,
    IssueRecord,
    canonical_label,
    pull_request_repo,
    replace_attempt_block,
)
from garage_sdk.events import EventLevel
from garage.agents.base import CodingAgent, SessionStatus
from garage.agents.local import LocalCapacityError
from garage.dependencies import DependencyCheck
from garage_sdk.manifest import Manifest


logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 6000
OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "pr_url": {"type": ["string", "null"]},
        "test_result": {"type": "string", "enum": ["passed", "failed", "skipped"]},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["summary", "pr_url", "test_result", "risk"],
}
_DONE = {SessionStatus.finished, SessionStatus.failed, SessionStatus.expired}


class DispatchRecord(BaseModel):
    """One fix session started for one issue."""

    issue: int
    session_id: str
    session_url: str
    started_at: datetime
    project: str | None = None
    labeled: bool = False
    done: bool = False
    status: str | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    attempt: int = 0
    outcome: str | None = None
    failure: str | None = None
    summary: str | None = None

    @property
    def in_flight(self) -> bool:
        """Whether this attempt still holds one of its project's slots: session running, or its PR still open."""
        return self.outcome is None and (
            not self.done or (self.pr_url is not None and self.pr_state in (None, "open"))
        )

    @property
    def public_reference(self) -> str:
        """Describe local sessions without publishing filesystem paths."""
        if self.session_url.lower().startswith("file:"):
            return f"local run {self.session_id}, branch garage/{self.session_id}"
        return self.session_url


class DispatchLedger(BaseModel):
    """Every dispatch this machine has made, newest last."""

    records: list[DispatchRecord] = Field(default_factory=list)
    dependency_holds: dict[int, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> DispatchLedger:
        """Read the ledger, or start an empty one."""
        if not path.exists():
            return cls()
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        """Write the ledger atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.model_dump_json(indent=2))
        os.replace(tmp, path)

    def latest(self, issue: int) -> DispatchRecord | None:
        """Return the newest record for an issue."""
        return next((record for record in reversed(self.records) if record.issue == issue), None)


def attempt_block(record: DispatchRecord, max_attempts: int) -> str:
    """Render bounded, scrubbed context for the next attempt."""
    prefix = f"{ATTEMPTS_START}\n### Previous attempt\nAttempt {record.attempt} of {max_attempts}\n"
    failure = scrub_text(record.failure or "unknown failure")[:500]
    suffix = f"\nPR: {scrub_text(record.pr_url or 'none')[:400]}\n{ATTEMPTS_END}"
    prefix += f"Failed: {failure}\nSummary: "
    summary = scrub_text(record.summary or "none")
    return prefix + summary[: 1499 - len(prefix) - len(suffix)] + suffix


def _replace_prompt_values(text: str, *, repo: str, issue: int, outpost: str, command: str = "") -> str:
    return (
        text.replace("{repo}", repo)
        .replace("{issue}", str(issue))
        .replace("{outpost}", outpost)
        .replace("{command}", command)
    )


def build_fix_prompt(issue: IssueRecord, repo: str, manifest: Manifest) -> str:
    """Build a scrubbed, manifest-driven prompt for a coding-agent fix session."""
    body = (
        "\n".join(scrub_text(line) for line in issue.body.splitlines())
        if ATTEMPTS_START in issue.body
        else scrub_text(issue.body)
    ).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[... truncated]"
    outpost = manifest.dispatch.outpost or ""
    base = manifest.dispatch.base_branch
    protected = base if base == "main" else f"{base} or main"
    lines = [
        f"Fix GitHub issue #{issue.number} in {repo}: {scrub_text(issue.title)}",
        f"Issue: {issue.url}",
        f"Started automatically by garage for {manifest.app.id} (Dispatch).",
        "",
        "Issue body (comments are not included):",
        f"<<<\n{body}\n>>>",
        "",
        "Rules:",
        f"- Work only in {repo}. Branch off the latest {base} and open ONE pull request into {base} "
        f"with 'Closes #{issue.number}' in its body. Never merge, never push directly to {protected}, never deploy.",
    ]
    if ATTEMPTS_START in issue.body:
        lines.append(
            '- An earlier attempt failed (see "Previous attempt" at the top of the issue body). '
            "Garage closed its pull request; start fresh from the latest base, open a NEW pull request, "
            "and fix what failed."
        )
    checks = manifest.build.check
    if checks:
        rendered = [f"`{command}`" for command in checks]
        if len(rendered) == 1:
            commands = rendered[0]
        elif len(rendered) == 2:
            commands = " and ".join(rendered)
        else:
            commands = ", ".join(rendered[:-1]) + " and " + rendered[-1]
        lines.append(f"- Before opening the PR, pass {commands}.")
    for kit in manifest.test_kit:
        command = _replace_prompt_values(
            kit.command,
            repo=repo,
            issue=issue.number,
            outpost=outpost,
        )
        instructions = _replace_prompt_values(
            kit.instructions or "",
            repo=repo,
            issue=issue.number,
            outpost=outpost,
            command=command,
        )
        if kit.runs_on.startswith("outpost:") and instructions:
            lines.append(f"- {instructions}")
        else:
            line = f"- Run `{command}` and report the result in the PR."
            if instructions:
                line += f" {instructions}"
            lines.append(line)
    for rule in manifest.dispatch.prompt_rules:
        lines.append(
            "- "
            + _replace_prompt_values(
                rule,
                repo=repo,
                issue=issue.number,
                outpost=outpost,
            )
        )
    lines.extend(
        [
            "- If you can't find a fix you are confident in, don't open a PR; explain what you found in the summary.",
            "- When done, fill the structured output: summary (plain language, no secrets), pr_url, test_result, risk.",
        ]
    )
    return "\n".join(lines)


def _has_summary(output: dict[str, object]) -> bool:
    summary = output.get("summary")
    return isinstance(summary, str) and bool(summary.strip())


def _error_detail(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        return f"HTTP {response.status_code}: {response.text[:500]}"
    return str(error)


class Dispatcher:
    """Poll for ready issues and start one coding-agent session for each."""

    def __init__(
        self,
        tracker: Tracker,
        agent: CodingAgent,
        ledger_path: Path,
        *,
        manifest: Manifest,
        interval_s: float,
        dry_run: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        legacy_ledger_path: Path | None = None,
        project: str | None = None,
        hold: Callable[[IssueRecord], str | None] | None = None,
        max_open: int = 10,
        max_attempts: int = 2,
    ) -> None:
        """Configure the tracker, agent, ledger, and polling behavior."""
        if max_open < 1:
            raise ValueError("max_open must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts
        self.project = project
        self.max_open = max_open
        self.hold = hold
        self.tracker = tracker
        self.agent = agent
        self.ledger_path = ledger_path
        self.legacy_ledger_path = legacy_ledger_path
        self.manifest = manifest
        self.interval_s = interval_s
        self.dry_run = dry_run
        self.clock = clock
        self._labels_ready = False
        self._wake = asyncio.Event()

    def _emit(self, type: str, component: str, level: EventLevel = "info", **data: object) -> None:
        if self.project:
            data["project"] = self.project
        emit(type, component, level, **data)

    def poke(self) -> None:
        """Wake the dispatch loop to check for new ready issues."""
        self._wake.set()

    def _load_ledger(self) -> DispatchLedger:
        if self.ledger_path.exists():
            return DispatchLedger.load(self.ledger_path)
        if self.legacy_ledger_path is not None and self.legacy_ledger_path.exists():
            return DispatchLedger.load(self.legacy_ledger_path)
        return DispatchLedger()

    def _save_ledger(self, ledger: DispatchLedger) -> None:
        ledger.save(self.ledger_path)

    async def run(self) -> None:
        """Poll forever, starting with one pass right away."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Garage dispatch failed")
                self._emit("factory.dispatch_failed", "factory", "error", exc_type=type(exc).__name__, error=str(exc))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass
            self._wake.clear()

    def request_status(self, number: int) -> dict[str, object]:
        """Return the latest dispatch state for a filed request."""
        record = self._load_ledger().latest(number)
        if record is None:
            return {"state": "queued", "session_url": None, "pr_url": None}
        state = record.outcome or ((record.status or "finished") if record.done else "working")
        return {
            "state": state,
            "session_url": record.session_url,
            "pr_url": record.pr_url,
        }

    async def poll_once(self) -> list[str]:
        """Follow up on running sessions, then dispatch new ready issues."""
        ledger = self._load_ledger()
        done = await self._follow_up(ledger)
        await self._refresh_pull_requests(ledger)
        if not self._labels_ready and not self.dry_run:
            await self.tracker.ensure_labels(LABELS)
            self._labels_ready = True
        ready = sorted(await self.tracker.list_issues(READY), key=lambda item: item.number)
        dependencies = DependencyCheck(
            self.tracker.repo, self.tracker.get_issue, known={issue.number: issue for issue in ready}
        )
        planned: list[int] = []
        for issue in ready:
            action = await self._consider(ledger, issue, dependencies, planned)
            if action:
                done.append(action)
        return done

    def _own(self, ledger: DispatchLedger) -> list[DispatchRecord]:
        return [record for record in ledger.records if record.project in (None, self.project)]

    def in_flight(self, ledger: DispatchLedger) -> list[int]:
        """Issues holding one of this project's ``max_open`` slots (any attempt still in flight), lowest first."""
        return sorted({record.issue for record in self._own(ledger) if record.in_flight})

    async def _refresh_pull_requests(self, ledger: DispatchLedger) -> None:
        changed = False
        for record in self._own(ledger):
            if record.outcome == "superseded":
                if not self.dry_run:
                    changed = await self._close_superseded(record, record.issue) or changed
                continue
            if not record.done or record.outcome is not None:
                continue
            try:
                if record.attempt == 0:
                    if record.pr_url and record.pr_state in (None, "open"):
                        pull = await self.tracker.get_pull_request(record.pr_url)
                        record.pr_state = pull.state if pull else "missing"
                        changed = True
                    continue
                pull = await self.tracker.get_pull_request(record.pr_url) if record.pr_url else None
                record.pr_state = pull.state if pull else "missing"
                changed = True
                if self.dry_run:
                    continue
                if pull and pull.state == "merged":
                    issue = await self.tracker.get_issue(record.issue)
                    if issue and issue.state == "open":
                        await self.tracker.comment(record.issue, f"Merged: {record.pr_url}. Closing as completed.")
                        await self.tracker.update_issue(record.issue, state="closed", state_reason="completed")
                    record.outcome = "merged"
                    self._save_ledger(ledger)
                    self._emit("factory.dispatch_merged", "factory", issue=record.issue, pr_url=record.pr_url)
                    continue
                failure = None
                if not record.pr_url:
                    failure = "no pull request"
                elif not pull or pull.state == "closed":
                    failure = "pull request closed without merging"
                else:
                    checks = await self.tracker.list_checks(pull)
                    if checks and all(check.completed for check in checks) and any(check.failed for check in checks):
                        failure = "checks failed: " + ", ".join(check.name for check in checks if check.failed)
                if failure:
                    issue = await self.tracker.get_issue(record.issue)
                    await self._attempt_failed(ledger, record, failure, issue)
            except Exception as exc:
                logger.warning("Could not watch issue #%s: %s", record.issue, _error_detail(exc))
                continue
        if changed and not self.dry_run:
            self._save_ledger(ledger)

    async def _attempt_failed(
        self, ledger: DispatchLedger, record: DispatchRecord, failure: str, issue: IssueRecord | None
    ) -> None:
        outcome = "failed"
        if issue is None or issue.state != "open":
            pass
        elif record.attempt < self.max_attempts:
            await self.tracker.update_issue(
                record.issue,
                body=replace_attempt_block(
                    issue.body, attempt_block(record.model_copy(update={"failure": failure}), self.max_attempts)
                ),
            )
            await self.tracker.comment(
                record.issue, f"Attempt {record.attempt} failed: {failure}. Starting attempt {record.attempt + 1}."
            )
            await self.tracker.remove_label(record.issue, DISPATCHED)
        else:
            await self.tracker.add_labels(record.issue, [BLOCKED])
            await self.tracker.comment(
                record.issue,
                f"Attempt {record.attempt} failed: {failure}. Remove `{BLOCKED}` to allow a fresh attempt.",
            )
            await self.tracker.remove_label(record.issue, DISPATCHED)
            outcome = "blocked"
        record.outcome = outcome
        record.failure = failure
        self._save_ledger(ledger)
        self._emit(
            "factory.dispatch_attempt_failed", "factory", issue=record.issue, attempt=record.attempt, failure=failure
        )
        if outcome == "blocked":
            self._emit("factory.dispatch_blocked", "factory", issue=record.issue, attempt=record.attempt)
        elif issue is not None and issue.state == "open":
            self._emit("factory.dispatch_retry", "factory", issue=record.issue, attempt=record.attempt + 1)

    def _cap_hold(self, ledger: DispatchLedger, issue: int, planned: list[int]) -> str | None:
        busy = sorted({*self.in_flight(ledger), *planned})
        if issue in busy or len(busy) < self.max_open:
            return None
        where = f" in project {self.project}" if self.project else ""
        refs = ", ".join(f"#{number}" for number in busy)
        return f"waiting for a free slot: {len(busy)} of {self.max_open} in flight{where} ({refs})"

    async def _consider(
        self, ledger: DispatchLedger, issue: IssueRecord, dependencies: DependencyCheck, planned: list[int]
    ) -> str | None:
        if BLOCKED in {canonical_label(label) for label in issue.labels}:
            return None
        if issue.state != "open" or DISPATCHED in issue.labels:
            return None
        record = next((record for record in reversed(self._own(ledger)) if record.issue == issue.number), None)
        if record is not None and not record.labeled:
            if self.dry_run:
                return f"label #{issue.number} (session already started: {record.session_url})"
            await self._label(ledger, record)
            return f"label #{issue.number} (session already started: {record.session_url})"
        reason = self.hold(issue) if self.hold is not None else None
        if reason is not None:
            return f"hold #{issue.number}: {reason}"
        reason = await dependencies.hold(issue)
        if reason is not None:
            if not self.dry_run and issue.number not in ledger.dependency_holds:
                await self._note_dependency_hold(ledger, issue.number, reason)
            return f"hold #{issue.number}: {reason}"
        reason = self._cap_hold(ledger, issue.number, planned)
        if reason is not None:
            return f"hold #{issue.number}: {reason}"
        if self.dry_run:
            planned.append(issue.number)
            return f"start session for #{issue.number}: {issue.title}"
        if ledger.dependency_holds.pop(issue.number, None) is not None:
            self._save_ledger(ledger)
        return await self._dispatch(ledger, issue)

    async def _note_dependency_hold(self, ledger: DispatchLedger, number: int, reason: str) -> None:
        await self.tracker.comment(
            number,
            f"Not starting yet: {reason}.\n\n"
            "Garage starts it once every issue named on a `Depends on` line is closed as completed.",
        )
        ledger.dependency_holds[number] = reason
        self._save_ledger(ledger)
        self._emit("factory.dispatch_waiting", "factory", issue=number, reason=reason)

    async def _dispatch(self, ledger: DispatchLedger, issue: IssueRecord) -> str | None:
        repo = self.tracker.repo
        previous = next((record for record in reversed(self._own(ledger)) if record.issue == issue.number), None)
        retry = previous is not None and (
            previous.outcome == "failed" or (previous.outcome is None and previous.done and previous.attempt >= 1)
        )
        attempt = previous.attempt + 1 if previous is not None and retry else 1
        try:
            created = await self.agent.create_session(
                build_fix_prompt(issue, repo, self.manifest),
                title=f"Fix #{issue.number}: {scrub_text(issue.title)}"[:80],
                tags=["factory", "dispatch", *([f"project:{self.project}"] if self.project else [])],
                structured_output_schema=OUTPUT_SCHEMA,
                repos=[repo],
            )
        except LocalCapacityError as exc:
            logger.warning("Deferred #%d: %s; will retry on the next poll", issue.number, exc)
            self._emit("factory.dispatch_deferred", "factory", "warning", issue=issue.number, reason=str(exc))
            return f"deferred #{issue.number}: {exc}; will retry on the next poll"
        except Exception as exc:
            detail = _error_detail(exc)
            logger.error("Could not start a fix session for #%d: %s", issue.number, detail)
            self._emit("factory.dispatch_failed", "factory", "error", issue=issue.number, error=detail)
            return f"failed to start session for #{issue.number}: {detail}"
        record = DispatchRecord(
            issue=issue.number,
            session_id=created.session_id,
            session_url=created.url,
            started_at=self.clock(),
            project=self.project,
            attempt=attempt,
        )
        ledger.records.append(record)
        self._save_ledger(ledger)
        for earlier in self._own(ledger):
            if earlier is record or earlier.issue != issue.number:
                continue
            await self._close_superseded(earlier, issue.number)
            if earlier.outcome is None:
                earlier.outcome = "superseded"
        self._save_ledger(ledger)
        self._emit("factory.dispatched", "factory", issue=issue.number, session_id=created.session_id)
        await self._label(ledger, record)
        return f"started {created.url} for #{issue.number}: {issue.title}"

    async def _close_superseded(self, record: DispatchRecord, issue_number: int) -> bool:
        if not record.pr_url or record.pr_state not in (None, "open") or record.outcome == "merged":
            return False
        repo = self.tracker.repo
        pr_repo = pull_request_repo(record.pr_url)
        if pr_repo is None or pr_repo.lower() != repo.lower():
            logger.warning("Not closing pull request outside repository %s: %s", repo, record.pr_url)
            return False
        try:
            await self.tracker.close_pull_request(
                record.pr_url, f"Superseded by a new Garage attempt for #{issue_number}."
            )
        except Exception as exc:
            logger.warning("Could not close superseded pull request %s: %s", record.pr_url, _error_detail(exc))
            return False
        record.pr_state = "closed"
        self._emit("factory.dispatch_pr_closed", "factory", issue=issue_number, pr_url=record.pr_url)
        return True

    async def _label(self, ledger: DispatchLedger, record: DispatchRecord) -> None:
        await self.tracker.add_labels(record.issue, [DISPATCHED, FIXING])
        await self.tracker.comment(
            record.issue,
            f"Started a coding-agent fix session: {record.public_reference}\n\n"
            f"Remove `{DISPATCHED}` to allow another attempt.\n\n"
            f"<!-- factory-session: {record.session_id} -->",
        )
        record.labeled = True
        self._save_ledger(ledger)

    async def _follow_up(self, ledger: DispatchLedger) -> list[str]:
        done: list[str] = []
        if self.dry_run:
            return done
        for record in ledger.records:
            if record.project not in (None, self.project) or record.done or not record.labeled:
                continue
            try:
                session = await self.agent.get_session(record.session_id)
            except Exception as exc:
                logger.warning("Could not refresh fix session %s: %s", record.session_id, exc)
                continue
            status = self.agent.map_status(session.status.lower(), (session.status_detail or "").lower())
            output = session.structured_output or {}
            output_pr_url = output.get("pr_url")
            has_pr = (isinstance(output_pr_url, str) and bool(output_pr_url)) or bool(session.pull_requests)
            if status in {SessionStatus.blocked, SessionStatus.expired} and (_has_summary(output) or has_pr):
                status = SessionStatus.finished
            if status is None or status not in _DONE:
                continue
            pr_url: str | None = output_pr_url if isinstance(output_pr_url, str) and output_pr_url else None
            if pr_url is None and session.pull_requests:
                pr_url = session.pull_requests[-1].pr_url
            summary = output.get("summary")
            if self.dry_run:
                done.append(f"report outcome on #{record.issue}")
                continue
            lines = [f"Coding-agent fix session ended ({status.value}): {record.public_reference}"]
            lines.append(f"Pull request: {pr_url}" if pr_url else "No pull request was opened.")
            test_result = output.get("test_result")
            if not isinstance(test_result, str):
                test_result = output.get("robot_test")
            if isinstance(test_result, str):
                lines.append(f"Test result: {scrub_text(test_result)}")
            if isinstance(summary, str) and summary.strip():
                lines += ["", scrub_text(summary).strip()[:2000]]
            await self.tracker.comment(record.issue, "\n".join(lines))
            await self.tracker.remove_label(record.issue, FIXING)
            record.done = True
            record.status = status.value
            record.pr_url = pr_url
            record.attempt = record.attempt or 1
            record.summary = scrub_text(summary) if isinstance(summary, str) else None
            self._save_ledger(ledger)
            self._emit(
                "factory.dispatch_ended", "factory", issue=record.issue, status=status.value, has_pr=bool(pr_url)
            )
            done.append(f"session for #{record.issue} ended ({status.value})")
        return done
