"""Planner: any feature text in, a validated task graph out, on the configured agent."""

import sys
import json
from pathlib import Path
from dataclasses import replace

import pytest
from conftest import FakeAgent, example_manifest

from garage import cli
from garage.planner import (
    PLAN_SCHEMA,
    Planner,
    PlanError,
    TaskGraph,
    parse_plan,
    render_plan,
    codex_runner,
    devin_runner,
    build_planner,
    claude_runner,
    build_plan_prompt,
)
from garage.projects import load_projects
from garage.settings import settings_from_env
from garage.agents.base import SessionState


GRAPH: dict[str, object] = {
    "summary": "Add a planner in two steps.",
    "tasks": [
        {"id": "t2", "title": "Add the CLI", "body": "Wire `garage plan`.", "depends_on": ["t1"]},
        {"id": "t1", "title": "Add the module", "body": "Create planner.py.\n\nTests too.", "depends_on": []},
        {"id": "t3", "title": "Document it", "body": "README.", "depends_on": []},
    ],
}


def test_graph_orders_tasks_after_their_dependencies() -> None:
    graph = TaskGraph.model_validate(GRAPH)
    assert [task.id for task in graph.order()] == ["t1", "t2", "t3"]


@pytest.mark.parametrize(
    ("tasks", "error"),
    [
        ([], "at least 1"),
        ([{"id": "a", "title": "A", "body": ""}, {"id": "a", "title": "B", "body": ""}], "duplicate task ids: a"),
        ([{"id": "a", "title": "A", "body": "", "depends_on": ["a"]}], "depends on itself"),
        ([{"id": "a", "title": "A", "body": "", "depends_on": ["zz"]}], "unknown task(s): zz"),
        (
            [
                {"id": "a", "title": "A", "body": "", "depends_on": ["b"]},
                {"id": "b", "title": "B", "body": "", "depends_on": ["a"]},
            ],
            "cycle",
        ),
        ([{"id": " ", "title": "A", "body": ""}], "must not be blank"),
    ],
)
def test_invalid_graphs_are_rejected(tasks: list[dict[str, object]], error: str) -> None:
    with pytest.raises(PlanError, match="invalid task graph") as raised:
        parse_plan({"summary": "s", "tasks": tasks})
    assert error in str(raised.value)


def test_parse_plan_accepts_json_text_and_rejects_nothing() -> None:
    assert parse_plan(json.dumps(GRAPH)).tasks[0].id == "t2"
    with pytest.raises(PlanError, match="no structured result"):
        parse_plan(None)
    with pytest.raises(PlanError, match="not JSON"):
        parse_plan("sorry, I can't")


def test_prompt_keeps_any_text_and_forbids_changes() -> None:
    text = "voice note: uh, so I want a planner\n\n- bullets\n# heading"
    prompt = build_plan_prompt(text, repo="sesh11/garage", product_brief="Garage builds PRs.")
    assert text in prompt
    assert "sesh11/garage" in prompt and "Garage builds PRs." in prompt
    assert "Do not edit files" in prompt


async def test_planner_runs_the_agent_with_the_schema() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    async def run(prompt: str, schema: dict[str, object]) -> object:
        seen.append((prompt, schema))
        return GRAPH

    graph = await Planner(run, repo="sesh11/garage").plan("Add a planner")
    assert len(graph.tasks) == 3
    assert seen[0][1] is PLAN_SCHEMA
    with pytest.raises(ValueError, match="must not be empty"):
        await Planner(run, repo="sesh11/garage").plan("  \n")


def test_render_plan_lists_build_order_and_dependencies() -> None:
    text = render_plan(TaskGraph.model_validate(GRAPH))
    assert text.index("[t1]") < text.index("[t2]") < text.index("[t3]")
    assert "   Depends on: t1" in text
    assert "   Tests too." in text


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake-planner"
    path.write_text(
        f"#!{sys.executable}\n"
        + f"GRAPH = {GRAPH!r}\n"
        + """
import os, sys, json
from pathlib import Path
args = sys.argv[1:]
Path(os.environ['FAKE_ARGS']).write_text(json.dumps({'args': args, 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}))
mode = os.getenv('FAKE_MODE', 'ok')
if mode == 'hang':
    import time
    time.sleep(30)
if mode == 'nonzero':
    print('auth: please log in', file=sys.stderr)
    sys.exit(3)
if args[0] == 'exec':
    Path(args[args.index('-o') + 1]).write_text(json.dumps(GRAPH))
else:
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}))
    if mode == 'error':
        print(json.dumps({'type': 'result', 'is_error': True, 'result': 'API Error: 401'}))
    else:
        print(json.dumps({'type': 'result', 'structured_output': GRAPH}))
"""
    )
    path.chmod(0o755)
    return path


