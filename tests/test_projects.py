# mypy: disable-error-code="no-untyped-def"

import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Callable

import httpx
import pytest
from conftest import FakeAgent, FakeTracker, example_manifest

from garage import cli
from garage.ui import UiProject, create_app
from garage.store import UNASSIGNED, Store
from garage.report import READY, RequestLogger
from garage.tracker import IssueRecord
from garage_sdk.api import ClientKind, SubmitWork
from garage.dispatch import Dispatcher
from garage.projects import only, project_info, load_projects, select_projects
from garage.requests import BridgeProject, RequestBridge
from garage_sdk.events import EventLog
from garage.incidents.registry import build_detector


def write_projects(tmp_path: Path) -> Path:
    path = tmp_path / "projects.toml"
    path.write_text(
        f"""
[[project]]
name = "garage"
repo = "sesh11/garage"
path = "{tmp_path / "garage-checkout"}"
agent = "claude-code"
base_branch = "develop"
check = ["uv run pytest -q"]
prompt_rules = ["Keep garage-sdk changes additive."]

[[project]]
name = "ambient-home"
manifest = "{example_manifest()}"
path = "ambient-checkout"
"""
    )
    return path


def test_projects_file_loads_each_project_with_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GARAGE_REPO", "someone/else")
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path / "data"))
    garage, ambient = load_projects(write_projects(tmp_path))

    assert (garage.project, garage.repo) == ("garage", "sesh11/garage")
    assert garage.manifest.dispatch.agent == "claude-code"
    assert garage.manifest.dispatch.base_branch == "develop"
    assert garage.manifest.build.check == ["uv run pytest -q"]
    assert garage.manifest.dispatch.prompt_rules == ["Keep garage-sdk changes additive."]
    assert garage.local_repo_path == tmp_path / "garage-checkout"
    assert garage.events_dir is None

    assert (ambient.project, ambient.repo) == ("ambient-home", "sesh11/ambient-home")
    assert ambient.manifest.dispatch.outpost == "my-mac"
    assert ambient.local_repo_path == tmp_path / "ambient-checkout"
    assert ambient.rules_path == example_manifest().parent / "rules.toml"
    assert garage.dispatch_ledger != ambient.dispatch_ledger
    assert ambient.dispatch_ledger == tmp_path / "data" / "projects" / "ambient-home" / "dispatch.json"


def test_projects_file_uses_garage_toml_inside_the_checkout(tmp_path) -> None:
    checkout = tmp_path / "service"
    checkout.mkdir()
    (checkout / "garage.toml").write_text(example_manifest().read_text())
    (checkout / "rules.toml").write_text((example_manifest().parent / "rules.toml").read_text())
    path = tmp_path / "projects.toml"
    path.write_text('[[project]]\nname = "home"\npath = "service"\n')
    (project,) = load_projects(path)
    assert (project.project, project.repo, project.rules_path) == (
        "home",
        "sesh11/ambient-home",
        checkout / "rules.toml",
    )


@pytest.mark.parametrize(
    "body",
    [
        '[[project]]\nname = "a"\nrepo = "o/a"\n[[project]]\nname = "a"\nrepo = "o/b"\n',
        '[[project]]\nname = "a"\nrepo = "o/a"\n[[project]]\nname = "b"\nrepo = "O/A"\n',
        '[[project]]\nname = "Bad Name"\nrepo = "o/a"\n',
        '[[project]]\nname = "a"\n',
        '[[project]]\nname = "a"\nrepo = "o/a"\nagent = "robot"\n',
        '[[project]]\nname = "a"\nrepo = "o/a"\ntypo = 1\n',
    ],
)
def test_invalid_projects_files_are_rejected(tmp_path, body) -> None:
    path = tmp_path / "projects.toml"
    path.write_text(body)
    with pytest.raises(ValueError):
        load_projects(path)


def test_single_manifest_is_a_one_project_setup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path))
    (project,) = load_projects(example_manifest())
    assert project.project == "ambient-home"
    assert project.dispatch_ledger == tmp_path / "dispatch.json"
    assert select_projects(manifest=example_manifest()) is None
    monkeypatch.setenv("GARAGE_PROJECTS", str(write_projects(tmp_path)))
    assert select_projects(manifest=example_manifest()) is None
    assert [p.project for p in select_projects() or []] == ["garage", "ambient-home"]
    with pytest.raises(ValueError, match="known projects: garage, ambient-home"):
        only(select_projects() or [], "nope")


