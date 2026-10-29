"""Issue tracker models and protocol."""

import re
from typing import Protocol
from datetime import datetime

from pydantic import BaseModel


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
    ) -> IssueRecord:
        """Update an issue."""
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