async def test_claude_runner_is_read_only(fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(record))
    run = claude_runner(cli_path=str(fake_cli), model="opus", cwd=tmp_path, timeout_s=10)
    long_prompt = "plan it " * 40_000
    graph = parse_plan(await run(long_prompt, PLAN_SCHEMA))
    assert len(graph.tasks) == 3
    recorded = json.loads(record.read_text())
    assert recorded["stdin"] == long_prompt
    args = recorded["args"]
    assert all("plan it" not in arg for arg in args)
    assert args[args.index("--allowedTools") + 1] == "Read,Glob,Grep"
    assert set(args[args.index("--disallowedTools") + 1].split(",")) >= {"Bash", "Edit", "Write"}
    assert "--strict-mcp-config" in args
    assert args[args.index("--model") + 1] == "opus"
    assert json.loads(args[args.index("--json-schema") + 1]) == PLAN_SCHEMA
    assert json.loads(record.read_text())["cwd"] == str(tmp_path)


async def test_codex_runner_uses_a_read_only_sandbox(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(record))
    run = codex_runner(cli_path=str(fake_cli), model=None, cwd=tmp_path, timeout_s=10)
    assert len(parse_plan(await run("plan it", PLAN_SCHEMA)).tasks) == 3
    args = json.loads(record.read_text())["args"]
    assert args[args.index("-s") + 1] == "read-only"
    assert "--model" not in args
    assert args[-1] == "-"
    assert json.loads(record.read_text())["stdin"] == "plan it"


@pytest.mark.parametrize(("mode", "error"), [("nonzero", "exited with 3: auth: please log in"), ("error", "401")])
async def test_cli_failures_raise_plan_errors(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, error: str
) -> None:
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_MODE", mode)
    run = claude_runner(cli_path=str(fake_cli), model=None, cwd=tmp_path, timeout_s=10)
    with pytest.raises(PlanError, match=error):
        await run("plan it", PLAN_SCHEMA)


async def test_slow_cli_times_out(fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_MODE", "hang")
    run = claude_runner(cli_path=str(fake_cli), model=None, cwd=tmp_path, timeout_s=0.5)
    with pytest.raises(PlanError, match="timed out"):
        await run("plan it", PLAN_SCHEMA)


async def test_missing_cli_raises_plan_error(tmp_path: Path) -> None:
    run = claude_runner(cli_path=str(tmp_path / "nope"), model=None, cwd=tmp_path, timeout_s=10)
    with pytest.raises(PlanError, match="could not start"):
        await run("plan it", PLAN_SCHEMA)


class FakeDevin(FakeAgent):
    next: SessionState

    async def get_session(self, session_id: str) -> SessionState:
        state = await super().get_session(session_id)
        self.states[session_id] = self.next
        return state


async def test_devin_runner_waits_for_the_finished_plan() -> None:
    agent = FakeDevin()
    agent.next = SessionState(session_id="s1", status="exit", structured_output=GRAPH)
    run = devin_runner(agent, repo="sesh11/garage", tags=["project:garage"], poll_s=0)  # type: ignore[arg-type]
    output = await run(build_plan_prompt("Planner please", repo="sesh11/garage"), PLAN_SCHEMA)
    assert parse_plan(output).summary == GRAPH["summary"]
    _, options = agent.created[0]
    assert options["repos"] == ["sesh11/garage"]
    assert options["title"] == "Plan: Planner please"
    assert options["tags"] == ["garage", "plan", "project:garage"]


async def test_devin_runner_reports_a_session_without_a_plan() -> None:
    agent = FakeDevin()
    agent.next = SessionState(session_id="s1", status="error")
    run = devin_runner(agent, repo="sesh11/garage", poll_s=0)  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="without a plan"):
        await run("plan", PLAN_SCHEMA)
    agent.next = SessionState(session_id="s1", status="running", status_detail="waiting_for_user")
    with pytest.raises(PlanError, match="waiting for input"):
        await run("plan", PLAN_SCHEMA)


