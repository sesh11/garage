# mypy: disable-error-code="no-untyped-def,no-untyped-call"

import json
import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from conftest import FakeTracker
from fastapi.testclient import TestClient

from garage.ui import MAX_REQUEST_BODY_BYTES, create_app
from garage_sdk import configure
from garage.report import REQUEST, RequestLogger
from garage.tracker import IssueRecord
from garage.requests import Request, RequestBridge
from garage_sdk.events import EventLog


BASE_URL = "http://127.0.0.1:8770"


def payload(**updates) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "client:123",
        "request": "Please add this feature",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": "sesh11/ambient-home",
    }
    result.update(updates)
    return result


def loopback_client(app, **kwargs):
    return TestClient(app, base_url=BASE_URL, **kwargs)


def make_app(tracker: FakeTracker | None = None, token: str | None = None, *, status=None):
    logger = RequestLogger(tracker) if tracker is not None else None
    return create_app(
        event_log=None,
        detector=None,
        bridge=RequestBridge(logger, bridge_token=token, status=status),
        allowed_clients=frozenset({"testclient", "127.0.0.1", "::1"}),
    )


def test_auth_loopback_validation_and_disabled_responses() -> None:
    app = make_app(token="secret")
    with loopback_client(app) as client:
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
    remote = loopback_client(app, client=("192.0.2.10", 10000))
    response = remote.post("/api/requests", json=payload(), headers={"Authorization": "Bearer secret"})
    assert response.status_code == 403
    assert response.json() == {"error": "loopback only"}


@pytest.mark.asyncio
async def test_bridge_calls_on_filed_only_when_an_issue_is_created() -> None:
    filed: list[None] = []
    request = Request.model_validate(payload())
    bridge = RequestBridge(RequestLogger(FakeTracker()), on_filed=lambda: filed.append(None))

    issue = await bridge.file(request)

    assert issue is not None
    assert filed == [None]

    capped = RequestBridge(RequestLogger(FakeTracker(), max_per_day=0), on_filed=lambda: filed.append(None))
    assert await capped.file(request) is None
    assert filed == [None]


def test_request_status_returns_loopback_shape_without_json_requirement() -> None:
    def status(number: int) -> dict[str, object]:
        return {
            "state": "working",
            "session_url": f"https://app.devin.ai/sessions/{number}",
            "pr_url": None,
        }

    with loopback_client(make_app(FakeTracker(), status=status)) as client:
        response = client.get("/api/requests/42")

    assert response.status_code == 200
    assert response.json() == {
        "number": 42,
        "dispatch": True,
        "state": "working",
        "session_url": "https://app.devin.ai/sessions/42",
        "pr_url": None,
    }


def test_request_status_falls_back_to_queued_when_dispatch_is_unavailable() -> None:
    with loopback_client(make_app(FakeTracker())) as client:
        response = client.get("/api/requests/42")

    assert response.status_code == 200
    assert response.json() == {
        "number": 42,
        "dispatch": False,
        "state": "queued",
        "session_url": None,
        "pr_url": None,
    }


