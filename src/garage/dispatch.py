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
from garage.report import READY, FIXING, LABELS, DISPATCHED
from garage.tracker import Tracker, IssueRecord
from garage.agents.base import CodingAgent, SessionStatus
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
    labeled: bool = False
    done: bool = False
    status: str | None = None
    pr_url: str | None = None


class DispatchLedger(BaseModel):
    """Every dispatch this machine has made, newest last."""

    records: list[DispatchRecord] = Field(default_factory=list)

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


def _replace_prompt_values(text: str, *, repo: str, issue: int, outpost: str, command: str = "") -> str:
    return (
        text.replace("{repo}", repo)
        .replace("{issue}", str(issue))
        .replace("{outpost}", outpost)
        .replace("{command}", command)
    )


def build_fix_prompt(issue: IssueRecord, repo: str, manifest: Manifest) -> str:
    """Build a scrubbed, manifest-driven prompt for a coding-agent fix session."""
    body = scrub_text(issue.body).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[... truncated]"
    outpost = manifest.dispatch.outpost or ""
    lines = [
        f"Fix GitHub issue #{issue.number} in {repo}: {scrub_text(issue.title)}",
        f"Issue: {issue.url}",
        f"Started automatically by garage for {manifest.app.id} (Dispatch).",
        "",
        "Issue body (comments are not included):",
        f"<<<\n{body}\n>>>",
        "",
        "Rules:",
        f"- Work only in {repo}. Branch off the latest main and open ONE pull request with "
        f"'Closes #{issue.number}' in its body. Never merge, never push to main, never deploy.",
    ]
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
    ) -> None:
        """Configure the tracker, agent, ledger, and polling behavior."""
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
                emit("factory.dispatch_failed", "factory", "error", exc_type=type(exc).__name__, error=str(exc))
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
        state = (record.status or "finished") if record.done else "working"
        return {
            "state": state,
            "session_url": record.session_url,
            "pr_url": record.pr_url,
        }

    async def poll_once(self) -> list[str]:
        """Follow up on running sessions, then dispatch new ready issues."""
        ledger = self._load_ledger()
        done = await self._follow_up(ledger)
        if not self._labels_ready and not self.dry_run:
            await self.tracker.ensure_labels(LABELS)
            self._labels_ready = True
        for issue in sorted(await self.tracker.list_issues(READY), key=lambda item: item.number):
            action = await self._consider(ledger, issue)
            if action:
                done.append(action)
        return done

    async def _consider(self, ledger: DispatchLedger, issue: IssueRecord) -> str | None:
        if issue.state != "open" or DISPATCHED in issue.labels:
            return None
        record = ledger.latest(issue.number)
        if record is not None and not record.labeled:
            if self.dry_run:
                return f"label #{issue.number} (session already started: {record.session_url})"
            await self._label(ledger, record)
            return f"label #{issue.number} (session already started: {record.session_url})"
        if self.dry_run:
            return f"start session for #{issue.number}: {issue.title}"
        return await self._dispatch(ledger, issue)

    async def _dispatch(self, ledger: DispatchLedger, issue: IssueRecord) -> str | None:
        repo = self.tracker.repo
        try:
            created = await self.agent.create_session(
                build_fix_prompt(issue, repo, self.manifest),
                title=f"Fix #{issue.number}: {scrub_text(issue.title)}"[:80],
                tags=["factory", "dispatch"],
                structured_output_schema=OUTPUT_SCHEMA,
                repos=[repo],
            )
        except Exception as exc:
            detail = _error_detail(exc)
            logger.error("Could not start a fix session for #%d: %s", issue.number, detail)
            emit("factory.dispatch_failed", "factory", "error", issue=issue.number, error=detail)
            return f"failed to start session for #{issue.number}: {detail}"
        record = DispatchRecord(
            issue=issue.number, session_id=created.session_id, session_url=created.url, started_at=self.clock()
        )
        ledger.records.append(record)
        self._save_ledger(ledger)
        emit("factory.dispatched", "factory", issue=issue.number, session_id=created.session_id)
        await self._label(ledger, record)
        return f"started {created.url} for #{issue.number}: {issue.title}"

    async def _label(self, ledger: DispatchLedger, record: DispatchRecord) -> None:
        await self.tracker.add_labels(record.issue, [DISPATCHED, FIXING])
        await self.tracker.comment(
            record.issue,
            f"Started a Devin fix session: {record.session_url}\n\n"
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
            if record.done or not record.labeled:
                continue
            try:
                session = await self.agent.get_session(record.session_id)
            except Exception as exc:
                logger.warning("Could not refresh fix session %s: %s", record.session_id, exc)
                continue
            status = self.agent.map_status(session.status.lower(), (session.status_detail or "").lower())
            output = session.structured_output or {}
            if status is SessionStatus.blocked and _has_summary(output):
                status = SessionStatus.finished
            if status is None or status not in _DONE:
                continue
            output_pr_url = output.get("pr_url")
            pr_url: str | None = output_pr_url if isinstance(output_pr_url, str) and output_pr_url else None
            if pr_url is None and session.pull_requests:
                pr_url = session.pull_requests[-1].pr_url
            summary = output.get("summary")
            if self.dry_run:
                done.append(f"report outcome on #{record.issue}")
                continue
            lines = [f"Devin fix session ended ({status.value}): {record.session_url}"]
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
            self._save_ledger(ledger)
            emit("factory.dispatch_ended", "factory", issue=record.issue, status=status.value, has_pr=bool(pr_url))
            done.append(f"session for #{record.issue} ended ({status.value})")
        return done
