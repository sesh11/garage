"""Projects: the named units of work one Garage serves.

A project today is one GitHub repository plus how to work on it: a local checkout, the coding agent, the base
branch, check commands and prompt rules. Each project resolves to a manifest (``garage.toml``), so detection,
reporting and dispatch reuse the single-repository code unchanged. A plain manifest is a one-project setup.
"""

import os
import tomllib
from pathlib import Path
from dataclasses import replace

from pydantic import Field, BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from garage_sdk.api import RepoRef, ProjectInfo, normalize_repo, normalize_project
from garage.settings import GarageSettings, settings_from_env, settings_for_manifest
from garage_sdk.manifest import (
    Manifest,
    AppSection,
    BuildSection,
    DetectSection,
    PolicySection,
    ReportSection,
    SignalsSection,
    DispatchSection,
    load_manifest,
)


AGENTS = ("devin", "claude-code", "codex")


class ProjectEntry(BaseModel):
    """One ``[[project]]`` table in a projects file; unset fields come from the project's manifest."""

    model_config = ConfigDict(extra="forbid")

    name: str
    repo: str | None = None
    path: str | None = None
    manifest: str | None = None
    agent: str | None = None
    base_branch: str | None = None
    check: list[str] | None = None
    prompt_rules: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return normalize_project(value)

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str | None) -> str | None:
        return normalize_repo(value) if value is not None else None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        if value is not None and value not in AGENTS:
            raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
        return value

    @field_validator("base_branch")
    @classmethod
    def _branch(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("base_branch must not be empty")
        return value


class ProjectsFile(BaseModel):
    """A projects file: a list of ``[[project]]`` tables."""

    model_config = ConfigDict(extra="forbid")

    project: list[ProjectEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "ProjectsFile":
        names = [entry.name for entry in self.project]
        if len(set(names)) != len(names):
            raise ValueError("project names must be unique")
        return self


def _path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _manifest_for(entry: ProjectEntry, base: Path) -> tuple[Manifest, Path]:
    checkout = _path(entry.path, base) if entry.path else None
    manifest_path = _path(entry.manifest, base) if entry.manifest else None
    if manifest_path is None and checkout is not None and (checkout / "garage.toml").is_file():
        manifest_path = checkout / "garage.toml"
    if manifest_path is not None:
        manifest = load_manifest(manifest_path)
        manifest_dir = manifest_path.parent
    else:
        if entry.repo is None:
            raise ValueError(f"project {entry.name!r} needs repo, or a manifest that sets [app].repo")
        manifest = Manifest(
            app=AppSection(id=entry.name, repo=entry.repo),
            build=BuildSection(),
            signals=SignalsSection(),
            detect=DetectSection(),
            report=ReportSection(),
            dispatch=DispatchSection(),
            policy=PolicySection(),
        )
        manifest_dir = checkout or base
    dispatch = manifest.dispatch.model_copy(
        update={
            key: value
            for key, value in (
                ("agent", entry.agent),
                ("base_branch", entry.base_branch),
                ("prompt_rules", entry.prompt_rules),
            )
            if value is not None
        }
    )
    build = manifest.build.model_copy(update={"check": entry.check} if entry.check is not None else {})
    app = AppSection(id=entry.name, repo=entry.repo or manifest.app.repo)
    return manifest.model_copy(update={"app": app, "dispatch": dispatch, "build": build}), manifest_dir


def load_projects(path: Path) -> list[GarageSettings]:
    """Load a projects file, or a single manifest as a one-project list."""
    path = path.expanduser().resolve()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid projects file at {path}: {exc}") from exc
    if "project" not in raw:
        return [settings_from_env(path)]
    try:
        entries = ProjectsFile.model_validate(raw).project
    except ValidationError as exc:
        raise ValueError(f"invalid projects file at {path}: {exc}") from exc
    projects: list[GarageSettings] = []
    for entry in entries:
        manifest, manifest_dir = _manifest_for(entry, path.parent)
        settings = settings_for_manifest(manifest, manifest_dir, project_overrides=False)
        projects.append(
            replace(
                settings,
                project=entry.name,
                local_repo_path=_path(entry.path, path.parent) if entry.path else None,
                ledger_path=settings.data_dir / "projects" / entry.name / "dispatch.json",
            )
        )
    repos = [project.repo for project in projects]
    if len(set(repos)) != len(repos):
        raise ValueError(f"invalid projects file at {path}: two projects use the same repo")
    return projects


def select_projects(manifest: Path | None = None, projects: Path | None = None) -> list[GarageSettings] | None:
    """Return projects-file settings, or None to use the single manifest the old way.

    Order: ``--projects``, ``--manifest``, ``GARAGE_PROJECTS``, ``GARAGE_MANIFEST``, then ``./garage.toml``.
    """
    if projects is not None:
        return load_projects(projects)
    if manifest is not None:
        return None
    selected = os.getenv("GARAGE_PROJECTS") or None
    if selected is not None:
        return load_projects(Path(selected))
    return None


def only(projects: list[GarageSettings], name: str | None) -> list[GarageSettings]:
    """Keep the named project, or every project when no name is given."""
    if name is None:
        return projects
    wanted = normalize_project(name)
    chosen = [project for project in projects if project.project == wanted]
    if not chosen:
        known = ", ".join(project.project for project in projects)
        raise ValueError(f"unknown project {name!r}; known projects: {known}")
    return chosen


def project_info(settings: GarageSettings) -> ProjectInfo:
    """Describe a project for API clients."""
    return ProjectInfo(
        name=settings.project,
        repos=[RepoRef(full_name=settings.repo)],
        agent=settings.manifest.dispatch.agent,
        base_branch=settings.manifest.dispatch.base_branch,
    )
