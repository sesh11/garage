"""Issue order: ``Depends on #N`` lines in an issue body hold it until every dependency is closed as completed."""

from __future__ import annotations
import re
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from garage.tracker import IssueRecord


_LINE = re.compile(r"\bdepends\s+on\b\s*:?(?P<rest>[^\n]*)", re.IGNORECASE)
_REF = re.compile(
    r"(?:https?://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/(?:issues|pull)/"
    r"|(?P<repo>[\w.-]+/[\w.-]+)#"
    r"|#)(?P<number>\d+)",
    re.IGNORECASE,
)
_MAX_DEPTH = 50


@dataclass(frozen=True)
class Dependency:
    """One ``Depends on`` reference: an issue number, and the repository it names if it names one."""

    number: int
    repo: str | None = None


def parse_dependencies(body: str) -> list[Dependency]:
    """Return the dependencies an issue body names, in order, without repeats."""
    found: dict[Dependency, None] = {}
    for line in _LINE.finditer(body):
        for ref in _REF.finditer(line.group("rest")):
            repo = ref.group("url_repo") or ref.group("repo")
            found.setdefault(Dependency(int(ref.group("number")), repo.lower() if repo else None))
    return list(found)


def _completed(issue: IssueRecord) -> bool:
    return issue.state == "closed" and issue.state_reason in (None, "completed")


def _refs(numbers: list[int]) -> str:
    return ", ".join(f"#{number}" for number in numbers)


@dataclass
class DependencyCheck:
    """Decide whether issues wait on their dependencies, reading each dependency at most once."""

    repo: str
    lookup: Callable[[int], Awaitable[IssueRecord | None]]
    known: dict[int, IssueRecord | None] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)

    async def _get(self, number: int) -> IssueRecord | None:
        if number not in self.known and number not in self.errors:
            try:
                self.known[number] = await self.lookup(number)
            except Exception as exc:
                self.errors[number] = str(exc) or type(exc).__name__
        return self.known.get(number)

    def _same_repo(self, dependency: Dependency) -> bool:
        return dependency.repo is None or dependency.repo == self.repo.lower()

    async def _cycle(self, root: int, start: int) -> list[int] | None:
        """Return a path ``root -> start -> ... -> root`` through open issues, or None."""
        stack: list[tuple[int, list[int]]] = [(start, [root, start])]
        seen = {start}
        while stack:
            number, path = stack.pop()
            if len(path) > _MAX_DEPTH:
                continue
            issue = await self._get(number)
            if issue is None or issue.state != "open":
                continue
            for dependency in parse_dependencies(issue.body):
                if not self._same_repo(dependency):
                    continue
                if dependency.number == root:
                    return [*path, root]
                if dependency.number not in seen:
                    seen.add(dependency.number)
                    stack.append((dependency.number, [*path, dependency.number]))
        return None

    async def hold(self, issue: IssueRecord) -> str | None:
        """Return why ``issue`` must wait for its dependencies, or None when it may start."""
        problems: list[str] = []
        waiting: list[int] = []
        for dependency in parse_dependencies(issue.body):
            if not self._same_repo(dependency):
                problems.append(
                    f"depends on {dependency.repo}#{dependency.number}, but only issues in {self.repo} can be "
                    "dependencies"
                )
                continue
            number = dependency.number
            if number == issue.number:
                problems.append("depends on itself")
                continue
            found = await self._get(number)
            if number in self.errors:
                problems.append(f"could not read dependency #{number}: {self.errors[number]}")
            elif found is None:
                problems.append(f"dependency #{number} does not exist")
            elif found.pull_request:
                problems.append(f"dependency #{number} is a pull request, not an issue")
            elif found.state == "open":
                cycle = await self._cycle(issue.number, number)
                if cycle is not None:
                    problems.append("dependency cycle " + " -> ".join(f"#{step}" for step in cycle))
                else:
                    waiting.append(number)
            elif not _completed(found):
                reason = (found.state_reason or "").replace("_", " ")
                problems.append(f"dependency #{number} was closed as {reason}, not completed")
        if problems:
            return "; ".join(problems)
        if waiting:
            return f"waiting on {_refs(waiting)} (still open)"
        return None
