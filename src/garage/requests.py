"""Request filing models and bridge."""

import asyncio
from collections.abc import Callable

from pydantic import BaseModel, AwareDatetime

from garage.report import RequestLogger
from garage.tracker import IssueRecord


class Request(BaseModel):
    """One request submitted to Garage."""

    id: str
    request: str
    created_at: AwareDatetime
    repository: str | None = None


class RequestBridge:
    """Serialize request filing and expose optional request reporting."""

    def __init__(
        self,
        logger: RequestLogger | None,
        *,
        bridge_token: str | None = None,
        on_filed: Callable[[], None] | None = None,
        status: Callable[[int], dict[str, object]] | None = None,
    ) -> None:
        """Configure the optional request logger and bridge authentication token."""
        self.logger = logger
        self.bridge_token = bridge_token
        self.on_filed = on_filed
        self.status = status
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Whether request filing is configured."""
        return self.logger is not None

    async def file(self, request: Request) -> IssueRecord | None:
        """File a matching request while preventing duplicate concurrent submissions."""
        if self.logger is None or not self.logger.wants(request.repository):
            return None
        async with self._lock:
            issue = await self.logger.file(request)
        if issue is not None and self.on_filed is not None:
            self.on_filed()
        return issue

    def status_of(self, number: int) -> dict[str, object]:
        """Return the dispatch status for one filed request."""
        details = (
            self.status(number)
            if self.status is not None
            else {
                "state": "queued",
                "session_url": None,
                "pr_url": None,
            }
        )
        return {"number": number, "dispatch": self.status is not None, **details}

    async def mark_fixing(self, number: int) -> None:
        """Move a request issue from ready to fixing."""
        if self.logger is not None:
            await self.logger.mark_fixing(number)
