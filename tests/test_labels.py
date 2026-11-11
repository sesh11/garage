# mypy: disable-error-code="no-untyped-def"
"""The ``factory:*`` to ``garage:*`` label rename: old names are read, only new names are written."""

import json
from urllib.parse import unquote, parse_qs, urlparse

import httpx
import pytest
from conftest import FakeAgent, example_manifest

from garage.github import GitHubIssues
from garage.report import READY, FIXING, LABELS, REQUEST, PROPOSED, DISPATCHED
from garage.tracker import legacy_label, canonical_label
from garage.dispatch import Dispatcher
from garage.settings import settings_from_env


class FakeGitHub:
    """A tiny stateful GitHub REST API that keeps each issue's raw label names."""

    def __init__(self, issues: dict[int, list[str]], closed: frozenset[int] = frozenset()) -> None:
        self.labels = {number: list(names) for number, names in issues.items()}
        self.closed = closed
        self.repo_labels: set[str] = {"factory:proposed"}
        self.writes: list[str] = []

    def _issue(self, number: int) -> dict[str, object]:
        return {
            "number": number,
            "html_url": f"https://github.com/o/r/issues/{number}",
            "state": "closed" if number in self.closed else "open",
            "title": f"issue {number}",
            "body": "",
            "labels": [{"name": name} for name in self.labels[number]],
            "created_at": "2026-11-08T12:00:00Z",
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = "/" + unquote(urlparse(str(request.url)).path).split("/", 4)[-1]
        query = parse_qs(urlparse(str(request.url)).query)
        if request.method != "GET":
            self.writes.append(f"{request.method} {path}")
        if request.method == "GET" and path == "/issues":
            state, wanted = query["state"][0], query.get("labels", [None])[0]
            found = [
                self._issue(number)
                for number in sorted(self.labels)
                if (state == "all" or number not in self.closed) and (wanted is None or wanted in self.labels[number])
            ]
            return httpx.Response(200, json=found)
        if request.method == "GET" and path == "/labels":
            return httpx.Response(200, json=[{"name": name} for name in sorted(self.repo_labels)])
        if request.method == "POST" and path == "/labels":
            self.repo_labels.add(json.loads(request.content)["name"])
            return httpx.Response(201, json={})
        if path.startswith("/issues/") and "/labels" in path:
            number = int(path.split("/")[2])
            if request.method == "POST":
                for name in json.loads(request.content)["labels"]:
                    if name not in self.labels[number]:
                        self.labels[number].append(name)
                return httpx.Response(200, json=[])
            name = path.split("/labels/", 1)[1]
            if name not in self.labels[number]:
                return httpx.Response(404, json={})
            self.labels[number].remove(name)
            return httpx.Response(200, json=[])
        if path.endswith("/comments"):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})


def test_canonical_and_legacy_names() -> None:
    assert canonical_label("factory:ready-for-fix") == READY == "garage:ready-for-fix"
    assert canonical_label("garage:proposed") == PROPOSED
    assert canonical_label("bug") == "bug"
    assert legacy_label(DISPATCHED) == "factory:dispatched"
    assert legacy_label("bug") is None
    assert all(name.startswith("garage:") for name in LABELS)


@pytest.mark.asyncio
async def test_old_labels_are_read_as_new_and_removed_under_either_name() -> None:
    github_api = FakeGitHub(
        {1: ["factory:ready-for-fix"], 2: ["garage:ready-for-fix", "factory:ready-for-fix"], 3: []}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(github_api)) as http:
        github = GitHubIssues("o/r", "token", http=http)
        ready = await github.list_issues(READY)
        assert [(issue.number, issue.labels) for issue in ready] == [(1, [READY]), (2, [READY])]
        await github.remove_label(1, READY)
        await github.remove_label(2, READY)
    assert github_api.labels[1] == [] and github_api.labels[2] == []


@pytest.mark.asyncio
async def test_old_label_issue_dispatches_once_and_old_dispatched_is_respected(tmp_path) -> None:
    settings = settings_from_env(example_manifest())
    github_api = FakeGitHub({1: ["factory:ready-for-fix"], 2: ["factory:ready-for-fix", "factory:dispatched"]})
    agent = FakeAgent()
    async with httpx.AsyncClient(transport=httpx.MockTransport(github_api)) as http:
        github = GitHubIssues("o/r", "token", http=http)
        dispatcher = Dispatcher(
            github, agent, tmp_path / "dispatch.json", manifest=settings.manifest, interval_s=60, dry_run=False
        )
        await dispatcher.poll_once()
        await dispatcher.poll_once()
    assert len(agent.created) == 1
    assert github_api.labels[1] == ["factory:ready-for-fix", DISPATCHED, FIXING]
    assert not any("factory:" in write for write in github_api.writes if write.startswith("POST"))


def test_labels_migrate_dry_run_then_apply(monkeypatch, tmp_path, capsys) -> None:
    from garage import cli

    github_api = FakeGitHub(
        {1: ["factory:proposed", "bug"], 2: ["garage:request"], 3: ["factory:request", "factory:proposed"], 4: []},
        closed=frozenset({3}),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(github_api))
    monkeypatch.setattr(cli, "GitHubIssues", lambda repo, token: GitHubIssues(repo, token, http=http))
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARAGE_GITHUB_TOKEN", "fake")
    manifest = str(example_manifest())

    assert cli.main(["labels", "migrate", "--manifest", manifest]) == 0
    out = capsys.readouterr().out
    assert "[dry run] sesh11/ambient-home#1 issue 1: factory:proposed -> garage:proposed" in out
    assert "#3" not in out and "Run again with --apply" in out
    assert github_api.writes == []

    assert cli.main(["labels", "migrate", "--manifest", manifest, "--apply", "--include-closed"]) == 0
    assert github_api.labels == {1: ["bug", PROPOSED], 2: [REQUEST], 3: [REQUEST, PROPOSED], 4: []}
    assert set(LABELS) <= github_api.repo_labels
    assert not any("factory:" in write for write in github_api.writes if write.startswith("POST"))

    assert cli.main(["labels", "migrate", "--manifest", manifest]) == 0
    assert "no factory:* labels left in sesh11/ambient-home" in capsys.readouterr().out
