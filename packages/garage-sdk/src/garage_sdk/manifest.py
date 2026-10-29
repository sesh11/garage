"""Application manifest schemas and loading."""

import tomllib
from typing import Literal
from pathlib import Path

from pydantic import Field, BaseModel, ConfigDict, ValidationError, model_validator


class AppSection(BaseModel):
    """Application identity and source repository."""

    id: str
    repo: str


class BuildSection(BaseModel):
    """Setup and verification commands for the application."""

    setup: str | None = None
    check: list[str] = Field(default_factory=list)


class SignalsSection(BaseModel):
    """Event signal source and retention settings."""

    source: Literal["jsonl"] = "jsonl"
    path: str | None = None
    retention_days: int = 14


class DetectSection(BaseModel):
    """Incident detector configuration."""

    detector: str = "rules"
    rules: str | None = None


class TestKit(BaseModel):
    """A named application test command and its execution constraints."""

    name: str
    command: str
    runs_on: str = "cloud"
    when: str | list[str] = "always"
    max_minutes: int | None = None
    costs_money: bool = False
    instructions: str | None = None


class ReportSection(BaseModel):
    """Issue report attribution and daily creation limit."""

    attribution: str | None = None
    request_attribution: str | None = None
    max_new_issues_per_day: int = 5


class DispatchSection(BaseModel):
    """Dispatch agent, cadence, and outpost configuration."""

    agent: str = "devin"
    interval: str = "1h"
    outpost: str | None = None
    legacy_ledger: str | None = None
    prompt_rules: list[str] = Field(default_factory=list)


class PolicySection(BaseModel):
    """Human approvals and automation policy; auto_accept is not acted on in v0."""

    needs_approval: list[str] = Field(default_factory=lambda: ["triage", "plan", "release"])
    auto_release: bool = False
    auto_accept: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_human_plan_and_release(self) -> "PolicySection":
        """Keep plan and release decisions under human approval in v0."""
        if self.auto_release or not {"plan", "release"}.issubset(self.needs_approval):
            raise ValueError("v0 requires human approval for plan and release")
        return self


class Manifest(BaseModel):
    """Top-level app manifest; unknown sections are ignored for forward compatibility."""

    model_config = ConfigDict(extra="ignore")

    app: AppSection
    product_brief: str | None = None
    build: BuildSection
    signals: SignalsSection
    detect: DetectSection
    test_kit: list[TestKit] = Field(default_factory=list)
    report: ReportSection
    dispatch: DispatchSection
    policy: PolicySection


def load_manifest(path: Path) -> Manifest:
    """Load a TOML manifest, reporting parsing and validation errors with its path."""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        return Manifest.model_validate(raw)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid manifest at {path}: {exc}") from exc