@pytest.mark.parametrize(
    ("path", "headers", "client_address", "error"),
    [
        ("/api/requests/42", {"Host": "evil.example:8770"}, ("127.0.0.1", 10000), "unexpected host"),
        ("/api/requests/42", {}, ("192.0.2.10", 10000), "loopback only"),
    ],
)
def test_request_status_rejects_non_loopback_client_or_host(
    path: str, headers: dict[str, str], client_address: tuple[str, int], error: str
) -> None:
    app = make_app(FakeTracker(), status=lambda _: {"state": "queued", "session_url": None, "pr_url": None})
    with loopback_client(app, client=client_address) as client:
        response = client.get(path, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error": error}


def test_request_status_validation_disabled_and_token_requirements() -> None:
    with loopback_client(make_app()) as client:
        assert client.get("/api/requests/invalid").status_code == 422
        assert client.get("/api/requests/0").status_code == 422
        disabled = client.get("/api/requests/1")
        assert disabled.status_code == 409
        assert disabled.json() == {"error": "request reporting is disabled"}

    app = make_app(FakeTracker(), token="secret", status=lambda _: {"state": "queued"})
    with loopback_client(app) as client:
        assert client.get("/api/requests/1").status_code == 401
        response = client.get("/api/requests/1", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.json() == {
        "number": 1,
        "dispatch": True,
        "state": "queued",
    }


def _assert_rejected_without_tracker_calls(
    tracker: FakeTracker,
    *,
    headers: dict[str, str],
    status_code: int,
    error: str,
    path: str = "/api/requests",
    token: str | None = None,
) -> None:
    with loopback_client(make_app(tracker, token=token)) as client:
        response = client.post(path, content=json.dumps(payload()).encode("utf-8"), headers=headers)
    assert response.status_code == status_code
    assert response.json() == {"error": error}
    assert tracker.calls == []
    assert tracker.mutations == []


@pytest.mark.parametrize(
    ("headers", "status_code", "error"),
    [
        (
            {"Origin": "https://untrusted.example", "Content-Type": "text/plain"},
            403,
            "cross-origin request rejected",
        ),
        ({"Origin": "null", "Content-Type": "application/json"}, 403, "cross-origin request rejected"),
        (
            {"Origin": "https://127.0.0.1:8770", "Content-Type": "application/json"},
            403,
            "cross-origin request rejected",
        ),
        (
            {"Origin": "http://127.0.0.1:8771", "Content-Type": "application/json"},
            403,
            "cross-origin request rejected",
        ),
        ({"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}, 403, "cross-origin request rejected"),
        ({"Host": "evil.example:8770", "Content-Type": "application/json"}, 403, "unexpected host"),
        ({"Host": "", "Content-Type": "application/json"}, 403, "unexpected host"),
        ({"Content-Type": "text/plain"}, 415, "content type must be application/json"),
        ({}, 415, "content type must be application/json"),
    ],
)
def test_request_bridge_rejects_untrusted_headers_without_tracker_calls(
    headers: dict[str, str], status_code: int, error: str
) -> None:
    _assert_rejected_without_tracker_calls(
        FakeTracker(),
        headers=headers,
        status_code=status_code,
        error=error,
    )


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({"Origin": "https://untrusted.example"}, "cross-origin request rejected"),
        ({"Origin": "null"}, "cross-origin request rejected"),
        ({"Origin": "https://127.0.0.1:8770"}, "cross-origin request rejected"),
        ({"Origin": "http://127.0.0.1:8771"}, "cross-origin request rejected"),
        ({"Host": "evil.example:8770"}, "unexpected host"),
        ({"Host": ""}, "unexpected host"),
        ({"Sec-Fetch-Site": "cross-site"}, "cross-origin request rejected"),
    ],
)
def test_fixing_rejects_untrusted_headers_without_tracker_calls(headers: dict[str, str], error: str) -> None:
    _assert_rejected_without_tracker_calls(
        FakeTracker(),
        headers=headers,
        status_code=403,
        error=error,
        path="/api/requests/1/fixing",
    )


def test_correct_bearer_token_does_not_allow_cross_origin_request() -> None:
    _assert_rejected_without_tracker_calls(
        FakeTracker(),
        headers={
            "Authorization": "Bearer secret",
            "Origin": "https://untrusted.example",
            "Content-Type": "application/json",
        },
        status_code=403,
        error="cross-origin request rejected",
        token="secret",
    )


def test_valid_json_requests_allow_non_browser_and_same_origin_clients() -> None:
    tracker = FakeTracker()
    with loopback_client(make_app(tracker)) as client:
        no_origin = client.post("/api/requests", json=payload())
        charset = client.post(
            "/api/requests",
            content=json.dumps(payload(id="charset")),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        same_origin = client.post(
            "/api/requests",
            json=payload(id="same-origin"),
            headers={"Host": "127.0.0.1:8770", "Origin": "http://127.0.0.1:8770"},
        )
    assert no_origin.status_code == 200
    assert no_origin.json()["issue"]["number"] == 1
    assert charset.status_code == 200
    assert charset.json()["issue"]["number"] == 2
    assert same_origin.status_code == 200
    assert same_origin.json()["issue"]["number"] == 3


@pytest.mark.parametrize("host", ["localhost:8770", "[::1]:8770"])
def test_allowed_host_variants_can_file_requests(host: str) -> None:
    tracker = FakeTracker()
    with loopback_client(make_app(tracker)) as client:
        response = client.post("/api/requests", json=payload(), headers={"Host": host})
    assert response.status_code == 200
    assert response.json()["issue"]["number"] == 1


def test_validation_rejects_extra_blank_and_bad_ids() -> None:
    with loopback_client(make_app()) as client:
        assert client.post("/api/requests", json=payload(extra="x")).status_code == 422
        assert client.post("/api/requests", json=payload(id="invalid id")).status_code == 422
        assert client.post("/api/requests", json=payload(request="  ")).status_code == 422
        assert client.post("/api/requests", json=payload(created_at="2025-01-01T00:00:00")).status_code == 422


def test_duplicate_requests_are_idempotent_and_concurrent() -> None:
    tracker = FakeTracker()
    app = make_app(tracker)
    body = payload()
    with loopback_client(app) as client:
        assert client.post("/api/requests", json=body).json()["issue"]["number"] == 1
        assert client.post("/api/requests", json=body).json()["issue"]["number"] == 1
    assert len(tracker.creates) == 1
    assert len(tracker.issues) == 1

    concurrent_tracker = FakeTracker()
    concurrent_app = make_app(concurrent_tracker)

    async def concurrent_posts() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=concurrent_app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await asyncio.gather(*(client.post("/api/requests", json=payload()) for _ in range(8)))

    results = asyncio.run(concurrent_posts())
    assert all(response.status_code == 200 for response in results)
    assert len(concurrent_tracker.creates) == 1


def test_null_or_mismatched_repository_and_daily_cap_return_null() -> None:
    tracker = FakeTracker()
    with loopback_client(make_app(tracker)) as client:
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
    with loopback_client(app) as client:
        assert client.post("/api/requests", json=payload()).json() == {"issue": None}


def test_success_body_is_scrubbed() -> None:
    tracker = FakeTracker()
    app = make_app(tracker)
    secret = "Authorization: Bearer abcdefghijk123"
    request = payload(request=f"Please review {secret}")
    with loopback_client(app) as client:
        response = client.post("/api/requests", json=request)
    assert response.status_code == 200
    assert response.json()["issue"]["number"] == 1
    body = tracker.creates[0][1]
    assert "<secret>" in body
    assert secret not in body


def test_body_limit_uses_content_length_and_actual_stream() -> None:
    app = make_app()
    with loopback_client(app) as client:
        oversized = client.post(
            "/api/requests",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        assert oversized.status_code == 413
        assert oversized.json() == {"error": "request body too large"}

    async def post_unbounded_stream() -> httpx.Response:
        async def body():
            yield b"{" + b" " * (MAX_REQUEST_BODY_BYTES // 2)
            yield b" " * (MAX_REQUEST_BODY_BYTES // 2) + b"}"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await client.post(
                "/api/requests",
                content=body(),
                headers={"Content-Type": "application/json"},
            )

    response = asyncio.run(post_unbounded_stream())
    assert response.status_code == 413
    assert response.json() == {"error": "request body too large"}


def test_tracker_failure_is_scrub_safe_and_fixing_contract(tmp_path) -> None:
    tracker = FakeTracker()
    tracker.fail_create = True
    log = EventLog(tmp_path / "events")
    configure(log)
    try:
        with loopback_client(make_app(tracker)) as client:
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
    with loopback_client(make_app()) as client:
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
    with loopback_client(make_app(tracker)) as client:
        assert client.post("/api/requests/1/fixing").json() == {"ok": True}
    assert "add_labels" in tracker.mutations and "remove_label" in tracker.mutations
