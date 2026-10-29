# mypy: disable-error-code="no-untyped-def"

from datetime import datetime, timezone, timedelta

import pytest
from conftest import FakeAgent, FakeTracker, example_manifest

from garage.cli import detect_over
from garage.report import READY, IncidentReporter
from garage.tracker import IssueRecord
from garage.dispatch import Dispatcher
from garage.settings import settings_from_env
from garage_sdk.events import EventLog
from garage.incidents.registry import build_detector


@pytest.mark.asyncio
async def test_offline_report_and_dispatch_dry_run(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    now = datetime.now(timezone.utc)
    source = EventLog(tmp_path / "events", clock=lambda: now)
    for _ in range(3):
        source.emit("mic.check", "app", verdict="silent")
    detector = build_detector(settings.detector, settings.rules_path)
    start = now - timedelta(hours=24)
    incidents = await detect_over(detector, source.read(start, now), start, now)

    tracker = FakeTracker()
    reporter = IncidentReporter(tracker, dry_run=True)
    report_actions = await reporter.sync(incidents)
    assert report_actions and report_actions[0].startswith("open [factory:incident")
    assert not tracker.mutations

    tracker.issues.append(
        IssueRecord(
            number=42,
            url="https://github.com/sesh11/ambient-home/issues/42",
            state="open",
            title="Fix an incident",
            body="A rule-tripping issue.",
            labels=[READY],
            created_at=now,
        )
    )
    agent = FakeAgent()
    dispatcher = Dispatcher(
        tracker,
        agent,
        tmp_path / "dispatch.json",
        manifest=settings.manifest,
        interval_s=60,
        dry_run=True,
    )
    dispatch_actions = await dispatcher.poll_once()
    assert dispatch_actions == ["start session for #42: Fix an incident"]
    assert not agent.created
    assert not tracker.mutations
