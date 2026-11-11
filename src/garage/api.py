"""Authenticated local inbox endpoints."""

import base64
import asyncio
from collections.abc import Callable, Sequence, Awaitable

from fastapi import Request, APIRouter
from pydantic import ValidationError
from fastapi.responses import JSONResponse

from garage_sdk import emit
from garage.store import UNASSIGNED, Store
from garage_sdk.api import (
    RunLink,
    ItemList,
    InboxItem,
    IssueLink,
    ItemState,
    ClientKind,
    SubmitWork,
    ProjectInfo,
    ProjectList,
    AssignProject,
    normalize_project,
)
from garage.approval import initial_state
from garage.requests import Request as GarageRequest
from garage.requests import RequestBridge


def router(
    store: Store | None,
    bridge: RequestBridge | None,
    check: Callable[[Request, bool], JSONResponse | None],
    read_body: Callable[[Request], Awaitable[bytes | JSONResponse]],
    project_list: Sequence[ProjectInfo] = (),
) -> APIRouter:
    """Build inbox routes using the bridge's local transport protections."""
    routes = APIRouter(prefix="/v1")
    lock = asyncio.Lock()

    def authorize(request: Request) -> dict[str, str | None] | JSONResponse:
        path = request.url.path
        denied = check(request, request.method == "POST" and (path == "/v1/items" or path.endswith("/project")))
        if denied is not None:
            return denied
        if store is None:
            return JSONResponse({"error": "inbox unavailable"}, status_code=503)
        header = request.headers.get("authorization", "")
        client = store.authenticate(header[7:]) if header.startswith("Bearer ") else None
        return client if client else JSONResponse({"error": "unauthorized"}, status_code=401)

    def visible(item: InboxItem, client: dict[str, str | None]) -> bool:
        return client["kind"] == "owner" or item.submitted_by == client["name"]

    def resolve(payload: SubmitWork, client: dict[str, str | None]) -> tuple[str | None, str | None] | JSONResponse:
        """Pick the item's project and repository; a named project must exist and own the repository."""
        named = payload.project or (client.get("default_project") if payload.repo is None else None)
        if named is not None:
            target = bridge.project(named) if bridge is not None else None
            if target is None or target.name is None:
                if payload.project is not None:
                    return JSONResponse({"error": f"unknown project {named!r}"}, status_code=422)
            else:
                if payload.repo is not None and payload.repo != (target.repo or "").lower():
                    return JSONResponse(
                        {"error": f"repo {payload.repo} does not belong to project {target.name}"}, status_code=422
                    )
                return target.name, payload.repo or target.repo
        repo = payload.repo or client["default_repo"]
        target = bridge.project(repository=repo) if bridge is not None and repo else None
        if repo and target is None and bridge is not None and any(project.name for project in bridge.projects):
            return JSONResponse({"error": f"repo {repo} is not in any project"}, status_code=422)
        return (target.name if target is not None else None), repo

    async def file(item: InboxItem, name: str) -> JSONResponse | None:
        """File an open item for its project; returns an error response when the tracker fails."""
        if (
            item.issue is not None
            or not item.repos
            or item.state in {ItemState.cancelled, ItemState.declined, ItemState.done}
            or bridge is None
        ):
            return None
        try:
            issue = await bridge.file(
                GarageRequest(
                    id=item.id,
                    request=item.text,
                    created_at=item.created_at,
                    repository=item.repos[0].full_name,
                    project=item.project,
                )
            )
        except Exception as exc:
            assert store is not None
            store.save(item, name, "file_failed", {"exc_type": type(exc).__name__})
            emit(
                "factory.report_failed",
                "factory",
                "error",
                op="file",
                request_id=item.submit_id,
                exc_type=type(exc).__name__,
            )
            return JSONResponse({"error": "issue tracker request failed"}, status_code=502)
        if issue is not None and store is not None:
            item.issue = IssueLink(number=issue.number, url=issue.url)
            item.state = initial_state(issue.labels, needs_approval=True)
            store.save(item, name, "file_success")
        return None

    @routes.post("/items", response_model=None)
    async def submit(request: Request) -> JSONResponse:
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        assert store is not None
        body = await read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            payload = SubmitWork.model_validate_json(body)
        except ValidationError:
            return JSONResponse({"error": "invalid request"}, status_code=422)
        resolved = resolve(payload, client)
        if isinstance(resolved, JSONResponse):
            return resolved
        project, repo = resolved
        async with lock:
            name = str(client["name"])
            item, created = store.submit(name, ClientKind(str(client["kind"])), payload, repo, project)
            failed = await file(item, name)
            if failed is not None:
                return failed
            return JSONResponse(item.model_dump(mode="json"), status_code=201 if created else 200)

    @routes.get("/projects", response_model=None)
    async def projects(request: Request) -> JSONResponse:
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        return JSONResponse(ProjectList(projects=list(project_list)).model_dump(mode="json"))

    @routes.post("/items/{item_id}/project", response_model=None)
    async def assign(item_id: str, request: Request) -> JSONResponse:
        """Owner-only: put an item in a project (for example one waiting in needs_repo), then file it."""
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        assert store is not None
        if client["kind"] != "owner":
            return JSONResponse({"error": "only the owner can assign projects"}, status_code=403)
        body = await read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            payload = AssignProject.model_validate_json(body)
        except ValidationError:
            return JSONResponse({"error": "invalid request"}, status_code=422)
        target = bridge.project(payload.project) if bridge is not None else None
        if target is None or target.name is None or target.repo is None:
            return JSONResponse({"error": f"unknown project {payload.project!r}"}, status_code=422)
        async with lock:
            item = store.get(item_id)
            if item is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            if item.issue is not None or item.state in {ItemState.done, ItemState.declined, ItemState.cancelled}:
                return JSONResponse({"error": "item is already filed or closed"}, status_code=409)
            name = str(client["name"])
            store.assign(item, target.name, target.repo, name)
            failed = await file(item, name)
            if failed is not None:
                return failed
        return JSONResponse(item.model_dump(mode="json"))

    @routes.get("/items", response_model=None)
    async def listing(
        request: Request,
        state: ItemState | None = None,
        project: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> JSONResponse:
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        assert store is not None
        if not 1 <= limit <= 200:
            return JSONResponse({"error": "limit must be between 1 and 200"}, status_code=422)
        if project is not None and project != UNASSIGNED:
            try:
                project = normalize_project(project)
            except ValueError:
                return JSONResponse({"error": "invalid project"}, status_code=422)
        after: tuple[str, str] | None = None
        if cursor:
            try:
                timestamp, item_id = (
                    base64.b64decode(cursor.encode(), altchars=b"-_", validate=True).decode().split("|", 1)
                )
                after = timestamp, item_id
            except (ValueError, UnicodeError):
                return JSONResponse({"error": "invalid cursor"}, status_code=422)
        items = [
            item
            for item in store.items(state, project)
            if visible(item, client) and (after is None or (item.created_at.isoformat(), item.id) < after)
        ]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()
        return JSONResponse(ItemList(items=page, next_cursor=next_cursor).model_dump(mode="json"))

    @routes.get("/items/{item_id}", response_model=None)
    async def get(item_id: str, request: Request) -> JSONResponse:
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        assert store is not None
        item = store.get(item_id)
        if item is None or not visible(item, client):
            return JSONResponse({"error": "not found"}, status_code=404)
        status = bridge.status_of(item.issue.number, item.project) if item.issue and bridge else None
        if item.issue and status is not None and status["dispatch"]:
            previous_run, previous_state = item.run, item.state
            item.run = RunLink.model_validate(status)
            if item.state not in {ItemState.done, ItemState.declined, ItemState.cancelled} and item.run.state in {
                "working",
                "blocked",
            }:
                item.state = ItemState.running
            # A finished agent session is not proof that its change shipped.
            if item.run != previous_run or item.state != previous_state:
                store.save(item, str(client["name"]), "status")
        return JSONResponse(item.model_dump(mode="json"))

    @routes.post("/items/{item_id}/cancel", response_model=None)
    async def cancel(item_id: str, request: Request) -> JSONResponse:
        """Cancel the local record only; this does not touch GitHub or stop an agent."""
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        body = await read_body(request)
        if isinstance(body, JSONResponse):
            return body
        assert store is not None
        async with lock:
            item = store.get(item_id)
            if item is None or not visible(item, client):
                return JSONResponse({"error": "not found"}, status_code=404)
            if item.state in {ItemState.done, ItemState.declined, ItemState.cancelled}:
                return JSONResponse({"error": "item is terminal"}, status_code=409)
            item.state = ItemState.cancelled
            store.save(item, str(client["name"]), "cancel")
        return JSONResponse(item.model_dump(mode="json"))

    return routes
