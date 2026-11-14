from datetime import datetime, timezone, timedelta

import pytest
from conftest import FakeTracker

from garage.report import REQUEST, INCIDENT, OBSERVED, PROPOSED, RequestLogger, IncidentReporter
from garage.tracker import ATTEMPTS_END, ATTEMPTS_START, replace_attempt_block
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
    assert issue.labels == [INCIDENT, PROPOSED]
    assert "<!-- factory-fingerprint: error:abc123 -->" in issue.body
    assert "Filed automatically by garage (Report)" in issue.body


@pytest.mark.asyncio
async def test_incident_updates_preserve_attempt_context_without_redundant_writes() -> None:
    tracker = FakeTracker()
    reporter = IncidentReporter(tracker, min_update_interval=timedelta(0))
    incident = sample_incident()
    await reporter.sync([incident])
    block = f"{ATTEMPTS_START}\nPrevious attempt failed: lint\n{ATTEMPTS_END}"
    tracker.issues[0].body = replace_attempt_block(tracker.issues[0].body, block)
    before = tracker.mutations.copy()
    assert await reporter.sync([incident]) == []
    assert tracker.mutations == before
    changed = incident.model_copy(update={"count": 3})
    assert (await reporter.sync([changed]))[0].startswith("update #1")
    assert tracker.issues[0].body.startswith(block)
    assert tracker.issues[0].body.count(ATTEMPTS_START) == 1


@pytest.mark.asyncio
async def test_report_dry_run_prints_would_file_without_mutation() -> None:
    tracker = FakeTracker()
    reporter = IncidentReporter(tracker, dry_run=True)
    actions = await reporter.sync([sample_incident()])
    assert actions[0].startswith("open [garage:incident, garage:proposed]")
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
    assert issue.labels == [REQUEST, PROPOSED]
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