def test_example_projects_file_lists_garage_and_ambient_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = load_projects(Path(__file__).parents[1] / "examples" / "projects.toml")
    assert [(p.project, p.repo) for p in projects] == [
        ("garage", "sesh11/garage"),
        ("ambient-home", "sesh11/ambient-home"),
    ]


def test_store_upgrades_version_one_and_tags_items(tmp_path) -> None:
    path = tmp_path / "garage.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE clients (name TEXT PRIMARY KEY, kind TEXT NOT NULL, token_hash TEXT NOT NULL,
                default_repo TEXT, created_at TEXT NOT NULL);
            CREATE TABLE items (id TEXT PRIMARY KEY, submit_id TEXT NOT NULL, submitted_by TEXT NOT NULL,
                trust TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL,
                repos TEXT NOT NULL, source TEXT, context TEXT NOT NULL, issue TEXT, run TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, issue_number INTEGER,
                UNIQUE(submitted_by, submit_id));
            CREATE TABLE audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, client TEXT NOT NULL,
                action TEXT NOT NULL, item_id TEXT NOT NULL, detail TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "item_1",
                "s1",
                "app",
                "app",
                "ask",
                "old",
                "new",
                '[{"full_name": "sesh11/ambient-home"}]',
                None,
                "{}",
                None,
                None,
                now,
                now,
                None,
            ),
        )
        db.execute("INSERT INTO clients VALUES ('old','app','x',NULL,?)", (now,))
    store = Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.items()[0].project is None
    assert store.tag_projects({"sesh11/ambient-home": "ambient-home"}) == 1
    assert store.items(project="ambient-home")[0].text == "old"
    assert store.items(project=UNASSIGNED) == []
    store.add_client("new", ClientKind.app, project="garage")
    assert {c["name"]: c["default_project"] for c in store.clients()} == {"new": "garage", "old": None}


def two_project_app(tmp_path: Path):
    store = Store(tmp_path / "garage.db")
    trackers = {"garage": FakeTracker("sesh11/garage"), "ambient-home": FakeTracker("sesh11/ambient-home")}
    status_calls: list[tuple[str, int]] = []

    def status(name: str) -> Callable[[int], dict[str, object]]:
        def lookup(number: int) -> dict[str, object]:
            status_calls.append((name, number))
            return {"state": "working", "session_url": f"https://runs/{name}/{number}", "pr_url": None}

        return lookup

    bridge = RequestBridge(
        projects=[
            BridgeProject(name, tracker.repo, RequestLogger(tracker), status=status(name))
            for name, tracker in trackers.items()
        ]
    )
    settings = {p.project: p for p in load_projects(write_projects(tmp_path))}
    now = datetime.now(timezone.utc)
    events = EventLog(tmp_path / "ambient-events", clock=lambda: now)
    for _ in range(3):
        events.emit("mic.check", "app", verdict="silent")
    detector = build_detector("rules", example_manifest().parent / "rules.toml")
    projects = [
        UiProject("garage", None, None, info=project_info(settings["garage"])),
        UiProject("ambient-home", events, detector, info=project_info(settings["ambient-home"])),
    ]
    app = create_app(store=store, bridge=bridge, projects=projects)
    return app, store, trackers, status_calls


