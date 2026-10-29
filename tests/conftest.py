"""Shared offline service fakes."""

from pathlib import Path
from datetime import datetime, timezone

from garage.tracker import IssueRecord
from garage.agents.base import SessionState, SessionStatus, CreatedSession
from garage.agents.devin import DevinClient


class FakeTracker:
    def __init__(self, repo: str = "sesh11/ambient-home") -> None:
        self.repo = repo
        self.issues: list[IssueRecord] = []
        self.comments: list[tuple[int, str]] = []
        self.creates: list[tuple[str, str, list[str]]] = []
        self.mutations: list[str] = []
        self.labels: dict[str, tuple[str, str]] = {}
        self.fail_create = False

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        self.labels.update(labels)

    async def list_issues(self, label: str) -> list[IssueRecord]:
        return [issue for issue in self.issues if label in issue.labels]

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
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
    ) -> IssueRecord:
        issue = next(item for item in self.issues if item.number == number)
        update: dict[str, object] = {}
        if title is not None:
            update["title"] = title
        if body is not None:
            update["body"] = body
        if state is not None:
            update["state"] = state
        updated = issue.model_copy(update=update)
        self.issues[self.issues.index(issue)] = updated
        self.mutations.append("update")
        return updated

    async def add_labels(self, number: int, labels: list[str]) -> None:
        issue = next(item for item in self.issues if item.number == number)
        issue.labels = list(dict.fromkeys([*issue.labels, *labels]))
        self.mutations.append("add_labels")

    async def remove_label(self, number: int, label: str) -> None:
        issue = next(item for item in self.issues if item.number == number)
        issue.labels = [item for item in issue.labels if item != label]
        self.mutations.append("remove_label")

    async def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))
        self.mutations.append("comment")


class FakeAgent:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, object]]] = []
        self.states: dict[str, SessionState] = {}

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
        return self.states[session_id]

    @staticmethod
    def map_status(status: str, status_detail: str) -> SessionStatus | None:
        return DevinClient.map_status(status, status_detail)


def example_manifest() -> Path:
    return Path(__file__).parents[1] / "examples" / "ambient-home" / "garage.toml"
