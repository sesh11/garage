from datetime import datetime, timezone

import pytest
from conftest import FakeTracker

from garage.report import READY, REQUEST, INCIDENT, OBSERVED, RequestLogger, IncidentReporter
from garage.requests import Request
from garage_sdk.events import Event
from garage.incidents.base import Incident


def sample_incident() -> Incident:
    now = datetime.now(timezone.utc)
    event = Event(ts=now, type="runtime.error", component="app", level="error", data={"message": "failed"})
    return Incident(
        fingerprint="error:abc123",
        rule="error",
        title="Runtime error",
        severity="high",
        count=2,
        threshold=2,
        window_s=3600,
        first_seen=now,
        last_seen=now,
        ready=True,
        group={},
        samples=[event],
        detector="rules",
    )


@pytest.mark.asyncio
async def test_incident_report_preserves_labels_and_fingerprint() -> None:
    tracker = FakeTracker()
    reporter = IncidentReporter(tracker)
    actions = await reporter.sync([sample_incident()])
    issue = tracker.issues[0]
    assert actions[0].startswith("open #1")
    assert issue.labels == [INCIDENT, READY]
    assert "<!-- factory-fingerprint: error:abc123 -->" in issue.body
    assert "Filed automatically by garage (Report)" in issue.body


@pytest.mark.asyncio
async def test_report_dry_run_prints_would_file_without_mutation() -> None:
    tracker = FakeTracker()
    reporter = IncidentReporter(tracker, dry_run=True)
    actions = await reporter.sync([sample_incident()])
    assert actions[0].startswith("open [factory:incident, factory:ready-for-fix]")
    assert not tracker.mutations


@pytest.mark.asyncio
async def test_daily_cap_prevents_new_incident_issues() -> None:
    tracker = FakeTracker()
    reporter = IncidentReporter(tracker, max_new_per_day=0)
    actions = await reporter.sync([sample_incident()])
    assert actions[0].startswith("skip (daily cap 0)")
    assert not tracker.creates


@pytest.mark.asyncio
async def test_request_body_title_target_and_duplicate_id() -> None:
    tracker = FakeTracker()
    logger = RequestLogger(tracker)
    request = Request(
        id="from-bridge:1",
        request="Please add a safe feature",
        created_at=datetime.now(timezone.utc),
        repository="https://github.com/sesh11/ambient-home.git",
    )
    issue = await logger.file(request)
    assert issue is not None
    assert issue.title.startswith("Request: ")
    assert issue.labels == [REQUEST, READY]
    assert OBSERVED not in issue.labels
    assert f"<!-- factory-request: {request.id} -->" in issue.body
    assert "Filed automatically by garage." in issue.body
    assert await logger.file(request) == issue
    assert len(tracker.creates) == 1


@pytest.mark.asyncio
async def test_request_non_target_is_skipped() -> None:
    tracker = FakeTracker()
    logger = RequestLogger(tracker)
    request = Request(
        id="id",
        request="task",
        created_at=datetime.now(timezone.utc),
        repository="https://github.com/another/ambient-home",
    )
    assert await logger.file(request) is None
    assert not tracker.creates
