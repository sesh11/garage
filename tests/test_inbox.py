import json
from pathlib import Path

import httpx
import pytest
from conftest import FakeTracker
from pydantic import ValidationError
from test_report import sample_incident

from garage.ui import create_app
from garage.store import Store
from garage.report import READY, REQUEST, RequestLogger, IncidentReporter
from garage.tracker import IssueRecord
from garage_sdk.api import ClientKind, SubmitWork, json_schemas
from garage.requests import RequestBridge
from garage.incidents.base import Incident


def test_schemas_current() -> None:
    root = Path(__file__).parents[1] / "schemas" / "v1"
    assert {p.stem for p in root.glob("*.json")} == set(json_schemas())
    for name, schema in json_schemas().items():
        assert json.loads((root / f"{name}.json").read_text()) == schema


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "x", "text": " "},
        {"id": "!", "text": "hi"},
        {"id": "x", "text": "hi", "repo": "bad"},
        {"id": "x", "text": "hi", "extra": True},
        {"id": "x", "text": "hi", "created_at": "2026-01-01T00:00:00"},
    ],
)
def test_invalid_submit(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SubmitWork.model_validate(payload)


def test_store(tmp_path: Path) -> None:
    store = Store(tmp_path / "garage.db")
    token = store.add_client("me", ClientKind.owner, "Owner/Repo")
    assert token.startswith("gar_")
    assert store.authenticate(token)
    assert store.authenticate("wrong") is None
    assert token not in str(store.clients())
    with store.connection() as db:
        assert token not in str([tuple(row) for row in db.execute("SELECT * FROM clients")])
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    work = SubmitWork(id="one", text="hello", repo="https://github.com/Owner/Repo/")
    assert work.repo == "owner/repo"
    item, created = store.submit("me", ClientKind.owner, work, work.repo)
    assert created
    assert Store(store.path).submit("me", ClientKind.owner, work, work.repo) == (item, False)
    store.remove_client("me")
    assert store.authenticate(token) is None


async def test_inbox_routes(tmp_path: Path) -> None:
    store = Store(tmp_path / "garage.db")
    owner = store.add_client("owner", ClientKind.owner)
    app_token = store.add_client("app", ClientKind.app)
    app = create_app(event_log=None, detector=None, store=store, bridge=RequestBridge(None))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.get("/v1/items")).status_code == 401
        assert (await client.get("/v1/items?state=invalid")).status_code == 401
        assert (await client.get("/v1/unknown")).status_code == 401
        client.headers["Authorization"] = f"Bearer {owner}"
        assert (await client.get("/v1/items")).json() == {"items": [], "next_cursor": None}
        assert (await client.post("/v1/items", json={"id": "x", "text": " "})).status_code == 422
        assert (await client.post("/v1/items", json={"id": "x", "text": "a" * 17000})).status_code == 413
        assert (await client.get("/v1/items", headers={"Origin": "http://evil"})).status_code == 403
        first = await client.post("/v1/items", json={"id": "x", "text": "hello"})
        assert first.status_code == 201
        assert first.json()["state"] == "needs_repo"
        second = await client.post("/v1/items", json={"id": "x", "text": "changed"})
        assert second.status_code == 200
        assert second.json() == first.json()
        item_id = first.json()["id"]
        client.headers["Authorization"] = f"Bearer {app_token}"
        assert (await client.get("/v1/items")).json()["items"] == []
        assert (await client.get(f"/v1/items/{item_id}")).status_code == 404
        assert (await client.post(f"/v1/items/{item_id}/cancel")).status_code == 404
        own = await client.post("/v1/items", json={"id": "x", "text": "app work", "repo": "o/r"})
        assert own.json()["state"] == "new"
        client.headers["Authorization"] = f"Bearer {owner}"
        page = (await client.get("/v1/items?limit=1")).json()
        assert page["next_cursor"]
        assert len((await client.get("/v1/items", params={"cursor": page["next_cursor"]})).json()["items"]) == 1
        assert (await client.get("/v1/items?cursor=!bad")).status_code == 422
        assert (await client.post(f"/v1/items/{item_id}/cancel")).json()["state"] == "cancelled"
        assert (await client.post(f"/v1/items/{item_id}/cancel")).status_code == 409
        legacy = await client.post(
            "/api/requests",
            json={"id": "legacy", "request": "old caller", "created_at": "2026-01-01T00:00:00Z", "repository": "o/r"},
        )
        assert legacy.json() == {"issue": None}
        assert any(item.submitted_by == "legacy-bridge" for item in store.items())


async def test_no_store() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(event_log=None, detector=None), client=("127.0.0.1", 1)),
        base_url="http://localhost",
    ) as client:
        assert (await client.get("/v1/items")).status_code == 503


async def test_filing_retry_and_status(tmp_path: Path) -> None:
    store = Store(tmp_path / "garage.db")
    token = store.add_client("me", ClientKind.owner, "o/r")
    tracker = FakeTracker("o/r")
    tracker.fail_create = True
    status: dict[str, object] = {"state": "working", "session_url": "https://example.test/session"}
    bridge = RequestBridge(RequestLogger(tracker), bridge_token="separate-legacy-token", status=lambda _: status)
    app = create_app(event_log=None, detector=None, store=store, bridge=bridge)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        assert (await client.post("/v1/items", json={"id": "x", "text": "hello"})).status_code == 502
        assert store.items()[0].state == "new"
        tracker.fail_create = False
        retry = await client.post("/v1/items", json={"id": "x", "text": "hello"})
        assert retry.status_code == 200
        assert retry.json()["state"] == "approved"
        assert tracker.issues[0].labels == [REQUEST, READY]
        item_id = retry.json()["id"]
        running = (await client.get(f"/v1/items/{item_id}")).json()
        assert running["state"] == "running"
        assert (await client.get(f"/v1/items/{item_id}")).json() == running
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM audit WHERE action='status'").fetchone()[0] == 1
        status["state"] = "finished"
        finished = (await client.get(f"/v1/items/{item_id}")).json()
        assert finished["state"] == "running"
        assert finished["run"]["state"] == "finished"
        assert (await client.get(f"/v1/items/{item_id}")).json() == finished
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM audit WHERE action='status'").fetchone()[0] == 2
        await client.post("/v1/items", json={"id": "x", "text": "hello"})
        assert len(tracker.creates) == 1
        with store.connection() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit")]
            assert "file_failed" in actions and "file_success" in actions and "state_change" in actions


async def test_incident_callback() -> None:
    tracker = FakeTracker("o/r")
    calls: list[tuple[str, int, bool]] = []

    def on_filed(incident: Incident, issue: IssueRecord, ready: bool) -> None:
        calls.append((incident.fingerprint, issue.number, ready))

    incident = sample_incident()
    reporter = IncidentReporter(tracker, on_filed=on_filed)
    await reporter.sync([incident])
    assert calls == [(incident.fingerprint, 1, True)]
    tracker.issues[0].state = "closed"
    tracker.issues[0].closed_at = incident.first_seen.replace(year=2025)
    await reporter.sync([incident])
    assert calls[-1] == (incident.fingerprint, 1, False)
    dry = IncidentReporter(FakeTracker("o/r"), dry_run=True, on_filed=on_filed)
    await dry.sync([incident])
    assert len(calls) == 2
