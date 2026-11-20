"""Planner: turn any text describing a feature into a graph of tasks Garage can build one PR at a time.

The text has no required format: a sentence, notes, a spec or a transcript all work. A configured agent
(``[planner]`` in the manifest, default Claude Code with Opus) reads the repository without changing it and
returns tasks; each task is one pull request, and ``depends_on`` names the tasks that must land first.
"""

import json
import asyncio
import tempfile
from pathlib import Path
from collections.abc import Callable, Sequence, Awaitable

from pydantic import Field, BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from garage_sdk import scrub_text
from garage.settings import GarageSettings
from garage.agents.base import SessionStatus
from garage.agents.devin import DevinClient


READ_ONLY_TOOLS = "Read,Glob,Grep"
WRITE_TOOLS = "Bash,Edit,MultiEdit,Write,NotebookEdit"

PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "body", "depends_on"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "tasks"],
    "additionalProperties": False,
}


class PlanError(RuntimeError):
    """The planner agent did not return a usable task graph."""


class PlanTask(BaseModel):
    """One task: the work for a single pull request."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id", "title")
    @classmethod
    def _strip(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("depends_on")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value))


class TaskGraph(BaseModel):
    """Tasks and the order constraints between them; always a non-empty acyclic graph."""

    model_config = ConfigDict(extra="ignore")

    summary: str
    tasks: list[PlanTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _graph(self) -> "TaskGraph":
        ids = [task.id for task in self.tasks]
        duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate task ids: {', '.join(duplicates)}")
        known = set(ids)
        for task in self.tasks:
            if task.id in task.depends_on:
                raise ValueError(f"task {task.id} depends on itself")
            unknown = [dependency for dependency in task.depends_on if dependency not in known]
            if unknown:
                raise ValueError(f"task {task.id} depends on unknown task(s): {', '.join(unknown)}")
        if len(self.order()) != len(self.tasks):
            raise ValueError("task dependencies form a cycle")
        return self

    def order(self) -> list[PlanTask]:
        """Return tasks so each comes after its dependencies, keeping the planner's order otherwise."""
        done: set[str] = set()
        ordered: list[PlanTask] = []
        remaining = list(self.tasks)
        while remaining:
            ready = [task for task in remaining if done.issuperset(task.depends_on)]
            if not ready:
                break
            task = ready[0]
            ordered.append(task)
            done.add(task.id)
            remaining.remove(task)
        return ordered


def build_plan_prompt(text: str, *, repo: str, product_brief: str | None = None) -> str:
    """Return the planner agent's instructions for one feature description."""
    brief = f"\nProduct brief:\n{product_brief.strip()}\n" if product_brief and product_brief.strip() else ""
    return (
        f"You are planning work on the GitHub repository {repo} for Garage, which builds one pull request at a time."
        f"\n{brief}\n"
        "Turn the feature description below into a task graph. The description can be in any format (one "
        "sentence, notes, a spec, a transcript); work out what is being asked.\n\n"
        "Rules:\n"
        "- Read the repository to ground the plan in the real code. Do not edit files, run commands that change "
        "anything, create branches, push or open pull requests.\n"
        "- Each task is exactly one reviewable pull request that leaves the project working and tested.\n"
        "- Prefer few, meaningful tasks over many tiny ones; one task is fine for a small feature.\n"
        "- Give each task a short unique id (t1, t2, ...), an imperative title and a body a coding agent can work "
        "from alone: the goal, what to change and where, and acceptance criteria including tests.\n"
        "- depends_on lists the ids of tasks that must be merged first. Add a dependency only when a task "
        "really needs the other's code; independent tasks can be built in parallel. No cycles.\n"
        "- Do not write 'Depends on' lines in bodies; Garage adds them from depends_on.\n"
        "- summary is one or two plain sentences describing the whole plan.\n"
        "Return only the requested structured result.\n\n"
        "Feature description:\n<<<\n"
        f"{text.strip()}\n"
        ">>>\n"
    )


def parse_plan(output: object) -> TaskGraph:
    """Validate an agent's structured result as a task graph."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError as exc:
            raise PlanError(f"planner returned text that is not JSON: {exc}") from exc
    if output is None:
        raise PlanError("planner returned no structured result")
    try:
        return TaskGraph.model_validate(output)
    except ValidationError as exc:
        raise PlanError(f"planner returned an invalid task graph: {exc}") from exc


PlanRunner = Callable[[str, dict[str, object]], Awaitable[object]]
"""Run a prompt on an agent and return its structured result (any JSON value)."""


async def _run_cli(command: list[str], prompt: str, cwd: Path, timeout_s: float) -> str:
    """Run ``command`` with ``prompt`` on stdin, keeping it out of argv's size limit and process listings."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PlanError(f"could not start planner CLI {command[0]!r}: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(prompt.encode()), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise PlanError(f"planner timed out after {timeout_s:g}s") from None
    if process.returncode:
        lines = [line.strip() for line in stderr.decode(errors="replace").splitlines() if line.strip()]
        detail = scrub_text(" | ".join(lines[-3:]))
        raise PlanError(f"planner CLI exited with {process.returncode}" + (f": {detail}" if detail else ""))
    return stdout.decode(errors="replace")


