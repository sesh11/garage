"""Request filing models and bridge."""

import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from pydantic import BaseModel, AwareDatetime

from garage_sdk import emit
from garage.report import RequestLogger
from garage.tracker import IssueRecord
from garage_sdk.api import normalize_repo


class Request(BaseModel):
    """One request submitted to Garage."""

    id: str
    request: str
    created_at: AwareDatetime
    repository: str | None = None
    project: str | None = None


@dataclass
class BridgeProject:
    """One project's request filing: its repository, issue logger and dispatcher hooks."""

    name: str | None
    repo: str | None
    logger: RequestLogger | None
    on_filed: Callable[[], None] | None = None
    status: Callable[[int], dict[str, object]] | None = None


class RequestBridge:
    """Serialize request filing per project and expose optional request reporting.

    Pass ``projects`` to serve several projects; the positional ``logger`` form is a one-project bridge.
    """

    def __init__(
        self,
        logger: RequestLogger | None = None,
        *,
        bridge_token: str | None = None,
        on_filed: Callable[[], None] | None = None,
        status: Callable[[int], dict[str, object]] | None = None,
        projects: Sequence[BridgeProject] | None = None,
    ) -> None:
        """Configure the request loggers and bridge authentication token."""
        self.projects = (
            list(projects) if projects is not None else [BridgeProject(None, None, logger, on_filed, status)]
        )
        self.bridge_token = bridge_token
        self._lock = asyncio.Lock()

    @property
    def logger(self) -> RequestLogger | None:
        """The first project's request logger."""
        return self.projects[0].logger if self.projects else None

    @property
    def on_filed(self) -> Callable[[], None] | None:
        """The first project's dispatcher wake-up hook."""
        return self.projects[0].on_filed if self.projects else None

    @property
    def status(self) -> Callable[[int], dict[str, object]] | None:
        """The first project's dispatch status lookup."""
        return self.projects[0].status if self.projects else None

    @property
    def enabled(self) -> bool:
        """Whether request filing is configured for any project."""
        return any(project.logger is not None for project in self.projects)

    def project(self, name: str | None = None, repository: str | None = None) -> BridgeProject | None:
        """Find a project by name, else by repository; with neither, a one-project bridge's only project."""
        if name is not None:
            return next((project for project in self.projects if project.name == name), None)
        if repository:
            try:
                wanted = normalize_repo(repository)
            except ValueError:
                wanted = repository.strip().lower()
            for project in self.projects:
                if project.repo is not None:
                    if wanted in {project.repo.lower(), project.repo.lower().split("/")[-1]}:
                        return project
                elif project.logger is not None and project.logger.wants(repository):
                    return project
            return None
        return self.projects[0] if len(self.projects) == 1 else None

    async def file(self, request: Request) -> IssueRecord | None:
        """File a request for its project while preventing duplicate concurrent submissions."""
        target = self.project(request.project, request.repository)
        logger = target.logger if target is not None else None
        if target is not None and logger is None:
            return None
        if logger is not None and request.repository is None and request.project is not None:
            request = request.model_copy(update={"repository": logger.issues.repo})
        if target is None or logger is None or not logger.wants(request.repository):
            if request.project or request.repository:
                emit(
                    "factory.request_unrouted",
                    "factory",
                    "warning",
                    request_id=request.id,
                    project=request.project,
                    repository=request.repository,
                )
            return None
        async with self._lock:
            issue = await logger.file(request)
        if issue is not None and target.on_filed is not None:
            target.on_filed()
        return issue

    def status_of(self, number: int, project: str | None = None) -> dict[str, object]:
        """Return the dispatch status for one filed request."""
        target = self.project(project) if project is not None else (self.projects[0] if self.projects else None)
        status = target.status if target is not None else None
        details = status(number) if status is not None else {"state": "queued", "session_url": None, "pr_url": None}
        return {"number": number, "dispatch": status is not None, **details}

    async def mark_fixing(self, number: int, project: str | None = None) -> None:
        """Move a request issue from ready to fixing."""
        target = self.project(project) if project is not None else (self.projects[0] if self.projects else None)
        if target is not None and target.logger is not None:
            await target.logger.mark_fixing(number)
