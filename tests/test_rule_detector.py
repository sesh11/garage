"""Rule detector tests."""

# mypy: disable-error-code="no-untyped-def"

from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from garage_sdk.events import Event
from garage.incidents.base import normalize_message
from garage.incidents.rules import Rule, RuleDetector
from garage.incidents.registry import build_detector


NOW = datetime(2025, 1, 2, 12, tzinfo=timezone.utc)
RULES_PATH = Path(__file__).parents[1] / "examples" / "ambient-home" / "rules.toml"


def _event(event_type: str, minutes_ago: int = 0, **data: str | int | float | bool | None) -> Event:
    return Event(ts=NOW - timedelta(minutes=minutes_ago), type=event_type, component="test", data=data)


async def test_mic_rule_threshold_boundary_and_where_filter() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    silent = [_event("mic.check", verdict="silent") for _ in range(3)]
    ignored = _event("mic.check", verdict="ok")
    below = await detector.detect([*silent[:2], ignored], NOW)
    ready = await detector.detect([*silent, ignored], NOW)
    assert len(below) == 1 and below[0].count == 2 and not below[0].ready
    assert ready[0].count == 3 and ready[0].ready


async def test_window_excludes_old_events() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    events = [_event("mic.check", minutes_ago=60 * 7, verdict="silent"), _event("mic.check", verdict="silent")]
    assert [item.count for item in await detector.detect(events, NOW)] == [1]


async def test_grouping_normalizes_messages_and_fingerprints() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    events = [
        _event("log.error", message="timeout after 3s"),
        _event("log.error", message="timeout after 5s"),
    ]
    incidents = await detector.detect(events, NOW)
    again = await detector.detect(events, NOW)
    assert len(incidents) == 1
    assert incidents[0].group["message"] == "timeout after <number>s"
    assert incidents[0].fingerprint == again[0].fingerprint
    assert normalize_message("timeout after 3s") == normalize_message("timeout after 5s")


async def test_cleared_by_suppresses_recovered_and_pending_matches() -> None:
    rule = Rule(
        name="daemon_lost",
        title="Daemon lost",
        severity="critical",
        types=["daemon.lost"],
        threshold=1,
        window="24h",
        cleared_by="daemon.ready",
        clear_within="10m",
    )
    detector = RuleDetector([rule])
    recovered = await detector.detect(
        [_event("daemon.lost", minutes_ago=20), _event("daemon.ready", minutes_ago=15)], NOW
    )
    unrecovered = await detector.detect([_event("daemon.lost", minutes_ago=12)], NOW)
    pending = await detector.detect([_event("daemon.lost", minutes_ago=5)], NOW)
    assert recovered == []
    assert len(unrecovered) == 1 and unrecovered[0].ready
    assert pending == []


def test_rules_detector_requires_rules_path_and_unknown_text() -> None:
    with pytest.raises(ValueError, match=r"the rules detector needs \[detect\]\.rules in the manifest"):
        build_detector("rules", None)
    with pytest.raises(ValueError, match="unknown detector"):
        build_detector("not-real", None)
    assert build_detector("rules", RULES_PATH).name == "rules"


def test_invalid_rules_files(tmp_path) -> None:
    invalid_toml = tmp_path / "invalid.toml"
    invalid_toml.write_text("[[rule]]\nname = ")
    with pytest.raises(ValueError, match="invalid incident rules file"):
        RuleDetector.from_file(invalid_toml)
    missing_clear_window = tmp_path / "missing-clear.toml"
    missing_clear_window.write_text(
        '[[rule]]\nname="x"\ntitle="x"\nseverity="low"\ntypes=["x"]\nthreshold=1\nwindow="1h"\ncleared_by="x"\n'
    )
    with pytest.raises(ValueError, match="clear_within is required"):
        RuleDetector.from_file(missing_clear_window)
