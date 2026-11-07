"""Portable v1 inbox contracts."""

import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, BaseModel, JsonValue, ConfigDict, AwareDatetime, field_validator


class ItemKind(StrEnum):
    """The kind of work submitted."""

    ask = "ask"
    incident = "incident"


class ItemState(StrEnum):
    """Inbox lifecycle states."""

    new = "new"
    needs_repo = "needs_repo"
    triage = "triage"
    needs_info = "needs_info"
    planning = "planning"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    running = "running"
    testing = "testing"
    done = "done"
    declined = "declined"
    cancelled = "cancelled"


class ClientKind(StrEnum):
    """The caller's trust classification."""

    owner = "owner"
    app = "app"
    agent = "agent"
    public = "public"


def normalize_repo(value: str) -> str:
    """Validate and normalize a GitHub repository reference."""
    value = re.sub(r"^https?://github\.com/", "", value.strip(), flags=re.IGNORECASE).rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository must be owner/name or a github.com URL")
    return value.lower()


class Source(BaseModel):
    """Optional caller-supplied provenance."""

    client: str | None = None
    app_id: str | None = None
    channel: str | None = None


class RepoRef(BaseModel):
    """A repository attached to an item."""

    full_name: str
    role: Literal["primary", "also"] = "primary"

    @field_validator("full_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Require an owner/name reference."""
        if "://" in value:
            raise ValueError("expected owner/name")
        return normalize_repo(value)


class IssueLink(BaseModel):
    """A filed issue."""

    number: int
    url: str


class RunLink(BaseModel):
    """Dispatch status, without claiming a successful fix."""

    state: str
    session_url: str | None = None
    pr_url: str | None = None


class SubmitWork(BaseModel):
    """An idempotent work submission."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    kind: ItemKind = ItemKind.ask
    text: str = Field(min_length=1, max_length=4000)
    repo: str | None = None
    created_at: AwareDatetime | None = None
    source: Source | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Reject whitespace-only text."""
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("repo")
    @classmethod
    def repository(cls, value: str | None) -> str | None:
        """Normalize optional repository input."""
        return normalize_repo(value) if value is not None else None


class InboxItem(BaseModel):
    """Garage's durable record of one submission."""

    id: str
    submit_id: str
    submitted_by: str
    trust: ClientKind
    kind: ItemKind
    text: str
    state: ItemState
    repos: list[RepoRef] = Field(default_factory=list)
    source: Source | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)
    issue: IssueLink | None = None
    run: RunLink | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ItemList(BaseModel):
    """One page of visible inbox items."""

    items: list[InboxItem]
    next_cursor: str | None = None


class ApiError(BaseModel):
    """An API error."""

    error: str


def json_schemas() -> dict[str, dict[str, object]]:
    """Return the public model schemas for non-Python clients."""
    return {
        model.__name__: model.model_json_schema()
        for model in (Source, RepoRef, IssueLink, RunLink, SubmitWork, InboxItem, ItemList, ApiError)
    }
