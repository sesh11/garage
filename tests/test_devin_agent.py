"""Devin session-client tests using an offline HTTP transport."""

import json

import httpx
import pytest

from garage.agents.base import SessionStatus
from garage.agents.devin import DevinClient


@pytest.mark.asyncio
async def test_create_session_posts_v3_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"session_id": "session-1", "url": "https://app.devin.ai/sessions/session-1", "status": "new"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        agent = DevinClient("fake-key", "org-test", max_acu=7, http=client)
        created = await agent.create_session(
            "Fix a bug",
            title="Fix #1",
            tags=["factory", "dispatch"],
            structured_output_schema={"type": "object"},
            repos=["owner/repo"],
        )
    payload = json.loads(requests[0].content)
    assert created.session_id == "session-1"
    assert requests[0].url.path == "/v3/organizations/org-test/sessions"
    assert payload["repos"] == ["owner/repo"]
    assert payload["max_acu_limit"] == 7
    assert "fake-key" not in requests[0].url.query.decode()


@pytest.mark.asyncio
async def test_get_session_maps_api_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "running",
                "status_detail": "waiting_for_user",
                "structured_output": {"summary": "done"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await DevinClient("fake-key", "org-test", http=client).get_session("session-1")
    assert state.status == "running"
    assert state.structured_output == {"summary": "done"}


def test_status_mapping() -> None:
    assert DevinClient.map_status("running", "working") is SessionStatus.working
    assert DevinClient.map_status("running", "waiting_for_user") is SessionStatus.blocked
    assert DevinClient.map_status("exit", "finished") is SessionStatus.finished
    assert DevinClient.map_status("error", "internal_error") is SessionStatus.failed
    assert DevinClient.map_status("suspended", "inactivity") is SessionStatus.expired
    assert DevinClient.map_status("unknown", "") is None