def claude_runner(*, cli_path: str, model: str | None, cwd: Path, timeout_s: float) -> PlanRunner:
    """Run Claude Code headless with read-only tools and a JSON-schema result."""

    async def run(prompt: str, schema: dict[str, object]) -> object:
        command = [
            cli_path,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(schema),
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            READ_ONLY_TOOLS,
            "--disallowedTools",
            WRITE_TOOLS,
            "--strict-mcp-config",
        ]
        if model:
            command += ["--model", model]
        stdout = await _run_cli(command, prompt, cwd, timeout_s)
        for line in reversed(stdout.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                if event.get("is_error") is True:
                    raise PlanError(f"planner failed: {scrub_text(str(event.get('result') or 'error'))}")
                return event.get("structured_output")
        raise PlanError("planner CLI finished without a result")

    return run


def codex_runner(*, cli_path: str, model: str | None, cwd: Path, timeout_s: float) -> PlanRunner:
    """Run Codex headless in a read-only sandbox with an output schema."""

    async def run(prompt: str, schema: dict[str, object]) -> object:
        with tempfile.TemporaryDirectory(prefix="garage-plan-") as scratch:
            schema_path = Path(scratch) / "schema.json"
            result_path = Path(scratch) / "result.json"
            schema_path.write_text(json.dumps(schema))
            command = [
                cli_path,
                "exec",
                "--json",
                "-C",
                str(cwd),
                "-s",
                "read-only",
                "--output-schema",
                str(schema_path),
                "-o",
                str(result_path),
            ]
            if model:
                command += ["--model", model]
            await _run_cli([*command, "-"], prompt, cwd, timeout_s)
            try:
                return result_path.read_text()
            except OSError as exc:
                raise PlanError("planner CLI finished without a result") from exc

    return run


def devin_runner(
    client: DevinClient,
    *,
    repo: str,
    tags: Sequence[str] = (),
    timeout_s: float = 3600,
    poll_s: float = 15,
) -> PlanRunner:
    """Run a Devin session that only reads the repository, and wait for its structured result."""

    async def run(prompt: str, schema: dict[str, object]) -> object:
        first = next((line.strip() for line in prompt.split("<<<", 1)[-1].splitlines() if line.strip()), "feature")
        created = await client.create_session(
            prompt,
            title=f"Plan: {first[:70]}",
            tags=["garage", "plan", *tags],
            structured_output_schema=schema,
            repos=[repo],
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            state = await client.get_session(created.session_id)
            status = client.map_status(state.status, state.status_detail or "")
            if state.structured_output is not None and status != SessionStatus.working:
                return state.structured_output
            if status in (SessionStatus.failed, SessionStatus.expired, SessionStatus.finished):
                raise PlanError(f"Devin planner session {created.url} ended ({state.status}) without a plan")
            if loop.time() >= deadline:
                raise PlanError(f"Devin planner session {created.url} did not finish within {timeout_s:g}s")
            await asyncio.sleep(poll_s)

    return run


class Planner:
    """Turn feature descriptions into task graphs on one agent."""

    def __init__(self, run: PlanRunner, *, repo: str, product_brief: str | None = None) -> None:
        """Use ``run`` to reach the agent; ``repo`` and ``product_brief`` ground the prompt."""
        self.run = run
        self.repo = repo
        self.product_brief = product_brief

    async def plan(self, text: str) -> TaskGraph:
        """Return the task graph for ``text``; raises PlanError when the agent's answer is unusable."""
        if not text.strip():
            raise ValueError("feature description must not be empty")
        prompt = build_plan_prompt(text, repo=self.repo, product_brief=self.product_brief)
        return parse_plan(await self.run(prompt, PLAN_SCHEMA))


def build_planner(
    settings: GarageSettings,
    *,
    agent: str | None = None,
    model: str | None = None,
    repo_path: Path | None = None,
) -> Planner:
    """Return the planner for a project; ``agent`` and ``model`` override its ``[planner]`` settings."""
    selected = agent or settings.planner_agent
    chosen_model = model or (settings.planner_model if selected == settings.planner_agent else None)
    if selected == "claude-code" and chosen_model is None:
        chosen_model = "opus"
    cwd = (repo_path or settings.local_repo_path or settings.manifest_dir).expanduser().resolve()
    run: PlanRunner
    if selected == "devin":
        if not settings.devin_api_key or not settings.devin_org_id:
            raise ValueError("the devin planner needs DEVIN_API_KEY and DEVIN_ORG_ID")
        client = DevinClient(
            settings.devin_api_key,
            settings.devin_org_id,
            base_url=settings.devin_api_base_url,
            max_acu=settings.devin_max_acu,
        )
        tags = [f"project:{settings.project}"] if settings.project else []
        run = devin_runner(client, repo=settings.repo, tags=tags, timeout_s=settings.local_timeout_s)
    elif selected == "claude-code":
        run = claude_runner(
            cli_path=settings.claude_path, model=chosen_model, cwd=cwd, timeout_s=settings.local_timeout_s
        )
    elif selected == "codex":
        run = codex_runner(
            cli_path=settings.codex_path, model=chosen_model, cwd=cwd, timeout_s=settings.local_timeout_s
        )
    else:
        raise ValueError(f"unknown planner agent: {selected}")
    return Planner(run, repo=settings.repo, product_brief=settings.manifest.product_brief)


def render_plan(graph: TaskGraph) -> str:
    """Render a task graph as plain text, in build order."""
    lines = [graph.summary.strip(), ""]
    for index, task in enumerate(graph.order(), start=1):
        lines.append(f"{index}. [{task.id}] {task.title}")
        if task.depends_on:
            lines.append(f"   Depends on: {', '.join(task.depends_on)}")
        lines.extend(f"   {line}" if line else "" for line in task.body.strip().splitlines())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