async def test_two_projects_file_tag_and_filter_items(tmp_path) -> None:
    app, store, trackers, status_calls = two_project_app(tmp_path)
    owner = store.add_client("owner", ClientKind.owner)
    voice = store.add_client("voice", ClientKind.app, project="ambient-home")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        client.headers["Authorization"] = f"Bearer {owner}"
        projects = (await client.get("/v1/projects")).json()["projects"]
        assert [(p["name"], p["repos"][0]["full_name"]) for p in projects] == [
            ("garage", "sesh11/garage"),
            ("ambient-home", "sesh11/ambient-home"),
        ]

        garage = (
            await client.post("/v1/items", json={"id": "g1", "text": "Add projects", "project": "garage"})
        ).json()
        assert (garage["project"], garage["repos"][0]["full_name"], garage["state"]) == (
            "garage",
            "sesh11/garage",
            "approved",
        )
        by_repo = (
            await client.post("/v1/items", json={"id": "a1", "text": "Fix mic", "repo": "sesh11/ambient-home"})
        ).json()
        assert by_repo["project"] == "ambient-home"
        assert len(trackers["garage"].creates) == len(trackers["ambient-home"].creates) == 1

        unknown = await client.post("/v1/items", json={"id": "x1", "text": "?", "project": "nope"})
        assert unknown.status_code == 422
        mismatch = await client.post("/v1/items", json={"id": "x2", "text": "?", "project": "garage", "repo": "o/r"})
        assert mismatch.status_code == 422
        stray = await client.post("/v1/items", json={"id": "x3", "text": "?", "repo": "someone/else"})
        assert (stray.status_code, stray.json()["error"]) == (422, "repo someone/else is not in any project")

        waiting = (await client.post("/v1/items", json={"id": "w1", "text": "Which repo?"})).json()
        assert (waiting["project"], waiting["state"]) == (None, "needs_repo")

        def names(response: httpx.Response) -> list[str]:
            return [item["text"] for item in response.json()["items"]]

        assert names(await client.get("/v1/items", params={"project": "garage"})) == ["Add projects"]
        assert names(await client.get("/v1/items", params={"project": "ambient-home"})) == ["Fix mic"]
        assert names(await client.get("/v1/items", params={"project": UNASSIGNED})) == ["Which repo?"]
        assert (await client.get("/v1/items", params={"project": "Bad Name"})).status_code == 422

        moved = (await client.post(f"/v1/items/{waiting['id']}/project", json={"project": "garage"})).json()
        assert (moved["project"], moved["state"], moved["issue"]["number"]) == ("garage", "approved", 2)
        assert len(trackers["garage"].creates) == 2

        detail = (await client.get(f"/v1/items/{by_repo['id']}")).json()
        assert detail["run"]["session_url"] == "https://runs/ambient-home/1"
        assert status_calls[-1] == ("ambient-home", 1)

        client.headers["Authorization"] = f"Bearer {voice}"
        defaulted = (await client.post("/v1/items", json={"id": "v1", "text": "Lights flicker"})).json()
        assert defaulted["project"] == "ambient-home"
        assert (
            await client.post(f"/v1/items/{defaulted['id']}/project", json={"project": "garage"})
        ).status_code == 403

        del client.headers["Authorization"]
        dashboard = (await client.get("/api/items", params={"project": "ambient-home"})).json()["items"]
        assert [item["text"] for item in dashboard] == ["Lights flicker", "Fix mic"]
        assert (await client.get("/api/items", headers={"Origin": "http://evil"})).status_code == 403
        incidents = (await client.get("/api/incidents")).json()["incidents"]
        assert incidents and {incident["project"] for incident in incidents} == {"ambient-home"}
        assert (await client.get("/api/incidents", params={"project": "garage"})).json()["incidents"] == []
        assert (await client.get("/api/incidents", params={"project": "nope"})).status_code == 404
        redirect = await client.get("/", params={"project": "garage"})
        assert redirect.headers["location"] == "/incidents?project=garage"
        assert [p["name"] for p in (await client.get("/api/projects")).json()["projects"]] == [
            "garage",
            "ambient-home",
        ]

        legacy = await client.post(
            "/api/requests",
            json={"id": "l1", "request": "Legacy ask", "created_at": "2026-01-01T00:00:00Z", "project": "garage"},
        )
        assert legacy.json()["issue"]["number"] == 3
        assert (await client.get("/api/requests/3")).json()["session_url"] == "https://runs/garage/3"
        legacy_items = [i for i in store.items(project="garage") if i.submitted_by == "legacy-bridge"]
        assert [i.text for i in legacy_items] == ["Legacy ask"]


async def test_bridge_does_not_file_for_an_unknown_repository(tmp_path) -> None:
    tracker = FakeTracker("sesh11/garage")
    bridge = RequestBridge(projects=[BridgeProject("garage", tracker.repo, RequestLogger(tracker))])
    from garage.requests import Request

    created = datetime.now(timezone.utc)
    assert await bridge.file(Request(id="1", request="x", created_at=created, repository="o/other")) is None
    assert await bridge.file(Request(id="2", request="x", created_at=created)) is None
    assert await bridge.file(Request(id="3", request="x", created_at=created, project="garage")) is not None
    assert tracker.creates and not tracker.mutations[1:]


async def test_bridge_reports_only_truly_unrouted_requests(tmp_path) -> None:
    from garage_sdk import configure
    from garage.requests import Request

    bridge = RequestBridge(
        projects=[
            BridgeProject("garage", "sesh11/garage", None),
            BridgeProject("ambient-home", "sesh11/ambient-home", None),
        ]
    )
    log = EventLog(tmp_path / "events")
    configure(log)
    try:
        created = datetime.now(timezone.utc)
        assert await bridge.file(Request(id="1", request="x", created_at=created, project="garage")) is None
        assert await bridge.file(Request(id="2", request="x", created_at=created, repository="o/other")) is None
        events = log.read(created.replace(year=2000))
        assert [event.data["request_id"] for event in events if event.type == "factory.request_unrouted"] == ["2"]
    finally:
        configure(None)


