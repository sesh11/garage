"""Shared offline service fakes."""

from pathlib import Path
from datetime import datetime, timezone

import pytest

from garage.tracker import IssueRecord
from garage.agents.base import SessionState, SessionStatus, CreatedSession
from garage.agents.devin import DevinClient
from garage.agents.local import LocalAgent


class FakeTracker:
    def __init__(self, repo: str = "sesh11/ambient-home") -> None:
        self.repo = repo
        self.issues: list[IssueRecord] = []
        self.comments: list[tuple[int, str]] = []
        self.creates: list[tuple[str, str, list[str]]] = []
        self.mutations: list[str] = []
        self.calls: list[str] = []
        self.labels: dict[str, tuple[str, str]] = {}
        self.fail_create = False
        self.actors: dict[tuple[int, str], str] = {}

    async def last_actor(self, number: int, event: str, label: str | None = None) -> str | None:
        return self.actors.get((number, event))

    async def changed_since(
        self, since: datetime, etag: str | None = None
    ) -> tuple[list[IssueRecord], str | None, bool]:
        self.calls.append("changed_since")
        found = [issue for issue in self.issues if (issue.updated_at or issue.created_at) >= since]
        return sorted(found, key=lambda issue: issue.updated_at or issue.created_at), None, False

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        self.calls.append("ensure_labels")
        self.labels.update(labels)

    async def list_issues(self, label: str) -> list[IssueRecord]:
        self.calls.append(f"list_issues:{label}")
        return [issue for issue in self.issues if label in issue.labels]

    async def get_issue(self, number: int) -> IssueRecord | None:
        self.calls.append(f"get_issue:{number}")
        return next((issue for issue in self.issues if issue.number == number), None)

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        self.calls.append("create_issue")
        if self.fail_create:
            raise RuntimeError("tracker unavailable")
        self.creates.append((title, body, labels))
        issue = IssueRecord(
            number=max((item.number for item in self.issues), default=0) + 1,
            url=f"https://github.com/{self.repo}/issues/{max((item.number for item in self.issues), default=0) + 1}",
            state="open",
            title=title,
            body=body,
            labels=list(labels),
            created_at=datetime.now(timezone.utc),
        )
        self.issues.append(issue)
        self.mutations.append("create")
        return issue

    async def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> IssueRecord:
        self.calls.append("update_issue")
        issue = next(item for item in self.issues if item.number == number)
        update: dict[str, object] = {}
        if title is not None:
            update["title"] = title
        if body is not None:
            update["body"] = body
        if state is not None:
            update["state"] = state
        if state_reason is not None:
            update["state_reason"] = state_reason
        update["updated_at"] = datetime.now(timezone.utc)
        updated = issue.model_copy(update=update)
        self.issues[self.issues.index(issue)] = updated
        self.mutations.append("update")
        return updated

    async def add_labels(self, number: int, labels: list[str]) -> None:
        self.calls.append("add_labels")
        issue = next(item for item in self.issues if item.number == number)
        issue.labels = list(dict.fromkeys([*issue.labels, *labels]))
        issue.updated_at = datetime.now(timezone.utc)
        self.mutations.append("add_labels")

    async def remove_label(self, number: int, label: str) -> None:
        self.calls.append("remove_label")
        issue = next(item for item in self.issues if item.number == number)
        issue.labels = [item for item in issue.labels if item != label]
        issue.updated_at = datetime.now(timezone.utc)
        self.mutations.append("remove_label")

    async def comment(self, number: int, body: str) -> None:
        self.calls.append("comment")
        self.comments.append((number, body))
        self.mutations.append("comment")


class FakeAgent:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, object]]] = []
        self.states: dict[str, SessionState] = {}
        self.calls: list[str] = []

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
        self.calls.append("create_session")
        self.created.append(
            (
                prompt,
                {
                    "title": title,
                    "tags": tags,
                    "structured_output_schema": structured_output_schema,
                    "repos": repos,
                    "platform": platform,
                },
            )
        )
        session_id = f"s{len(self.created)}"
        self.states[session_id] = SessionState(session_id=session_id, status="running")
        return CreatedSession(
            session_id=session_id,
            url=f"https://app.devin.ai/sessions/{session_id}",
            status="new",
        )

    async def get_session(self, session_id: str) -> SessionState:
        self.calls.append("get_session")
        return self.states[session_id]

    @staticmethod
    def map_status(status: str, status_detail: str) -> SessionStatus | None:
        return DevinClient.map_status(status, status_detail)


class FakeLocalAgent(FakeAgent):
    """Shaped like the local Claude Code / Codex agent: file:// run URLs, Garage's own status names, and the agent
    (not the model) opening the pull request, so the PR shows up only on the finished session."""

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
        created = await super().create_session(
            prompt,
            title=title,
            tags=tags,
            structured_output_schema=structured_output_schema,
            repos=repos,
            platform=platform,
        )
        self.states[created.session_id] = SessionState(session_id=created.session_id, status="working")
        return CreatedSession(
            session_id=created.session_id, url=f"file:///runs/{created.session_id}", status="working"
        )

    def finish(self, session_id: str, pr_url: str | None, summary: str = "Done.") -> None:
        self.states[session_id] = SessionState.model_validate(
            {
                "session_id": session_id,
                "status": "finished",
                "structured_output": {"summary": summary, "pr_url": pr_url, "test_result": "passed", "risk": "low"},
                "pull_requests": [{"pr_url": pr_url}] if pr_url else [],
            }
        )

    @staticmethod
    def map_status(status: str, status_detail: str) -> SessionStatus | None:
        return LocalAgent.map_status(status, status_detail)


def example_manifest() -> Path:
    return Path(__file__).parents[1] / "examples" / "ambient-home" / "garage.toml"


@pytest.fixture(autouse=True)
def _no_projects_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARAGE_PROJECTS", raising=False)


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GARAGE_DATA_DIR", raising=False)
