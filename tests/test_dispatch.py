# mypy: disable-error-code="no-untyped-def"

import json
from datetime import datetime, timezone

import pytest
from conftest import FakeAgent, FakeTracker, example_manifest

from garage.report import READY, FIXING, REQUEST, DISPATCHED
from garage.tracker import IssueRecord
from garage.dispatch import Dispatcher, DispatchLedger, DispatchRecord, build_fix_prompt
from garage.settings import settings_from_env
from garage.agents.base import SessionState


def issue_record(number: int = 1, labels: list[str] | None = None) -> IssueRecord:
    return IssueRecord(
        number=number,
        url=f"https://github.com/sesh11/ambient-home/issues/{number}",
        state="open",
        title="Fix a bug",
        body="The service is failing.",
        labels=labels or [READY],
        created_at=datetime.now(timezone.utc),
    )


def test_prompt_uses_manifest_check_test_kit_and_rules() -> None:
    settings = settings_from_env(example_manifest())
    prompt = build_fix_prompt(issue_record(), "sesh11/ambient-home", settings.manifest)
    assert "uv run ruff check ." in prompt
    assert "ambient-hw-test --markdown /tmp/ambient-hw-test-1.md" in prompt
    assert "my-mac" in prompt
    assert "https://github.com/sesh11/ambient-home.git" in prompt
    assert "{issue}" not in prompt and "{outpost}" not in prompt and "{repo}" not in prompt


@pytest.mark.asyncio
async def test_dispatch_starts_once_and_records_ledger(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    tracker = FakeTracker()
    tracker.issues.append(issue_record())
    agent = FakeAgent()
    path = tmp_path / "dispatch.json"
    dispatcher = Dispatcher(tracker, agent, path, manifest=settings.manifest, interval_s=60, dry_run=False)
    actions = await dispatcher.poll_once()
    assert len(agent.created) == 1
    assert actions and "started https://app.devin.ai/sessions/s1" in actions[0]
    assert DISPATCHED in tracker.issues[0].labels
    assert FIXING in tracker.issues[0].labels
    assert DispatchLedger.load(path).records[0].labeled
    assert "factory-session: s1" in tracker.comments[0][1]


@pytest.mark.asyncio
async def test_dispatch_dry_run_has_no_mutations_or_sessions(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    tracker = FakeTracker()
    tracker.issues.append(issue_record())
    agent = FakeAgent()
    dispatcher = Dispatcher(
        tracker, agent, tmp_path / "dispatch.json", manifest=settings.manifest, interval_s=60, dry_run=True
    )
    assert (await dispatcher.poll_once())[0].startswith("start session for #1")
    assert not tracker.mutations
    assert not agent.created


@pytest.mark.asyncio
async def test_dispatch_skips_requests_and_migrates_legacy_ledger(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    tracker = FakeTracker()
    tracker.issues.append(issue_record(labels=[READY, REQUEST]))
    agent = FakeAgent()
    legacy_path = tmp_path / "legacy.json"
    original = {
        "records": [
            {
                "issue": 8,
                "session_id": "legacy-session",
                "session_url": "https://app.devin.ai/sessions/legacy-session",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "labeled": True,
                "done": False,
            }
        ]
    }
    legacy_path.write_text(json.dumps(original), encoding="utf-8")
    legacy_before = legacy_path.read_bytes()
    dispatcher = Dispatcher(
        tracker,
        agent,
        tmp_path / "new.json",
        manifest=settings.manifest,
        interval_s=60,
        dry_run=False,
        legacy_ledger_path=legacy_path,
    )
    ledger = dispatcher._load_ledger()
    assert ledger.records[0].session_id == "legacy-session"
    assert legacy_path.read_bytes() == legacy_before
    assert await dispatcher.poll_once() == []
    assert not agent.created
    tracker.issues[0].labels = [READY]
    await dispatcher.poll_once()
    assert (tmp_path / "new.json").exists()
    assert legacy_path.read_bytes() == legacy_before


@pytest.mark.asyncio
async def test_blocked_session_with_summary_is_finished_and_uses_test_result(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    tracker = FakeTracker()
    tracker.issues.append(issue_record(labels=[READY, DISPATCHED, FIXING]))
    agent = FakeAgent()
    agent.states["s1"] = SessionState(
        session_id="s1",
        status="running",
        status_detail="waiting_for_user",
        structured_output={"summary": "Fixed the failure.", "test_result": "passed", "pr_url": None, "risk": "low"},
    )
    ledger = DispatchLedger(
        records=[
            DispatchRecord(
                issue=1,
                session_id="s1",
                session_url="https://app.devin.ai/sessions/s1",
                started_at=datetime.now(timezone.utc),
                labeled=True,
            )
        ]
    )
    path = tmp_path / "dispatch.json"
    ledger.save(path)
    dispatcher = Dispatcher(tracker, agent, path, manifest=settings.manifest, interval_s=60, dry_run=False)
    await dispatcher.poll_once()
    comment = tracker.comments[0][1]
    assert "ended (finished)" in comment
    assert "Test result: passed" in comment


@pytest.mark.asyncio
async def test_follow_up_reads_legacy_robot_test_output(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    tracker = FakeTracker()
    tracker.issues.append(issue_record(labels=[READY, DISPATCHED, FIXING]))
    agent = FakeAgent()
    agent.states["old"] = SessionState(
        session_id="old",
        status="exit",
        structured_output={"summary": "Completed.", "robot_test": "skipped"},
    )
    path = tmp_path / "dispatch.json"
    DispatchLedger(
        records=[
            DispatchRecord(
                issue=1,
                session_id="old",
                session_url="https://app.devin.ai/sessions/old",
                started_at=datetime.now(timezone.utc),
                labeled=True,
            )
        ]
    ).save(path)
    dispatcher = Dispatcher(tracker, agent, path, manifest=settings.manifest, interval_s=60, dry_run=False)
    await dispatcher.poll_once()
    assert "Test result: skipped" in tracker.comments[0][1]
