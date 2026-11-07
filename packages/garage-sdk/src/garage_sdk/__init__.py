"""Structured local telemetry."""

from .scrub import DROPPED_KEYS, scrub_data, scrub_text
from .events import Event, EventLog, emit, configure, get_event_log
from .resource import Resource
from .logging_bridge import EventLoggingHandler


__version__ = "0.2.0"


__all__ = [
    "DROPPED_KEYS",
    "Event",
    "EventLog",
    "EventLoggingHandler",
    "Resource",
    "__version__",
    "configure",
    "emit",
    "get_event_log",
    "scrub_data",
    "scrub_text",
]
