"""Structured event log tests."""

from datetime import datetime, timezone, timedelta

import pytest

from garage_sdk import EventLog, emit, configure, get_event_log


@pytest.fixture(autouse=True)
def reset_event_log():
    configure(None)
    yield
    configure(None)


def test_emit_read_round_trip_and_day_rollover(tmp_path) -> None:
    clock_values = iter(
        [
            datetime(2025, 1, 1, 23, 59, tzinfo=timezone.utc),
            datetime(2025, 1, 2, 0, 1, tzinfo=timezone.utc),
        ]
    )
    log = EventLog(tmp_path / "events", clock=lambda: next(clock_values))

    first = log.emit("mic.check", "mic", verdict="ok")
    second = log.emit("daemon.ready", "daemon")

    assert first is not None
    assert second is not None
    assert first.ts.tzinfo == timezone.utc
    assert sorted(path.name for path in (tmp_path / "events").glob("*.jsonl")) == [
        "2025-01-01.jsonl",
        "2025-01-02.jsonl",
    ]
    assert log.read(
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        datetime(2025, 1, 2, 0, 2, tzinfo=timezone.utc),
    ) == [first, second]


def test_prune_removes_old_day_files_and_keeps_recent(tmp_path) -> None:
    directory = tmp_path / "events"
    directory.mkdir()
    old = directory / "2024-12-28.jsonl"
    recent = directory / "2024-12-30.jsonl"
    old.write_text("{}\n")
    recent.write_text("{}\n")
    log = EventLog(
        directory,
        retention_days=2,
        clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert log.prune() == 1
    assert not old.exists()
    assert recent.exists()


def test_read_skips_malformed_lines_and_filters_range(tmp_path) -> None:
    log = EventLog(tmp_path / "events", clock=lambda: datetime(2025, 2, 1, tzinfo=timezone.utc))
    event = log.emit("mic.check", "mic")
    assert event is not None
    path = tmp_path / "events" / "2025-02-01.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")

    assert log.read(event.ts - timedelta(seconds=1), event.ts) == [event]


def test_emit_never_raises_when_directory_is_not_writable(tmp_path) -> None:
    not_a_directory = tmp_path / "events"
    not_a_directory.write_text("occupied")
    log = EventLog(not_a_directory)

    assert log.emit("mic.check", "mic") is None


def test_module_emit_is_noop_when_unconfigured() -> None:
    emit("mic.check", "mic")
    assert get_event_log() is None


def test_listeners_are_called_and_listener_errors_are_swallowed(tmp_path) -> None:
    log = EventLog(tmp_path / "events")
    received = []
    log.add_listener(received.append)
    log.add_listener(lambda event: (_ for _ in ()).throw(RuntimeError("listener error")))

    event = log.emit("mic.check", "mic")

    assert received == [event]
    assert log.read(datetime.now(timezone.utc) - timedelta(minutes=1)) == [event]


def test_legacy_event_line_parses_without_resource(tmp_path) -> None:
    directory = tmp_path / "events"
    directory.mkdir()
    path = directory / "2025-01-01.jsonl"
    path.write_text(
        '{"ts":"2025-01-01T00:00:00Z","type":"legacy","component":"old","level":"info","data":{"ok":true}}\n',
        encoding="utf-8",
    )
    log = EventLog(directory)

    events = log.read(datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 1, tzinfo=timezone.utc))

    assert len(events) == 1
    assert events[0].resource is None


def test_event_without_resource_keeps_legacy_jsonl_bytes(tmp_path) -> None:
    log = EventLog(tmp_path / "events", clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))

    log.emit("event", "component")

    assert (tmp_path / "events" / "2025-01-01.jsonl").read_bytes() == (
        b'{"ts":"2025-01-01T00:00:00Z","type":"event","component":"component","level":"info","data":{}}\n'
    )


def test_event_log_stamps_resource_on_events(tmp_path) -> None:
    from garage_sdk import Resource

    resource = Resource(app="app", version="1.2.3+abcdef0", install="abcd1234")
    log = EventLog(
        tmp_path / "events",
        clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
        resource=resource,
    )

    event = log.emit("event", "component")

    assert event is not None
    assert event.resource == resource
    raw = (tmp_path / "events" / "2025-01-01.jsonl").read_text(encoding="utf-8")
    assert '"resource":{"app":"app","version":"1.2.3+abcdef0","install":"abcd1234"}' in raw
