"""Issue tracker models and protocol."""

import re
from typing import Protocol
from datetime import datetime

from pydantic import BaseModel


LABEL_PREFIX = "garage:"
LEGACY_LABEL_PREFIX = "factory:"


def canonical_label(name: str) -> str:
    """Read a label under its current name: ``factory:X`` (the old prefix) is the same label as ``garage:X``."""
    return LABEL_PREFIX + name.removeprefix(LEGACY_LABEL_PREFIX) if name.startswith(LEGACY_LABEL_PREFIX) else name


def legacy_label(name: str) -> str | None:
    """Return the old ``factory:`` name of a ``garage:`` label, or None for any other label."""
    return LEGACY_LABEL_PREFIX + name.removeprefix(LABEL_PREFIX) if name.startswith(LABEL_PREFIX) else None


_MARKER = re.compile(r"<!--\s*factory-(?P<key>[a-z-]+):\s*(?P<value>[^\s>]+)\s*-->")


class IssueRecord(BaseModel):
    """The parts of an issue Garage tracks."""

    number: int
    url: str
    state: str
    title: str
    body: str
    labels: list[str]
    created_at: datetime
    closed_at: datetime | None = None
    updated_at: datetime | None = None
    state_reason: str | None = None
    author: str | None = None
    pull_request: bool = False

    def marker(self, key: str) -> str | None:
        """Return the value of a hidden ``<!-- factory-key: value -->`` marker in the body."""
        for match in _MARKER.finditer(self.body):
            if match.group("key") == key:
                return match.group("value")
        return None


class Tracker(Protocol):
    """Issue operations used by Garage reporting and dispatch."""

    repo: str

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """Ensure all named labels exist."""
        ...

    async def list_issues(self, label: str) -> list[IssueRecord]:
        """Return issues carrying a label."""
        ...

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        """Create an issue."""
        ...

    async def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> IssueRecord:
        """Update an issue; ``state_reason`` (``completed`` or ``not_planned``) applies when closing."""
        ...

    async def add_labels(self, number: int, labels: list[str]) -> None:
        """Add labels to an issue."""
        ...

    async def remove_label(self, number: int, label: str) -> None:
        """Remove one label from an issue."""
        ...

    async def comment(self, number: int, body: str) -> None:
        """Post an issue comment."""
        ...


class IssueFeed(Tracker, Protocol):
    """A tracker that can also list every issue changed since a moment, for watching new issues."""

    async def changed_since(
        self, since: datetime, etag: str | None = None
    ) -> tuple[list[IssueRecord], str | None, bool]:
        """Return issues and pull requests updated at or after ``since``, the new ETag, and not-modified."""
        ...

    async def last_actor(self, number: int, event: str, label: str | None = None) -> str | None:
        """Return the GitHub login that last did ``event`` (``labeled``, ``closed``, ``reopened``) on an issue."""
        ...
