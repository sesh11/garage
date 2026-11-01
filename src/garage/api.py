"""Authenticated local inbox endpoints."""

import base64
import asyncio
from collections.abc import Callable, Awaitable

from fastapi import Request, APIRouter
from pydantic import ValidationError
from fastapi.responses import JSONResponse

from garage_sdk import emit
from garage.store import Store
from garage_sdk.api import RunLink, ItemList, InboxItem, IssueLink, ItemState, ClientKind, SubmitWork
from garage.requests import Request as GarageRequest
from garage.requests import RequestBridge


def router(
    store: Store | None,
    bridge: RequestBridge | None,
    check: Callable[[Request, bool], JSONResponse | None],
    read_body: Callable[[Request], Awaitable[bytes | JSONResponse]],
) -> APIRouter:
    """Build inbox routes using the bridge's local transport protections."""
    routes = APIRouter(prefix="/v1")
    lock = asyncio.Lock()

    def authorize(request: Request) -> dict[str, str | None] | JSONResponse:
        denied = check(request, request.method == "POST" and request.url.path == "/v1/items")
        if denied is not None:
            return denied
        if store is None:
            return JSONResponse({"error": "inbox unavailable"}, status_code=503)
        header = request.headers.get("authorization", "")
        client = store.authenticate(header[7:]) if header.startswith("Bearer ") else None
        return client if client else JSONResponse({"error": "unauthorized"}, status_code=401)

    def visible(item: InboxItem, client: dict[str, str | None]) -> bool:
        return client["kind"] == "owner" or item.submitted_by == client["name"]

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
        async with lock:
            name = str(client["name"])
            item, created = store.submit(
                name, ClientKind(str(client["kind"])), payload, payload.repo or client["default_repo"]
            )
            if (
                item.issue is None
                and item.repos
                and item.state not in {ItemState.cancelled, ItemState.declined, ItemState.done}
                and bridge is not None
            ):
                try:
                    issue = await bridge.file(
                        GarageRequest(
                            id=item.id,
                            request=item.text,
                            created_at=item.created_at,
                            repository=item.repos[0].full_name,
                        )
                    )
                except Exception as exc:
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
                if issue is not None:
                    item.issue = IssueLink(number=issue.number, url=issue.url)
                    item.state = ItemState.approved
                    store.save(item, name, "file_success")
            return JSONResponse(item.model_dump(mode="json"), status_code=201 if created else 200)

    @routes.get("/items", response_model=None)
    async def listing(
        request: Request, state: ItemState | None = None, limit: int = 50, cursor: str | None = None
    ) -> JSONResponse:
        client = authorize(request)
        if isinstance(client, JSONResponse):
            return client
        assert store is not None
        if not 1 <= limit <= 200:
            return JSONResponse({"error": "limit must be between 1 and 200"}, status_code=422)
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
            for item in store.items(state)
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
        if item.issue and bridge and bridge.status:
            item.run = RunLink.model_validate(bridge.status_of(item.issue.number))
            if item.state not in {ItemState.done, ItemState.declined, ItemState.cancelled} and item.run.state in {
                "working",
                "blocked",
            }:
                item.state = ItemState.running
            # A finished agent session is not proof that its change shipped.
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
