"""Incidents UI and local request API."""

import hmac
import json
import getpass
import logging
import ipaddress
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from fastapi import FastAPI, Request
from pydantic import Field, BaseModel, ConfigDict, AwareDatetime, ValidationError, field_validator
from fastapi.responses import Response, FileResponse, JSONResponse, RedirectResponse

from garage.api import router
from garage_sdk import emit
from garage.store import UNASSIGNED, Store
from garage.report import IncidentReporter
from garage.tracker import IssueRecord
from garage_sdk.api import IssueLink, ItemState, ClientKind, SubmitWork, ProjectInfo, normalize_project
from garage.approval import Approvals, ApprovalError, describe, initial_state
from garage.requests import Request as GarageRequest
from garage.requests import RequestBridge
from garage_sdk.events import EventLog
from garage.incidents.base import Detector, Incident, parse_duration


logger = logging.getLogger(__name__)
MAX_REQUEST_BODY_BYTES = 16384


class _RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    request: str = Field(min_length=1, max_length=4000)
    created_at: AwareDatetime
    repository: str | None = Field(default=None, min_length=1, max_length=300)
    project: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("project")
    @classmethod
    def _project_name(cls, value: str | None) -> str | None:
        return normalize_project(value) if value is not None else None

    @field_validator("request")
    @classmethod
    def _request_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request must not be blank")
        return value


@dataclass
class UiProject:
    """What the dashboard shows for one project."""

    name: str | None
    event_log: EventLog | None
    detector: Detector | None
    reporter: IncidentReporter | None = None
    info: ProjectInfo | None = None


