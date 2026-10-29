"""Monitor scheduling and event-sink isolation tests."""

# mypy: disable-error-code="no-untyped-def"

import asyncio
from datetime import datetime, timezone, timedelta

from garage_sdk.events import EventLog
from garage.incidents.base import Incident
from garage.incidents.monitor import IncidentMonitor


class TinyDetector:
    name = "tiny"

    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def detect(self, events, now):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("temporary detector error")
        if not any(event.type == "trigger" for event in events):
            return []
        return [
            Incident(
                fingerprint="tiny:incident",
                rule="tiny_rule",
                title="Tiny incident",
                severity="high",
                count=1,
                threshold=1,
                window_s=60,
                first_seen=events[0].ts,
                last_seen=events[0].ts,
                ready=True,
                group={},
                samples=[events[0]],
                detector=self.name,
            )
        ]


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_monitor_emits_observed_and_ready_to_separate_sink(tmp_path) -> None:
    source = EventLog(tmp_path / "source")
    sink = EventLog(tmp_path / "sink")
    monitor = IncidentMonitor(TinyDetector(), source, sink=sink, lookback=timedelta(hours=1), debounce_s=0)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.01)
    source.emit("trigger", "test")
    await asyncio.sleep(0.03)
    source.emit("trigger", "test")
    await asyncio.sleep(0.03)
    await _stop(task)
    events = sink.read(datetime.now(timezone.utc) - timedelta(minutes=1))
    assert [event.type for event in events].count("incident.observed") == 1
    assert [event.type for event in events].count("incident.ready") == 1
    assert all(
        event.component != "factory" for event in source.read(datetime.now(timezone.utc) - timedelta(minutes=1))
    )


async def test_factory_and_heartbeat_events_do_not_trigger_evaluation(tmp_path) -> None:
    source = EventLog(tmp_path / "events")
    detector = TinyDetector()
    monitor = IncidentMonitor(detector, source, lookback=timedelta(hours=1), debounce_s=0)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.02)
    initial_calls = detector.calls
    source.emit("incident.observed", "factory")
    source.emit("app.heartbeat", "app")
    await asyncio.sleep(0.02)
    await _stop(task)
    assert detector.calls == initial_calls


async def test_poke_is_thread_safe_and_detector_failure_recovers(tmp_path) -> None:
    source = EventLog(tmp_path / "events")
    detector = TinyDetector(fail_first=True)
    monitor = IncidentMonitor(detector, source, lookback=timedelta(hours=1), debounce_s=0)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.02)
    source.emit("trigger", "test")
    await asyncio.sleep(0.02)
    await asyncio.to_thread(monitor.poke)
    await asyncio.sleep(0.04)
    await _stop(task)
    assert detector.calls >= 2
    assert monitor.latest