def _manifest(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "garage.toml"
    path.write_text(example_manifest().read_text() + extra)
    return path


def test_default_planner_is_claude_code_on_opus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARAGE_PLANNER_AGENT", raising=False)
    monkeypatch.delenv("GARAGE_PLANNER_MODEL", raising=False)
    settings = settings_from_env(_manifest(tmp_path))
    assert (settings.planner_agent, settings.planner_model) == ("claude-code", "opus")


def test_manifest_and_env_choose_the_planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARAGE_PLANNER_AGENT", raising=False)
    monkeypatch.delenv("GARAGE_PLANNER_MODEL", raising=False)
    manifest = _manifest(tmp_path, '\n[planner]\nagent = "codex"\n')
    settings = settings_from_env(manifest)
    assert (settings.planner_agent, settings.planner_model) == ("codex", None)
    monkeypatch.setenv("GARAGE_PLANNER_AGENT", "claude-code")
    monkeypatch.setenv("GARAGE_PLANNER_MODEL", "sonnet")
    settings = settings_from_env(manifest)
    assert (settings.planner_agent, settings.planner_model) == ("claude-code", "sonnet")
    monkeypatch.setenv("GARAGE_PLANNER_AGENT", "gpt")
    with pytest.raises(ValueError, match="planner agent must be one of"):
        settings_from_env(manifest)


def test_project_entry_wins_over_the_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARAGE_PLANNER_AGENT", raising=False)
    monkeypatch.delenv("GARAGE_PLANNER_MODEL", raising=False)
    _manifest(tmp_path, '\n[planner]\nagent = "claude-code"\nmodel = "sonnet"\n')
    projects = tmp_path / "projects.toml"
    projects.write_text(
        '[[project]]\nname = "a"\nmanifest = "garage.toml"\n[project.planner]\nmodel = "opus"\n\n'
        '[[project]]\nname = "b"\nrepo = "sesh11/b"\n[project.planner]\nagent = "codex"\n\n'
        '[[project]]\nname = "c"\nrepo = "sesh11/c"\n'
    )
    a, b, c = load_projects(projects)
    assert (a.planner_agent, a.planner_model) == ("claude-code", "opus")
    assert (b.planner_agent, b.planner_model) == ("codex", None)
    assert (c.planner_agent, c.planner_model) == ("claude-code", "opus")
    monkeypatch.setenv("GARAGE_PLANNER_AGENT", "codex")
    a, _, _ = load_projects(projects)
    assert (a.planner_agent, a.planner_model) == ("codex", None)
    monkeypatch.delenv("GARAGE_PLANNER_AGENT")
    projects.write_text('[[project]]\nname = "a"\nrepo = "sesh11/a"\n[project.planner]\nagent = "gpt"\n')
    with pytest.raises(ValueError, match="agent must be one of"):
        load_projects(projects)


def test_build_planner_needs_devin_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("DEVIN_ORG_ID", raising=False)
    settings = settings_from_env(_manifest(tmp_path))
    with pytest.raises(ValueError, match="DEVIN_API_KEY"):
        build_planner(settings, agent="devin")
    planner = build_planner(replace(settings, devin_api_key="k", devin_org_id="o"), agent="devin")
    assert planner.repo == settings.repo


def test_plan_command_prints_the_graph(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(record))
    monkeypatch.setenv("GARAGE_CLAUDE_PATH", str(fake_cli))
    monkeypatch.delenv("GARAGE_PLANNER_AGENT", raising=False)
    monkeypatch.delenv("GARAGE_PLANNER_MODEL", raising=False)
    manifest = _manifest(tmp_path)
    assert cli.main(["plan", "--manifest", str(manifest), "Add a planner"]) == 0
    assert "1. [t1] Add the module" in capsys.readouterr().out
    recorded = json.loads(record.read_text())
    assert recorded["args"][recorded["args"].index("--model") + 1] == "opus"
    assert "Add a planner" in recorded["stdin"]

    description = tmp_path / "feature.md"
    description.write_text("# Planner\nAny text")
    assert cli.main(["plan", "--manifest", str(manifest), "--file", str(description), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [task["id"] for task in printed["tasks"]] == ["t1", "t2", "t3"]
    assert "# Planner\nAny text" in json.loads(record.read_text())["stdin"]