def create_app(
    *,
    event_log: EventLog | None = None,
    detector: Detector | None = None,
    reporter: IncidentReporter | None = None,
    bridge: RequestBridge | None = None,
    store: Store | None = None,
    projects: Sequence[UiProject] | None = None,
    approvals: Approvals | None = None,
    allowed_clients: frozenset[str] = frozenset({"127.0.0.1", "::1"}),
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"}),
) -> FastAPI:
    """Create the local Garage incidents UI and optional request bridge for one or more projects."""
    shown = list(projects) if projects is not None else [UiProject(None, event_log, detector, reporter)]
    if shown:
        event_log, detector, reporter = shown[0].event_log, shown[0].detector, shown[0].reporter
    app = FastAPI(title="garage")
    app.state.event_log = event_log
    app.state.monitor = None
    app.state.monitors = []
    app.state.detector = detector

    def detector_names(entries: Sequence[UiProject]) -> str | None:
        names = list(dict.fromkeys(entry.detector.name for entry in entries if entry.detector is not None))
        return ", ".join(names) or None

    def chosen(project: str | None) -> list[UiProject] | JSONResponse:
        if project is None or project == "":
            return shown
        picked = [entry for entry in shown if entry.name == project.strip().lower()]
        return picked or JSONResponse(status_code=404, content={"error": f"unknown project {project!r}"})

    index_path = Path(__file__).parent / "ui" / "index.html"
    normalized_allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    @app.get("/")
    async def index(request: Request) -> RedirectResponse:
        query = request.url.query
        return RedirectResponse("/incidents" + (f"?{query}" if query else ""))

    @app.get("/incidents")
    async def incidents_page() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "detector": detector_names(shown),
            "reporting": any(entry.reporter is not None for entry in shown),
            "projects": [entry.name for entry in shown if entry.name is not None],
        }

    @app.get("/api/projects")
    async def project_list() -> dict[str, object]:
        return {
            "projects": [
                {
                    **(entry.info.model_dump(mode="json") if entry.info is not None else {"name": entry.name}),
                    "detector": entry.detector.name if entry.detector is not None else None,
                    "signals": entry.event_log is not None,
                    "reporting": entry.reporter is not None,
                }
                for entry in shown
                if entry.name is not None
            ]
        }

    @app.get("/api/events", response_model=None)
    async def events(
        since: str = "12h", type: str | None = None, limit: int = 500, project: str | None = None
    ) -> dict[str, object] | JSONResponse:
        now = datetime.now(timezone.utc)
        try:
            start = now - parse_duration(since)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        entries = chosen(project)
        if isinstance(entries, JSONResponse):
            return entries
        matched = [
            (entry.name, event)
            for entry in entries
            if entry.event_log is not None
            for event in entry.event_log.read(start, now)
            if type is None or event.type == type
        ]
        matched = sorted(matched, key=lambda pair: pair[1].ts, reverse=True)[: max(0, min(limit, 5000))]
        return {
            "since": start.isoformat(),
            "count": len(matched),
            "events": [
                {**event.model_dump(mode="json"), **({"project": name} if name is not None else {})}
                for name, event in matched
            ],
        }

    @app.get("/api/incidents", response_model=None)
    async def incidents(since: str = "24h", project: str | None = None) -> dict[str, object] | JSONResponse:
        now = datetime.now(timezone.utc)
        try:
            start = now - parse_duration(since)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        entries = chosen(project)
        if isinstance(entries, JSONResponse):
            return entries
        rows: list[dict[str, object]] = []
        for entry in entries:
            found: Sequence[Incident] = []
            if entry.event_log is not None and entry.detector is not None:
                found = await entry.detector.detect(entry.event_log.read(start, now), now)
            rows += [
                {
                    **incident.model_dump(mode="json"),
                    "project": entry.name,
                    "issue_url": entry.reporter.issue_url(incident.fingerprint) if entry.reporter else None,
                }
                for incident in found
            ]
        return {
            "detector": detector_names(entries),
            "generated_at": now.isoformat(),
            "incidents": rows,
        }

    def parse_authority(authority: str) -> tuple[str, str | None] | None:
        if not authority or "@" in authority:
            return None
        if authority.startswith("["):
            closing_bracket = authority.find("]")
            if closing_bracket < 0:
                return None
            hostname = authority[1:closing_bracket]
            suffix = authority[closing_bracket + 1 :]
            if suffix and not suffix.startswith(":"):
                return None
            port = suffix[1:] if suffix else None
            if port is not None and (not port.isascii() or not port.isdigit()):
                return None
            try:
                hostname = str(ipaddress.IPv6Address(hostname))
            except ipaddress.AddressValueError:
                return None
            return hostname.lower(), port

        if authority.count(":") > 1:
            return None
        hostname, separator, port = authority.partition(":")
        if not hostname or (separator and (not port or not port.isascii() or not port.isdigit())):
            return None
        return hostname.lower(), port if separator else None

    def check_bridge_request(
        request: Request, *, require_json: bool = False, bridge_auth: bool = True
    ) -> JSONResponse | None:
        if request.client is None or request.client.host not in allowed_clients:
            return JSONResponse(status_code=403, content={"error": "loopback only"})

        host_header = request.headers.get("host")
        host = parse_authority(host_header) if host_header is not None else None
        if host is None or host[0] not in normalized_allowed_hosts:
            return JSONResponse(status_code=403, content={"error": "unexpected host"})

        origin = request.headers.get("origin")
        if origin is not None:
            scheme, separator, authority = origin.partition("://")
            origin_host = parse_authority(authority) if separator and scheme.lower() == "http" else None
            if origin_host is None or origin_host != host:
                return JSONResponse(status_code=403, content={"error": "cross-origin request rejected"})
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
            return JSONResponse(status_code=403, content={"error": "cross-origin request rejected"})

        token = bridge.bridge_token if bridge is not None and bridge_auth else None
        if token is not None:
            authorization = request.headers.get("authorization", "")
            prefix = "Bearer "
            supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
            if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
        if require_json:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return JSONResponse(status_code=415, content={"error": "content type must be application/json"})
        return None

    @app.get("/api/items", response_model=None)
    async def item_list(
        request: Request, project: str | None = None, state: ItemState | None = None, limit: int = 200
    ) -> dict[str, object] | JSONResponse:
        """Inbox items for the dashboard; loopback only, like the request bridge."""
        denied = check_bridge_request(request, bridge_auth=False)
        if denied is not None:
            return denied
        if store is None:
            return {"items": []}
        if project is not None and project != UNASSIGNED:
            try:
                project = normalize_project(project)
            except ValueError:
                return JSONResponse(status_code=422, content={"error": "invalid project"})
        items = store.items(state, project or None)[: max(1, min(limit, 1000))]
        decisions = store.last_decisions()
        return {
            "items": [
                {**item.model_dump(mode="json"), "decision": describe(decisions[item.id])}
                if item.id in decisions
                else item.model_dump(mode="json")
                for item in items
            ]
        }

    @app.get("/api/items/{item_id}/history", response_model=None)
    async def item_history(item_id: str, request: Request) -> dict[str, object] | JSONResponse:
        """One item's decision log, oldest first, with a readable line per entry."""
        denied = check_bridge_request(request, bridge_auth=False)
        if denied is not None:
            return denied
        if store is None or store.get(item_id) is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        return {"history": [{**entry, "text": describe(entry)} for entry in store.history(item_id)]}

    @app.post("/api/items/{item_id}/{decision}", response_model=None)
    async def decide(item_id: str, decision: str, request: Request) -> dict[str, object] | JSONResponse:
        """Approve or decline from the dashboard's Waiting for you list; same record as GitHub and the CLI."""
        denied = check_bridge_request(request, require_json=True, bridge_auth=False)
        if denied is not None:
            return denied
        if decision not in {"approve", "decline"}:
            return JSONResponse(status_code=404, content={"error": "unknown action"})
        if approvals is None or store is None:
            return JSONResponse(status_code=503, content={"error": "approvals unavailable"})
        item = store.get(item_id)
        if item is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        try:
            act = approvals.approve if decision == "approve" else approvals.decline
            item = await act(item, getpass.getuser(), "dashboard")
        except ApprovalError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        except Exception as exc:
            logger.exception("Could not %s %s", decision, item_id)
            return JSONResponse(status_code=502, content={"error": f"GitHub update failed: {type(exc).__name__}"})
        return item.model_dump(mode="json")

    def issue_project(number: int, project: str | None) -> str | None:
        """Return the project a legacy issue number belongs to, when the caller did not say."""
        if project is not None or store is None:
            return project
        names = {item.project for item in store.items() if item.issue is not None and item.issue.number == number}
        return names.pop() if len(names) == 1 else None

    async def read_limited_body(request: Request) -> bytes | JSONResponse:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request body too large"})
            except ValueError:
                pass
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(status_code=413, content={"error": "request body too large"})
            chunks.append(chunk)
        return b"".join(chunks)

    def parse_request(body: bytes) -> _RequestIn | JSONResponse:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(status_code=422, content={"error": "invalid JSON"})
        try:
            return _RequestIn.model_validate(payload)
        except ValidationError:
            return JSONResponse(status_code=422, content={"error": "invalid request"})

    def request_model(payload: _RequestIn) -> GarageRequest:
        return GarageRequest(
            id=payload.id,
            request=payload.request,
            created_at=payload.created_at,
            repository=payload.repository,
            project=payload.project,
        )

    def issue_response(issue: IssueRecord | None) -> dict[str, object]:
        if issue is None:
            return {"issue": None}
        return {"issue": {"number": issue.number, "url": issue.url}}

    @app.post("/api/requests", response_model=None)
    async def file_request(request: Request) -> dict[str, object] | JSONResponse:
        denied = check_bridge_request(request, require_json=True)
        if denied is not None:
            return denied
        body = await read_limited_body(request)
        if isinstance(body, JSONResponse):
            return body
        payload = parse_request(body)
        if isinstance(payload, JSONResponse):
            return payload
        item = None
        if store is not None:
            # Legacy repository syntax remains accepted by the bridge unchanged.
            try:
                work = SubmitWork(
                    id=payload.id, text=payload.request, repo=payload.repository, created_at=payload.created_at
                )
            except ValidationError:
                work = SubmitWork(id=payload.id, text=payload.request, created_at=payload.created_at)
            target = (
                bridge.project(payload.project, payload.repository)
                if bridge is not None and (payload.project or payload.repository)
                else None
            )
            repo = work.repo or (target.repo if target is not None else None)
            item, _ = store.submit(
                "legacy-bridge", ClientKind.app, work, repo, target.name if target is not None else None
            )
        if bridge is None or not bridge.enabled:
            return {"issue": None}
        try:
            issue = await bridge.file(request_model(payload))
        except Exception as exc:
            if store is not None and item is not None:
                store.save(item, "legacy-bridge", "file_failed", {"exc_type": type(exc).__name__})
            emit(
                "factory.report_failed",
                "factory",
                "error",
                op="file",
                request_id=payload.id,
                exc_type=type(exc).__name__,
            )
            return JSONResponse(status_code=502, content={"error": "issue tracker request failed"})
        if store is not None and item is not None and issue is not None:
            item.issue = IssueLink(number=issue.number, url=issue.url)
            item.state = initial_state(issue.labels, needs_approval=True)
            store.save(item, "legacy-bridge", "file_success")
        return issue_response(issue)

    @app.get("/api/requests/{number}", response_model=None)
    async def request_status(
        number: str, request: Request, project: str | None = None
    ) -> dict[str, object] | JSONResponse:
        denied = check_bridge_request(request)
        if denied is not None:
            return denied
        try:
            issue_number = int(number)
        except ValueError:
            return JSONResponse(status_code=422, content={"error": "number must be at least 1"})
        if issue_number < 1:
            return JSONResponse(status_code=422, content={"error": "number must be at least 1"})
        if bridge is None or not bridge.enabled:
            return JSONResponse(status_code=409, content={"error": "request reporting is disabled"})
        return bridge.status_of(issue_number, issue_project(issue_number, project))

    @app.post("/api/requests/{number}/fixing", response_model=None)
    async def mark_fixing(number: str, request: Request, project: str | None = None) -> dict[str, bool] | JSONResponse:
        denied = check_bridge_request(request)
        if denied is not None:
            return denied
        body = await read_limited_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            issue_number = int(number)
        except ValueError:
            return JSONResponse(status_code=422, content={"error": "number must be at least 1"})
        if issue_number < 1:
            return JSONResponse(status_code=422, content={"error": "number must be at least 1"})
        if bridge is None or not bridge.enabled:
            return JSONResponse(status_code=409, content={"error": "request reporting is disabled"})
        project = issue_project(issue_number, project)
        try:
            await bridge.mark_fixing(issue_number, project)
        except Exception as exc:
            emit(
                "factory.report_failed",
                "factory",
                "error",
                op="mark_fixing",
                issue=issue_number,
                exc_type=type(exc).__name__,
            )
            return JSONResponse(status_code=502, content={"error": "issue tracker request failed"})
        if store is not None:
            for item in store.items():
                if (
                    item.issue is not None
                    and item.issue.number == issue_number
                    and (project is None or item.project == project)
                ):
                    item.state = ItemState.running
                    store.save(item, "legacy-bridge", "fixing")
        return {"ok": True}

    @app.middleware("http")
    async def protect_inbox(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
            denied = check_bridge_request(request, bridge_auth=False)
            if denied is not None:
                return denied
            if store is None:
                return JSONResponse(status_code=503, content={"error": "inbox unavailable"})
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer ") or store.authenticate(header[7:]) is None:
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)

    app.include_router(
        router(
            store,
            bridge,
            lambda request, require_json: check_bridge_request(request, require_json=require_json, bridge_auth=False),
            read_limited_body,
            [entry.info for entry in shown if entry.info is not None],
        )
    )
    return app
