"""Request filing models and bridge."""

import asyncio

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
    ) -> None:
        """Configure the optional request logger and bridge authentication token."""
        self.logger = logger
        self.bridge_token = bridge_token
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
            return await self.logger.file(request)

    async def mark_fixing(self, number: int) -> None:
        """Move a request issue from ready to fixing."""
        if self.logger is not None:
            await self.logger.mark_fixing(number)
