"""Incidents UI and local request API."""

import hmac
import json
import logging
import ipaddress
from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Sequence

from fastapi import FastAPI, Request
from pydantic import Field, BaseModel, ConfigDict, AwareDatetime, ValidationError, field_validator
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from garage_sdk import emit
from garage.report import IncidentReporter
from garage.tracker import IssueRecord
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

    @field_validator("request")
    @classmethod
    def _request_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request must not be blank")
        return value


def create_app(
    *,
    event_log: EventLog | None,
    detector: Detector | None,
    reporter: IncidentReporter | None = None,
    bridge: RequestBridge | None = None,
    allowed_clients: frozenset[str] = frozenset({"127.0.0.1", "::1"}),
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"}),
) -> FastAPI:
    """Create the local Garage incidents UI and optional request bridge."""
    app = FastAPI(title="garage")
    app.state.event_log = event_log
    app.state.monitor = None
    app.state.detector = detector
    index_path = Path(__file__).parent / "ui" / "index.html"
    normalized_allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/incidents")

    @app.get("/incidents")
    async def incidents_page() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "detector": detector.name if detector is not None else None,
            "reporting": reporter is not None,
        }

    @app.get("/api/events", response_model=None)
    async def events(
        since: str = "12h", type: str | None = None, limit: int = 500
    ) -> dict[str, object] | JSONResponse:
        now = datetime.now(timezone.utc)
        try:
            start = now - parse_duration(since)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if event_log is None:
            return {"since": start.isoformat(), "count": 0, "events": []}
        matched = event_log.read(start, now)
        if type is not None:
            matched = [event for event in matched if event.type == type]
        matched = sorted(matched, key=lambda event: event.ts, reverse=True)[: max(0, min(limit, 5000))]
        return {
            "since": start.isoformat(),
            "count": len(matched),
            "events": [event.model_dump(mode="json") for event in matched],
        }

    @app.get("/api/incidents", response_model=None)
    async def incidents(since: str = "24h") -> dict[str, object] | JSONResponse:
        now = datetime.now(timezone.utc)
        try:
            start = now - parse_duration(since)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        found: Sequence[Incident] = []
        if event_log is not None and detector is not None:
            found = await detector.detect(event_log.read(start, now), now)
        return {
            "detector": detector.name if detector is not None else None,
            "generated_at": now.isoformat(),
            "incidents": [
                {
                    **incident.model_dump(mode="json"),
                    "issue_url": reporter.issue_url(incident.fingerprint) if reporter is not None else None,
                }
                for incident in found
            ],
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

    def check_bridge_request(request: Request, *, require_json: bool = False) -> JSONResponse | None:
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

        token = bridge.bridge_token if bridge is not None else None
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
        if bridge is None or not bridge.enabled:
            return {"issue": None}
        try:
            issue = await bridge.file(request_model(payload))
        except Exception as exc:
            emit(
                "factory.report_failed",
                "factory",
                "error",
                op="file",
                request_id=payload.id,
                exc_type=type(exc).__name__,
            )
            return JSONResponse(status_code=502, content={"error": "issue tracker request failed"})
        return issue_response(issue)

    @app.get("/api/requests/{number}", response_model=None)
    async def request_status(number: str, request: Request) -> dict[str, object] | JSONResponse:
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
        return bridge.status_of(issue_number)

    @app.post("/api/requests/{number}/fixing", response_model=None)
    async def mark_fixing(number: str, request: Request) -> dict[str, bool] | JSONResponse:
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
        try:
            await bridge.mark_fixing(issue_number)
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
        return {"ok": True}

    return app
