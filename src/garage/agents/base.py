"""Coding-agent session models and protocol."""

from enum import StrEnum
from typing import Protocol

from pydantic import Field, BaseModel, ConfigDict


class SessionStatus(StrEnum):
    """Normalized coding-agent session status."""

    working = "working"
    blocked = "blocked"
    finished = "finished"
    failed = "failed"
    expired = "expired"


class CreatedSession(BaseModel):
    """What the Devin API returns when a session is created."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    url: str
    status: str


class _PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pr_url: str
    pr_state: str | None = None


class SessionState(BaseModel):
    """A coding-agent session's current status and structured output."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    status: str
    status_detail: str | None = None
    pull_requests: list[_PullRequest] = Field(default_factory=list)
    structured_output: dict[str, object] | None = None
    acus_consumed: float | None = None


class CodingAgent(Protocol):
    """Session operations required by the dispatcher."""

    async def create_session(
        self,
        prompt: str,
        *,
        title: str,
        tags: list[str],
        structured_output_schema: dict[str, object],
        repos: list[str] | None = None,
        platform: str | None = None,
    ) -> CreatedSession:
        """Create a coding-agent session."""
        ...

    async def get_session(self, session_id: str) -> SessionState:
        """Fetch a session's latest state."""
        ...

    @staticmethod
    def map_status(status: str, status_detail: str) -> SessionStatus | None:
        """Normalize an agent-specific session status."""
        ...
