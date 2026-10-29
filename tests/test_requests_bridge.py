# mypy: disable-error-code="no-untyped-def,no-untyped-call"

import asyncio
from datetime import datetime, timezone

import httpx
from conftest import FakeTracker
from fastapi.testclient import TestClient

from garage.ui import MAX_REQUEST_BODY_BYTES, create_app
from garage_sdk import configure
from garage.report import REQUEST, RequestLogger
from garage.tracker import IssueRecord
from garage.requests import RequestBridge
from garage_sdk.events import EventLog


def payload(**updates) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "client:123",
        "request": "Please add this feature",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": "sesh11/ambient-home",
    }
    result.update(updates)
    return result


def make_app(tracker: FakeTracker | None = None, token: str | None = None):
    logger = RequestLogger(tracker) if tracker is not None else None
    return create_app(
        event_log=None,
        detector=None,
        bridge=RequestBridge(logger, bridge_token=token),
        allowed_clients=frozenset({"testclient", "127.0.0.1", "::1"}),
    )


def test_auth_loopback_validation_and_disabled_responses() -> None:
    app = make_app(token="secret")
    with TestClient(app) as client:
        assert client.post("/api/requests", json=payload()).status_code == 401
        assert client.post("/api/requests", json=payload(), headers={"Authorization": "Bearer wrong"}).json() == {
            "error": "unauthorized"
        }
        assert client.post("/api/requests", json=payload(), headers={"Authorization": "Bearer secret"}).json() == {
            "issue": None
        }
        assert (
            client.post(
                "/api/requests",
                json=payload(),
                headers={"Authorization": "Bearer secret"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/requests",
                content="{",
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            ).status_code
            == 422
        )
    remote = TestClient(app, client=("192.0.2.10", 10000))
    response = remote.post("/api/requests", json=payload(), headers={"Authorization": "Bearer secret"})
    assert response.status_code == 403
    assert response.json() == {"error": "loopback only"}


def test_validation_rejects_extra_blank_and_bad_ids() -> None:
    with TestClient(make_app()) as client:
        assert client.post("/api/requests", json=payload(extra="x")).status_code == 422
        assert client.post("/api/requests", json=payload(id="invalid id")).status_code == 422
        assert client.post("/api/requests", json=payload(request="  ")).status_code == 422
        assert client.post("/api/requests", json=payload(created_at="2025-01-01T00:00:00")).status_code == 422


def test_duplicate_requests_are_idempotent_and_concurrent() -> None:
    tracker = FakeTracker()
    app = make_app(tracker)
    body = payload()
    with TestClient(app) as client:
        assert client.post("/api/requests", json=body).json()["issue"]["number"] == 1
        assert client.post("/api/requests", json=body).json()["issue"]["number"] == 1
    assert len(tracker.creates) == 1
    assert len(tracker.issues) == 1

    concurrent_tracker = FakeTracker()
    concurrent_app = make_app(concurrent_tracker)

    async def concurrent_posts() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=concurrent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await asyncio.gather(*(client.post("/api/requests", json=payload()) for _ in range(8)))

    results = asyncio.run(concurrent_posts())
    assert all(response.status_code == 200 for response in results)
    assert len(concurrent_tracker.creates) == 1


def test_null_or_mismatched_repository_and_daily_cap_return_null() -> None:
    tracker = FakeTracker()
    with TestClient(make_app(tracker)) as client:
        assert client.post("/api/requests", json=payload(repository=None)).json() == {"issue": None}
        assert client.post(
            "/api/requests",
            json=payload(id="other", repository="https://github.com/other/ambient-home.git"),
        ).json() == {"issue": None}
    assert not tracker.creates

    capped_logger = RequestLogger(FakeTracker(), max_per_day=0)
    app = create_app(
        event_log=None,
        detector=None,
        bridge=RequestBridge(capped_logger),
        allowed_clients=frozenset({"testclient", "127.0.0.1", "::1"}),
    )
    with TestClient(app) as client:
        assert client.post("/api/requests", json=payload()).json() == {"issue": None}


def test_success_body_is_scrubbed() -> None:
    tracker = FakeTracker()
    app = make_app(tracker)
    secret = "Authorization: Bearer abcdefghijk123"
    request = payload(request=f"Please review {secret}")
    with TestClient(app) as client:
        response = client.post("/api/requests", json=request)
    assert response.status_code == 200
    assert response.json()["issue"]["number"] == 1
    body = tracker.creates[0][1]
    assert "<secret>" in body
    assert secret not in body


def test_body_limit_uses_content_length_and_actual_stream() -> None:
    app = make_app()
    with TestClient(app) as client:
        oversized = client.post("/api/requests", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
        assert oversized.status_code == 413
        assert oversized.json() == {"error": "request body too large"}

    async def post_unbounded_stream() -> httpx.Response:
        async def body():
            yield b"{" + b" " * (MAX_REQUEST_BODY_BYTES // 2)
            yield b" " * (MAX_REQUEST_BODY_BYTES // 2) + b"}"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/api/requests", content=body())

    response = asyncio.run(post_unbounded_stream())
    assert response.status_code == 413
    assert response.json() == {"error": "request body too large"}


def test_tracker_failure_is_scrub_safe_and_fixing_contract(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.fail_create = True
    log = EventLog(tmp_path / "events")
    configure(log)
    try:
        with TestClient(make_app(tracker)) as client:
            response = client.post("/api/requests", json=payload())
            assert response.status_code == 502
            assert response.json() == {"error": "issue tracker request failed"}
            assert client.post("/api/requests/0/fixing").status_code == 422
            assert client.post("/api/requests/1/fixing").status_code == 502
        events = log.read(datetime.now(timezone.utc).replace(year=2000))
        assert any(event.type == "factory.report_failed" for event in events)
    finally:
        configure(None)


def test_fixing_disabled_and_success() -> None:
    with TestClient(make_app()) as client:
        response = client.post("/api/requests/1/fixing")
        assert response.status_code == 409
        assert response.json() == {"error": "request reporting is disabled"}
    tracker = FakeTracker()
    tracker.issues.append(
        IssueRecord(
            number=1,
            url="https://github.com/sesh11/ambient-home/issues/1",
            state="open",
            title="Request",
            body="<!-- factory-request: client:123 -->",
            labels=[REQUEST],
            created_at=datetime.now(timezone.utc),
        )
    )
    with TestClient(make_app(tracker)) as client:
        assert client.post("/api/requests/1/fixing").json() == {"ok": True}
    assert "add_labels" in tracker.mutations and "remove_label" in tracker.mutations
