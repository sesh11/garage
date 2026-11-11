"""Minimal GitHub Issues client for Garage."""

import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from garage.tracker import IssueRecord, legacy_label, canonical_label


_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def _record(payload: dict[str, object]) -> IssueRecord:
    raw_labels = payload.get("labels") or []
    labels = (
        [str(item.get("name")) for item in raw_labels if isinstance(item, dict)]
        if isinstance(raw_labels, list)
        else []
    )
    return IssueRecord.model_validate(
        {
            "number": payload["number"],
            "url": payload["html_url"],
            "state": payload["state"],
            "title": payload.get("title") or "",
            "body": payload.get("body") or "",
            "labels": list(dict.fromkeys(canonical_label(label) for label in labels)),
            "created_at": payload["created_at"],
            "closed_at": payload.get("closed_at"),
            "updated_at": payload.get("updated_at"),
            "state_reason": payload.get("state_reason"),
            "author": user.get("login") if isinstance(user := payload.get("user"), dict) else None,
            "pull_request": "pull_request" in payload,
        }
    )


class GitHubIssues:
    """Create, update, and list issues in one repository through the REST API."""

    def __init__(
        self,
        repo: str,
        token: str | None,
        *,
        base_url: str = "https://api.github.com",
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        """Configure the target repository and optional token (reads work without one on public repos)."""
        self.repo = repo
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._http = http
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self, method: str, url: str, json: object | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"
        merged = {**self._headers(), **(headers or {})}
        if self._http is not None:
            response = await self._http.request(method, url, json=json, headers=merged)
        else:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.request(method, url, json=json, headers=merged)
        if response.status_code != 304:
            response.raise_for_status()
        return response

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.repo}"

    async def list_issues(self, label: str) -> list[IssueRecord]:
        """Return every open and closed issue (not PR) carrying ``label``, under its old ``factory:`` name too."""
        found: dict[int, IssueRecord] = {}
        for name in dict.fromkeys(filter(None, [label, legacy_label(label)])):
            for item in await self._pages(f"{self._repo_path}/issues?state=all&labels={quote(name)}&per_page=100"):
                if "pull_request" not in item:
                    found.setdefault(int(str(item["number"])), _record(item))
        return [found[number] for number in sorted(found)]

    async def _pages(self, url: str | None) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        while url:
            response = await self._request("GET", url)
            payload = response.json()
            if isinstance(payload, list):
                items.extend(item for item in payload if isinstance(item, dict))
            match = _NEXT.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
        return items

    async def last_actor(self, number: int, event: str, label: str | None = None) -> str | None:
        """Return the GitHub login that last did ``event`` on the issue (for ``labeled``, with that label)."""
        login: str | None = None
        for item in await self._pages(f"{self._repo_path}/issues/{number}/events?per_page=100"):
            if item.get("event") != event:
                continue
            named = item.get("label")
            if label is not None and not (
                isinstance(named, dict) and canonical_label(str(named.get("name"))) == label
            ):
                continue
            actor = item.get("actor")
            login = str(actor.get("login")) if isinstance(actor, dict) and actor.get("login") else login
        return login

    async def legacy_labeled(self, *, include_closed: bool = False) -> list[tuple[int, str, list[str]]]:
        """Return (number, title, old ``factory:`` labels) for every issue or PR still carrying one."""
        state = "all" if include_closed else "open"
        found: list[tuple[int, str, list[str]]] = []
        for item in await self._pages(f"{self._repo_path}/issues?state={state}&per_page=100"):
            labels = item.get("labels") or []
            names = (
                [str(label.get("name")) for label in labels if isinstance(label, dict)]
                if isinstance(labels, list)
                else []
            )
            old = [name for name in names if canonical_label(name) != name]
            if old:
                found.append((int(str(item["number"])), str(item.get("title") or ""), old))
        return found

    async def changed_since(
        self, since: datetime, etag: str | None = None
    ) -> tuple[list[IssueRecord], str | None, bool]:
        """List issues and PRs updated at or after ``since``, oldest change first.

        Sends ``If-None-Match`` with the previous ETag; GitHub answers an unchanged list with 304, which does not
        count against the rate limit. Returns (records, etag, not_modified).
        """
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url: str | None = f"{self._repo_path}/issues?state=all&sort=updated&direction=asc&since={stamp}&per_page=100"
        found: list[IssueRecord] = []
        first = True
        new_etag: str | None = None
        while url:
            response = await self._request("GET", url, headers={"If-None-Match": etag} if first and etag else None)
            if first:
                if response.status_code == 304:
                    return [], etag, True
                new_etag = response.headers.get("etag")
                first = False
            payload = response.json()
            if isinstance(payload, list):
                found.extend(_record(item) for item in payload if isinstance(item, dict))
            match = _NEXT.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
        return found, new_etag, False

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """Create any missing labels; ``labels`` maps name to (color, description)."""
        response = await self._request("GET", f"{self._repo_path}/labels?per_page=100")
        existing = {str(item.get("name")) for item in response.json() if isinstance(item, dict)}
        for name, (color, description) in labels.items():
            if name in existing:
                continue
            try:
                await self._request(
                    "POST", f"{self._repo_path}/labels", {"name": name, "color": color, "description": description}
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422:
                    raise

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        """Open a new issue."""
        response = await self._request(
            "POST", f"{self._repo_path}/issues", {"title": title, "body": body, "labels": labels}
        )
        return _record(response.json())

    async def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> IssueRecord:
        """Edit an issue's title, body, or state (with an optional close reason)."""
        payload: dict[str, object] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if state_reason is not None:
            payload["state_reason"] = state_reason
        response = await self._request("PATCH", f"{self._repo_path}/issues/{number}", payload)
        return _record(response.json())

    async def add_labels(self, number: int, labels: list[str]) -> None:
        """Add labels without touching any others on the issue."""
        await self._request("POST", f"{self._repo_path}/issues/{number}/labels", {"labels": labels})

    async def remove_label(self, number: int, label: str) -> None:
        """Remove one label (and its old ``factory:`` name), ignoring it if it is already gone."""
        for name in filter(None, [label, legacy_label(label)]):
            try:
                await self._request("DELETE", f"{self._repo_path}/issues/{number}/labels/{quote(name, safe='')}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise

    async def comment(self, number: int, body: str) -> None:
        """Post a comment on an issue."""
        await self._request("POST", f"{self._repo_path}/issues/{number}/comments", {"body": body})

    async def create_pull_request(self, title: str, body: str, *, head: str, base: str) -> str:
        """Return an existing branch PR or open one, making publication retryable."""
        owner = self.repo.split("/")[0]
        response = await self._request(
            "GET",
            f"{self._repo_path}/pulls?state=all&head={quote(owner + ':' + head, safe='')}&base={quote(base, safe='')}",
        )
        existing = response.json()
        if existing:
            return str(existing[0]["html_url"])
        response = await self._request(
            "POST", f"{self._repo_path}/pulls", {"title": title, "body": body, "head": head, "base": base}
        )
        return str(response.json()["html_url"])
