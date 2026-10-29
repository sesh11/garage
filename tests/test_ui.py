# mypy: disable-error-code="no-untyped-def"

from fastapi.testclient import TestClient

from garage.ui import create_app
from garage_sdk.events import EventLog
from garage.incidents.base import Incident


class DetectorStub:
    name = "stub"

    async def detect(self, events, now):
        return [
            Incident(
                fingerprint="error:abc",
                rule="error",
                title="Runtime error",
                severity="high",
                count=1,
                threshold=1,
                window_s=60,
                first_seen=now,
                last_seen=now,
                ready=True,
                group={},
                samples=[],
                detector=self.name,
            )
        ]


def test_incidents_ui_routes_and_redirect(tmp_path) -> None:
    event_log = EventLog(tmp_path / "source")
    event_log.emit("test", "app")
    app = create_app(event_log=event_log, detector=DetectorStub())
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/incidents"
        page = client.get("/incidents")
        assert page.status_code == 200
        assert "garage" in page.text
        assert "Job board" not in page.text and "Cost tracking" not in page.text
        assert client.get("/api/health").json() == {"ok": True, "detector": "stub", "reporting": False}
        events = client.get("/api/events")
        assert events.status_code == 200
        assert events.json()["count"] == 1
        incidents = client.get("/api/incidents").json()
        assert incidents["detector"] == "stub"
        assert incidents["incidents"][0]["fingerprint"] == "error:abc"


def test_api_rejects_invalid_duration(tmp_path) -> None:
    app = create_app(event_log=EventLog(tmp_path / "events"), detector=None)
    with TestClient(app) as client:
        assert client.get("/api/incidents?since=bogus").status_code == 400
        assert client.get("/api/events?since=bogus").status_code == 400