async def test_dispatcher_tags_runs_with_their_project(tmp_path) -> None:
    tracker = FakeTracker("sesh11/garage")
    tracker.issues.append(
        IssueRecord(
            number=7,
            url="https://github.com/sesh11/garage/issues/7",
            state="open",
            title="Add projects",
            body="Body",
            labels=[READY],
            created_at=datetime.now(timezone.utc),
        )
    )
    agent = FakeAgent()
    (garage, _) = load_projects(write_projects(tmp_path))
    dispatcher = Dispatcher(
        tracker,
        agent,
        tmp_path / "dispatch.json",
        manifest=garage.manifest,
        interval_s=60,
        dry_run=False,
        project="garage",
    )
    await dispatcher.poll_once()
    prompt, details = agent.created[0]
    assert "project:garage" in details["tags"]  # type: ignore[operator]
    assert details["repos"] == ["sesh11/garage"]
    assert "Keep garage-sdk changes additive." in prompt
    assert json.loads((tmp_path / "dispatch.json").read_text())["records"][0]["project"] == "garage"


def test_cli_lists_projects_and_filters_items(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path / "data"))
    projects = write_projects(tmp_path)
    assert cli.main(["projects", "--projects", str(projects)]) == 0
    listing = capsys.readouterr().out
    assert "garage  repo=sesh11/garage  agent=claude-code  base=develop" in listing
    assert "ambient-home  repo=sesh11/ambient-home  agent=devin  base=main" in listing

    store = Store(tmp_path / "data" / "garage.db")
    store.submit("me", ClientKind.owner, SubmitWork(id="1", text="garage work"), "sesh11/garage", "garage")
    store.submit("me", ClientKind.owner, SubmitWork(id="2", text="home work"), "sesh11/ambient-home", "ambient-home")
    waiting, _ = store.submit("me", ClientKind.owner, SubmitWork(id="3", text="loose"), None)
    monkeypatch.setenv("GARAGE_PROJECTS", str(projects))
    assert cli.main(["items", "--project", "garage"]) == 0
    out = capsys.readouterr().out
    assert "[garage] me: garage work" in out and "home work" not in out
    assert cli.main(["items", "--unassigned"]) == 0
    assert "[-] me: loose" in capsys.readouterr().out
    assert cli.main(["items", "--project", "nope"]) == 1

    monkeypatch.delenv("GARAGE_REPORT", raising=False)
    assert cli.main(["assign", waiting.id, "ambient-home"]) == 0
    assert "now in project ambient-home" in capsys.readouterr().out
    moved = store.get(waiting.id)
    assert moved is not None and (moved.project, moved.state) == ("ambient-home", "new")


def test_cli_report_and_dispatch_preview_every_project(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "GitHubIssues", lambda *a, **k: (_ for _ in ()).throw(AssertionError("offline")))
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path / "data"))
    projects = write_projects(tmp_path)
    assert cli.main(["report", "--offline", "--projects", str(projects)]) == 0
    out = capsys.readouterr().out
    assert "== project garage (sesh11/garage) ==" in out
    assert "no [signals].path for garage; skipped" in out
    assert "== project ambient-home (sesh11/ambient-home) ==" in out
    assert cli.main(["dispatch", "--offline", "--projects", str(projects), "--project", "garage"]) == 0
    out = capsys.readouterr().out
    assert "[dry run] nothing to dispatch for sesh11/garage" in out and "ambient-home" not in out


def test_serve_runs_every_project_in_one_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GARAGE_REPORT", "1")
    monkeypatch.setenv("GARAGE_GITHUB_TOKEN", "test-token")
    projects = load_projects(write_projects(tmp_path))
    app = SimpleNamespace(router=SimpleNamespace(on_startup=[], on_shutdown=[]), state=SimpleNamespace())
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "configure", lambda _: None)
    monkeypatch.setattr(cli, "build_dispatcher", lambda settings: None)
    monkeypatch.setattr(cli, "create_app", lambda **kwargs: captured.update(kwargs) or app)
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)
    assert cli._serve(projects, "127.0.0.1", 8770, require_signals=False) == 0

    bridge = captured["bridge"]
    assert isinstance(bridge, RequestBridge)
    assert [(p.name, p.repo) for p in bridge.projects] == [
        ("garage", "sesh11/garage"),
        ("ambient-home", "sesh11/ambient-home"),
    ]
    assert all(p.logger is not None for p in bridge.projects)
    ui = captured["projects"]
    assert [(p.name, p.event_log is not None) for p in ui] == [("garage", False), ("ambient-home", True)]  # type: ignore[attr-defined]
    assert len(app.state.monitors) == 1
