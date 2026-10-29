"""Bridge external library errors into structured local telemetry."""

import logging
from collections.abc import Sequence

from .events import emit


LEGACY_IGNORED_PREFIXES = ("ambient_home",)


class EventLoggingHandler(logging.Handler):
    """Record ERROR+ messages from third-party loggers as telemetry."""

    def __init__(self, ignore_prefixes: Sequence[str] = LEGACY_IGNORED_PREFIXES) -> None:
        """Set the logger-name prefixes excluded from telemetry."""
        super().__init__()
        self.ignore_prefixes = tuple(ignore_prefixes)

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one external error record to the event log."""
        if (
            record.levelno < logging.ERROR
            or record.name.startswith(self.ignore_prefixes)
            or record.name.startswith("garage_sdk")
        ):
            return
        data: dict[str, object] = {"message": record.getMessage()}
        if record.exc_info and record.exc_info[0] is not None:
            data["exc_type"] = record.exc_info[0].__name__
        emit("log.error", record.name, "error", **data)
