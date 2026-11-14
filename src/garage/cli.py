"""Garage command-line entry point."""

import os
import sys
import json
import asyncio
import getpass
import logging
import secrets
import argparse
import ipaddress
from typing import Literal, cast
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections.abc import Callable, Sequence

import uvicorn
from dotenv import find_dotenv, load_dotenv

from garage import service
from garage.ui import UiProject, create_app
from garage_sdk import configure
from garage.store import UNASSIGNED, Store
from garage.github import GitHubIssues
from garage.report import (
    READY,
    LABELS,
    DEFAULT_ATTRIBUTION,
    DEFAULT_REQUEST_ATTRIBUTION,
    RequestLogger,
    IncidentReporter,
)
from garage.tracker import Tracker, IssueRecord, PullRequestRecord, canonical_label
from garage_sdk.api import ItemKind, IssueLink, ItemState, ClientKind, SubmitWork, json_schemas
from garage.approval import (
    Approvals,
    IssueWatcher,
    ApprovalError,
    ApprovalTarget,
    describe,
    dispatch_hold,
    initial_state,
)
from garage.dispatch import OUTPUT_SCHEMA, Dispatcher, build_fix_prompt
from garage.projects import only, project_info, select_projects
from garage.requests import Request, BridgeProject, RequestBridge
from garage.settings import GarageSettings, settings_from_env
from garage_sdk.events import Event, EventLog
from garage.agents.base import CodingAgent
from garage.agents.devin import DevinClient
from garage.agents.local import LocalAgent
from garage.incidents.base import Detector, Incident, parse_duration
from garage.incidents.monitor import IncidentMonitor
from garage.incidents.registry import build_detector


logger = logging.getLogger(__name__)


class _OfflineTracker:
    """Empty tracker used to keep dry runs independent of external services."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        return None

    async def list_issues(self, label: str) -> list[IssueRecord]:
        return []

    async def get_issue(self, number: int) -> IssueRecord | None:
        return None

    async def get_pull_request(self, url: str) -> PullRequestRecord | None:
        return None

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        raise RuntimeError("offline tracker cannot create issues")

    async def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> IssueRecord:
        raise RuntimeError("offline tracker cannot update issues")

    async def add_labels(self, number: int, labels: list[str]) -> None:
        raise RuntimeError("offline tracker cannot add labels")

    async def remove_label(self, number: int, label: str) -> None:
        raise RuntimeError("offline tracker cannot remove labels")

    async def comment(self, number: int, body: str) -> None:
        raise RuntimeError("offline tracker cannot comment")


def build_reporters(settings: GarageSettings) -> tuple[IncidentReporter | None, RequestLogger | None]:
    """Return reporting services when report mode and credentials are configured."""
    if not settings.report:
        return None, None
    if not settings.github_token:
        logger.warning("GARAGE_REPORT is on but GARAGE_GITHUB_TOKEN is not set; not filing GitHub issues.")
        return None, None
    issues = GitHubIssues(settings.repo, settings.github_token)
    cap = settings.max_new_issues_per_day
    return (
        IncidentReporter(
            issues,
            max_new_per_day=cap,
            attribution=settings.manifest.report.attribution or DEFAULT_ATTRIBUTION,
            needs_approval=settings.needs_approval,
        ),
        _request_logger(settings, issues),
    )


def _request_logger(settings: GarageSettings, issues: Tracker) -> RequestLogger:
    return RequestLogger(
        issues,
        max_per_day=settings.max_new_issues_per_day,
        attribution=settings.manifest.report.request_attribution or DEFAULT_REQUEST_ATTRIBUTION,
        needs_approval=settings.needs_approval,
    )


PROJECTS_HELP = "projects file listing every project (default: GARAGE_PROJECTS)"


def _legacy_ledger_path(settings: GarageSettings) -> Path | None:
    value = settings.manifest.dispatch.legacy_ledger
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else settings.manifest_dir / path


def build_dispatcher(settings: GarageSettings, *, dry_run: bool = False, offline: bool = False) -> Dispatcher | None:
    """Return the dispatcher when enabled and configured."""
    if not settings.dispatch and not dry_run:
        return None
    selected = settings.manifest.dispatch.agent
    if selected not in ("devin", "claude-code", "codex"):
        raise ValueError(f"unknown dispatch agent: {selected}")
    required = [("GARAGE_GITHUB_TOKEN", settings.github_token)]
    if selected == "devin":
        required += [("DEVIN_API_KEY", settings.devin_api_key), ("DEVIN_ORG_ID", settings.devin_org_id)]
    missing = [name for name, value in required if not value]
    if missing and not dry_run:
        logger.warning("GARAGE_DISPATCH is on but %s is not set; not dispatching.", ", ".join(missing))
        return None
    tracker: Tracker = (
        _OfflineTracker(settings.repo) if offline else GitHubIssues(settings.repo, settings.github_token)
    )
    agent: CodingAgent
    if selected == "devin":
        agent = DevinClient(
            settings.devin_api_key or "",
            settings.devin_org_id or "",
            base_url=settings.devin_api_base_url,
            max_acu=settings.devin_max_acu,
        )
    else:
        agent = LocalAgent(
            cast(Literal["codex", "claude-code"], selected),
            settings.local_repo_path or settings.manifest_dir,
            settings.data_dir,
            base_branch=settings.manifest.dispatch.base_branch,
            cli_path=settings.codex_path if selected == "codex" else settings.claude_path,
            model=settings.local_model,
            allowed_tools=settings.claude_allowed_tools,
            timeout_s=settings.local_timeout_s,
            max_concurrent=settings.local_max_concurrent,
            github=GitHubIssues(settings.repo, settings.github_token) if not offline else None,
        )
    return Dispatcher(
        tracker,
        agent,
        settings.dispatch_ledger,
        manifest=settings.manifest,
        interval_s=settings.dispatch_interval_s,
        dry_run=dry_run,
        legacy_ledger_path=_legacy_ledger_path(settings),
        project=settings.project or None,
        hold=None if offline else dispatch_hold(Store(settings.data_dir / "garage.db"), settings.repo),
        max_open=settings.max_open,
    )


async def detect_over(detector: Detector, events: Sequence[Event], start: datetime, now: datetime) -> list[Incident]:
    """Replay detection over the lookback so expired rule windows remain reportable."""
    relevant = sorted(
        (event for event in events if event.component != "factory" and event.type != "app.heartbeat"),
        key=lambda event: event.ts,
    )
    points = {now}
    for event in relevant:
        points.update(t for t in (event.ts, event.ts + timedelta(hours=1)) if start <= t <= now)
    best: dict[str, Incident] = {}
    for point in sorted(points):
        for incident in await detector.detect([event for event in relevant if event.ts <= point], point):
            current = best.get(incident.fingerprint)
            if current is None or (incident.ready, incident.count) > (current.ready, current.count):
                best[incident.fingerprint] = incident
    return sorted(best.values(), key=lambda incident: incident.first_seen)


def _event_log(settings: GarageSettings) -> EventLog:
    if settings.events_dir is None:
        raise ValueError("the manifest needs [signals].path or GARAGE_EVENTS_DIR")
    return EventLog(settings.events_dir, retention_days=settings.retention_days)


def _incident_sink(settings: GarageSettings) -> Callable[[Incident, IssueRecord, bool], None]:
    store = Store(settings.data_dir / "garage.db")

    def record(incident: Incident, issue: IssueRecord, ready: bool) -> None:
        work = SubmitWork(id=incident.fingerprint, kind=ItemKind.incident, text=incident.title, repo=settings.repo)
        item, _ = store.submit("garage-monitor", ClientKind.agent, work, work.repo, settings.project or None)
        item.issue = IssueLink(number=issue.number, url=issue.url)
        item.state = initial_state(issue.labels, settings.needs_approval)
        store.save(item, "garage-monitor", "file_success")

    return record


async def _report(settings: GarageSettings, since: str, dry_run: bool, offline: bool = False) -> list[str]:
    now = datetime.now(timezone.utc)
    event_log = _event_log(settings)
    detector = build_detector(settings.detector, settings.rules_path)
    start = now - parse_duration(since)
    incidents = await detect_over(detector, event_log.read(start, now), start, now)
    issues: Tracker = _OfflineTracker(settings.repo) if offline else GitHubIssues(settings.repo, settings.github_token)
    reporter = IncidentReporter(
        issues,
        max_new_per_day=settings.max_new_issues_per_day,
        dry_run=dry_run,
        attribution=settings.manifest.report.attribution or DEFAULT_ATTRIBUTION,
        on_filed=_incident_sink(settings) if not dry_run else None,
        needs_approval=settings.needs_approval,
    )
    print(f"{len(incidents)} incident(s) in the last {since} from {event_log.directory}")
    return await reporter.sync(incidents)


async def _dispatch_once(dispatcher: Dispatcher, show_prompt: bool) -> list[str]:
    if show_prompt:
        for issue in await dispatcher.tracker.list_issues(READY):
            if issue.state == "open":
                print(f"----- prompt for #{issue.number} -----")
                print(build_fix_prompt(issue, dispatcher.tracker.repo, dispatcher.manifest))
    return await dispatcher.poll_once()


def _event_signature(directory: Path) -> tuple[str, int, int] | None:
    try:
        newest = max(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime_ns)
        stat = newest.stat()
        return newest.name, stat.st_mtime_ns, stat.st_size
    except (OSError, ValueError):
        return None


def _loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve(
    projects: GarageSettings | Sequence[GarageSettings], host: str, port: int, *, require_signals: bool = True
) -> int:
    """Serve every project from one process: one inbox, one dashboard, per-project monitors and dispatchers."""
    projects = [projects] if isinstance(projects, GarageSettings) else list(projects)
    shared = projects[0]
    store = Store(shared.data_dir / "garage.db")
    tagged = store.tag_projects({project.repo.lower(): project.project for project in projects if project.project})
    if tagged:
        logger.info("Tagged %d existing inbox item(s) with their project.", tagged)
    sink = EventLog(shared.data_dir / "events")
    configure(sink)
    monitors: list[tuple[GarageSettings, IncidentMonitor, EventLog]] = []
    reporters: list[IncidentReporter] = []
    dispatchers: list[Dispatcher] = []
    watchers: list[IssueWatcher] = []
    targets: list[ApprovalTarget] = []
    bridge_projects: list[BridgeProject] = []
    ui_projects: list[UiProject] = []
    for settings in projects:
        source: EventLog | None = None
        detector: Detector | None = None
        if settings.events_dir is not None or require_signals:
            source = _event_log(settings)
            detector = build_detector(settings.detector, settings.rules_path)
        else:
            logger.info("Project %s has no [signals].path; incident detection is off for it.", settings.project)
        reporter, request_logger = build_reporters(settings)
        if source is not None and detector is not None:
            monitor = IncidentMonitor(detector, source, sink=sink, lookback=timedelta(seconds=settings.lookback_s))
            if reporter is not None:
                reporter.on_filed = _incident_sink(settings)
                monitor.add_listener(reporter.notify)
            monitors.append((settings, monitor, source))
        if reporter is not None:
            reporters.append(reporter)
        dispatcher = build_dispatcher(settings)
        if dispatcher is not None:
            dispatchers.append(dispatcher)
        wake = dispatcher.poke if dispatcher is not None else None
        targets.append(
            ApprovalTarget(
                settings.project or None,
                settings.repo,
                GitHubIssues(settings.repo, settings.github_token) if settings.github_token else None,
                on_approved=wake,
            )
        )
        if settings.intake:
            watcher = build_watcher(settings, store, on_approved=wake)
            if watcher is None:
                logger.warning("GARAGE_INTAKE is on but GARAGE_GITHUB_TOKEN is not set; not watching new issues.")
            else:
                watchers.append(watcher)
        bridge_projects.append(
            BridgeProject(
                settings.project or None,
                settings.repo,
                request_logger,
                on_filed=dispatcher.poke if dispatcher is not None else None,
                status=dispatcher.request_status if dispatcher is not None else None,
            )
        )
        ui_projects.append(UiProject(settings.project or None, source, detector, reporter, project_info(settings)))
    bridge = RequestBridge(bridge_token=shared.bridge_token, projects=bridge_projects)
    app = create_app(bridge=bridge, store=store, projects=ui_projects, approvals=Approvals(store, targets))
    app.state.monitor = monitors[0][1] if monitors else None
    app.state.monitors = [monitor for _, monitor, _ in monitors]

    tasks: list[asyncio.Task[None]] = []

    async def poll_events(settings: GarageSettings, monitor: IncidentMonitor, source: EventLog) -> None:
        previous = _event_signature(source.directory)
        while True:
            await asyncio.sleep(settings.poll_interval_s)
            current = _event_signature(source.directory)
            if current != previous:
                previous = current
                monitor.poke()

    async def start_background() -> None:
        for settings, monitor, source in monitors:
            tasks.append(asyncio.create_task(monitor.run()))
            tasks.append(asyncio.create_task(poll_events(settings, monitor, source)))
        for reporter in reporters:
            tasks.append(asyncio.create_task(reporter.run()))
        for dispatcher in dispatchers:
            tasks.append(asyncio.create_task(dispatcher.run()))
        for watcher in watchers:
            tasks.append(asyncio.create_task(watcher.run()))

    async def stop_background() -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        configure(None)

    app.router.on_startup.append(start_background)
    app.router.on_shutdown.append(stop_background)
    if not _loopback_host(host):
        logger.warning("Garage is binding to non-loopback host %s; the request bridge remains loopback-only.", host)
    logger.info("Serving project(s): %s", ", ".join(project.project or project.repo for project in projects))
    uvicorn.run(app, host=host, port=port)
    return 0


async def _handoff(args: argparse.Namespace) -> int:
    if not args.task.strip():
        raise ValueError("task must not be empty")
    repo = os.getenv("GARAGE_REPO")
    tags: list[str] = []
    if args.project is not None:
        project = only(_load(args)[0], args.project)[0]
        repo = project.repo
        tags = [f"project:{project.project}"]
        args.repo_path = args.repo_path or project.local_repo_path or project.manifest_dir
        args.base = args.base or project.manifest.dispatch.base_branch
        if args.agent is None and project.manifest.dispatch.agent in ("codex", "claude-code"):
            args.agent = project.manifest.dispatch.agent
    if args.agent is None or args.repo_path is None:
        raise ValueError("pass --agent and --repo-path, or --project for a project with a local agent and path")
    args.base = args.base or "main"
    token = os.getenv("GARAGE_GITHUB_TOKEN")
    if not args.no_push and (not repo or not token):
        raise ValueError("set GARAGE_REPO and GARAGE_GITHUB_TOKEN, or use --no-push")
    agent = LocalAgent(
        args.agent,
        args.repo_path,
        Path(os.getenv("GARAGE_DATA_DIR", "~/.garage")),
        base_branch=args.base,
        cli_path=os.getenv("GARAGE_CODEX_PATH" if args.agent == "codex" else "GARAGE_CLAUDE_PATH"),
        model=args.model or os.getenv("GARAGE_LOCAL_MODEL"),
        allowed_tools=os.getenv("GARAGE_CLAUDE_ALLOWED_TOOLS", "Read,Edit,Write,Bash,Glob,Grep"),
        timeout_s=parse_duration(os.getenv("GARAGE_LOCAL_TIMEOUT", "1h")).total_seconds(),
        max_concurrent=int(os.getenv("GARAGE_LOCAL_MAX_CONCURRENT", "2")),
        github=GitHubIssues(repo, token) if repo and token else None,
        no_push=args.no_push,
    )
    created = await agent.create_session(
        args.task,
        title=args.task.splitlines()[0][:80],
        tags=tags,
        structured_output_schema=OUTPUT_SCHEMA,
    )
    print(f"Run: {created.session_id}\nFiles: {created.url}", flush=True)
    while True:
        state = await agent.get_session(created.session_id)
        if args.detach or state.status != "working":
            print(f"Status: {state.status}\n{state.status_detail}")
            print(json.dumps(state.structured_output, indent=2))
            if args.detach:
                print("Detached. Poll with: garage handoff-status " + created.session_id)
            return 0 if state.status in ("working", "finished") else 1
        await asyncio.sleep(0.25)


async def _handoff_status(args: argparse.Namespace) -> int:
    directory = Path(os.getenv("GARAGE_DATA_DIR", "~/.garage")).expanduser().resolve()
    agent = LocalAgent(
        "codex",
        Path.cwd(),
        directory,
        github=(
            GitHubIssues(os.environ["GARAGE_REPO"], os.getenv("GARAGE_GITHUB_TOKEN"))
            if os.getenv("GARAGE_REPO")
            else None
        ),
    )
    state = await agent.get_session(args.run_id)
    print(state.model_dump_json(indent=2))
    return 0 if state.status in ("working", "finished") else 1


def _load(args: argparse.Namespace) -> tuple[list[GarageSettings], bool]:
    """Return the selected projects and whether they came from a projects file."""
    selected = select_projects(getattr(args, "manifest", None), getattr(args, "projects", None))
    if selected is None:
        return [settings_from_env(getattr(args, "manifest", None))], False
    return selected, True


def _print_projects(projects: Sequence[GarageSettings], as_json: bool) -> None:
    rows = [
        {
            **project_info(project).model_dump(mode="json"),
            "path": str(project.local_repo_path) if project.local_repo_path else None,
            "check": project.manifest.build.check,
            "prompt_rules": len(project.manifest.dispatch.prompt_rules),
            "signals": str(project.events_dir) if project.events_dir else None,
        }
        for project in projects
    ]
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for project, row in zip(projects, rows, strict=True):
        print(
            f"{project.project}  repo={project.repo}  agent={row['agent']}  base={row['base_branch']}"
            f"  path={row['path'] or '-'}  signals={row['signals'] or '-'}"
        )


async def _assign(settings: GarageSettings, item_id: str) -> int:
    store = Store(settings.data_dir / "garage.db")
    item = store.get(item_id)
    if item is None:
        print(f"no item {item_id}", file=sys.stderr)
        return 1
    if item.issue is not None or item.state in {ItemState.done, ItemState.declined, ItemState.cancelled}:
        print(f"{item_id} is already filed or closed", file=sys.stderr)
        return 1
    store.assign(item, settings.project, settings.repo, "cli")
    print(f"{item.id} is now in project {settings.project} ({settings.repo}), state {item.state}")
    _, request_logger = build_reporters(settings)
    if request_logger is None:
        print("Reporting is off, so no issue was filed. To file it, rerun this command with GARAGE_REPORT=1.")
        return 0
    bridge = RequestBridge(
        projects=[BridgeProject(settings.project, settings.repo, request_logger)],
    )
    issue = await bridge.file(
        Request(id=item.id, request=item.text, created_at=item.created_at, project=settings.project)
    )
    if issue is not None:
        item.issue = IssueLink(number=issue.number, url=issue.url)
        item.state = initial_state(issue.labels, settings.needs_approval)
        store.save(item, "cli", "file_success")
        print(f"Filed {issue.url} ({item.state})")
    return 0


def build_approvals(projects: Sequence[GarageSettings], store: Store) -> Approvals:
    """Approve and decline across every project; GitHub writes need GARAGE_GITHUB_TOKEN."""
    return Approvals(
        store,
        [
            ApprovalTarget(
                project.project or None,
                project.repo,
                GitHubIssues(project.repo, project.github_token) if project.github_token else None,
            )
            for project in projects
        ],
    )


def build_watcher(
    settings: GarageSettings,
    store: Store,
    *,
    dry_run: bool = False,
    rescan_s: float | None = None,
    on_approved: Callable[[], None] | None = None,
) -> IssueWatcher | None:
    """Watch one project's repository for new issues; needs GARAGE_GITHUB_TOKEN."""
    if not settings.github_token:
        return None
    return IssueWatcher(
        store,
        GitHubIssues(settings.repo, settings.github_token),
        settings.intake_cursor,
        project=settings.project or None,
        needs_approval=settings.needs_approval,
        interval_s=settings.intake_interval_s,
        dry_run=dry_run,
        rescan_s=rescan_s,
        on_approved=on_approved,
    )


async def _ask(settings: GarageSettings, text: str) -> int:
    store = Store(settings.data_dir / "garage.db")
    work = SubmitWork(
        id="ask-" + secrets.token_hex(6), text=text, repo=settings.repo, project=settings.project or None
    )
    item, _ = store.submit("cli", ClientKind.owner, work, settings.repo, settings.project or None)
    print(f"{item.id} saved for project {settings.project} ({settings.repo})")
    if not settings.github_token:
        print("GARAGE_GITHUB_TOKEN is not set, so no GitHub issue was filed; it stays in the inbox as new.")
        return 0
    issue = await _request_logger(settings, GitHubIssues(settings.repo, settings.github_token)).file(
        Request(
            id=item.id, request=text, created_at=item.created_at, repository=settings.repo, project=settings.project
        )
    )
    if issue is None:
        print("Not filed: the daily new-issue cap was reached.", file=sys.stderr)
        return 1
    item.issue = IssueLink(number=issue.number, url=issue.url)
    item.state = initial_state(issue.labels, settings.needs_approval)
    store.save(item, "cli", "file_success")
    print(f"Filed {issue.url} ({item.state})")
    if item.state == ItemState.awaiting_approval:
        print(f"Approve: garage approve {item.id}   Decline: garage decline {item.id}")
    return 0


async def _decide(projects: Sequence[GarageSettings], ref: str, project: str | None, approve: bool) -> int:
    store = Store(projects[0].data_dir / "garage.db")
    approvals = build_approvals(projects, store)
    try:
        item = approvals.find(ref, project)
        item = await (approvals.approve if approve else approvals.decline)(item, getpass.getuser(), "terminal")
    except ApprovalError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    where = f" ({item.issue.url})" if item.issue else ""
    print(f"{item.id} is now {item.state}{where}")
    return 0


async def _intake(settings: GarageSettings, dry_run: bool, since: str | None) -> list[str]:
    store = Store(settings.data_dir / "garage.db")
    watcher = build_watcher(
        settings, store, dry_run=dry_run, rescan_s=parse_duration(since).total_seconds() if since else None
    )
    if watcher is None:
        raise ValueError("GARAGE_GITHUB_TOKEN is not set; intake reads the project's GitHub issues")
    return await watcher.poll_once()


def _log(projects: Sequence[GarageSettings], ref: str, project: str | None, as_json: bool) -> int:
    store = Store(projects[0].data_dir / "garage.db")
    try:
        item = build_approvals(projects, store).find(ref, project)
    except ApprovalError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    entries = store.history(item.id)
    if as_json:
        print(json.dumps({"item": item.model_dump(mode="json"), "history": entries}, indent=2))
        return 0
    issue = f"  #{item.issue.number}" if item.issue else ""
    print(f"{item.id}{issue}  {item.state}  {item.text.splitlines()[0][:80] if item.text else ''}")
    for entry in entries:
        at = datetime.fromisoformat(str(entry["at"])).astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"  {at}  {describe(entry)}")
    return 0


async def _migrate_labels(settings: GarageSettings, apply: bool, include_closed: bool) -> list[str]:
    """Relabel issues from the old ``factory:`` names to ``garage:``; only print the plan unless ``apply``."""
    if not settings.github_token:
        raise ValueError("GARAGE_GITHUB_TOKEN is not set; the migration reads the project's GitHub issues")
    github = GitHubIssues(settings.repo, settings.github_token)
    found = await github.legacy_labeled(include_closed=include_closed)
    if apply and found:
        await github.ensure_labels(LABELS)
    actions: list[str] = []
    for number, title, old in found:
        new = [canonical_label(name) for name in old]
        if apply:
            await github.add_labels(number, new)
            for name in old:
                await github.remove_label(number, name)
        actions.append(f"#{number} {title}: {', '.join(old)} -> {', '.join(new)}")
    return actions


def main(argv: list[str] | None = None) -> int:
    """Run the Garage CLI."""
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(Path.home() / ".garage" / ".env")
    parser = argparse.ArgumentParser(prog="garage", description="Local incident monitoring and dispatch.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    schemas_parser = subparsers.add_parser("schemas", help="write v1 JSON schemas")
    schemas_parser.add_argument("--out", type=Path, default=Path("schemas/v1"))
    client_parser = subparsers.add_parser("client", help="manage inbox client tokens")
    client_parser.add_argument("--manifest", type=Path)
    client_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    clients = client_parser.add_subparsers(dest="client_command", required=True)
    add = clients.add_parser("add")
    add.add_argument("name")
    add.add_argument("--kind", choices=["owner", "app", "agent"], required=True)
    add.add_argument("--default-repo")
    add.add_argument("--default-project", help="project used when a submission names neither project nor repo")
    add.add_argument("--manifest", type=Path, default=argparse.SUPPRESS)
    add.add_argument("--projects", type=Path, default=argparse.SUPPRESS, help=PROJECTS_HELP)
    clients.add_parser("list")
    remove = clients.add_parser("remove")
    remove.add_argument("name")
    items_parser = subparsers.add_parser("items", help="read the local inbox")
    items_parser.add_argument("--manifest", type=Path)
    items_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    items_parser.add_argument("--state", type=ItemState)
    items_parser.add_argument("--project", help="only this project's items")
    items_parser.add_argument("--unassigned", action="store_true", help="only items without a project")
    items_parser.add_argument("--json", action="store_true")
    assign_parser = subparsers.add_parser("assign", help="put an inbox item in a project and file it")
    assign_parser.add_argument("--manifest", type=Path)
    assign_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    assign_parser.add_argument("item_id")
    assign_parser.add_argument("project")
    projects_parser = subparsers.add_parser("projects", help="list the projects Garage serves")
    projects_parser.add_argument("--manifest", type=Path)
    projects_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    projects_parser.add_argument("--json", action="store_true")

    report_parser = subparsers.add_parser("report", help="report incidents once")
    report_parser.add_argument("--manifest", type=Path)
    report_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    report_parser.add_argument("--project", help="only this project")
    report_parser.add_argument("--dry-run", action="store_true", help="print actions without changing issues")
    report_parser.add_argument("--offline", action="store_true", help="preview without checking existing issues")
    report_parser.add_argument("--since", default="24h", help="lookback such as 6h or 7d")

    intake_parser = subparsers.add_parser("intake", help="take in new GitHub issues once, as proposed")
    intake_parser.add_argument("--manifest", type=Path)
    intake_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    intake_parser.add_argument("--project", help="only this project")
    intake_parser.add_argument("--dry-run", action="store_true", help="print actions without changing anything")
    intake_parser.add_argument("--since", help="also look at issues opened this far back, such as 1h or 2d")
    intake_parser.set_defaults(offline=False)

    log_parser = subparsers.add_parser("log", help="show an item's decision log (who approved, when, from where)")
    log_parser.add_argument("--manifest", type=Path)
    log_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    log_parser.add_argument("--project", help="the item's project, when an issue number is ambiguous")
    log_parser.add_argument("--json", action="store_true")
    log_parser.add_argument("id", help="item id, issue number, or issue URL")

    labels_parser = subparsers.add_parser("labels", help="manage Garage's GitHub labels")
    label_commands = labels_parser.add_subparsers(dest="labels_command", required=True)
    migrate = label_commands.add_parser(
        "migrate", help="rename old factory:* labels to garage:* (prints the plan unless --apply)"
    )
    migrate.add_argument("--manifest", type=Path)
    migrate.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    migrate.add_argument("--project", help="only this project")
    migrate.add_argument("--dry-run", action="store_true", help="print the plan without changing anything (default)")
    migrate.add_argument("--apply", action="store_true", help="relabel the issues and create the garage:* labels")
    migrate.add_argument("--include-closed", action="store_true", help="closed issues too, not only open ones")

    ask_parser = subparsers.add_parser("ask", help="file a request as a proposed GitHub issue")
    ask_parser.add_argument("--manifest", type=Path)
    ask_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    ask_parser.add_argument("--project", help="the project to ask (optional with one project)")
    ask_parser.add_argument("text")
    for name, verb in (("approve", "approve"), ("decline", "decline")):
        decide = subparsers.add_parser(name, help=f"{verb} a proposed item (GitHub issue and inbox)")
        decide.add_argument("--manifest", type=Path)
        decide.add_argument("--projects", type=Path, help=PROJECTS_HELP)
        decide.add_argument("--project", help="the project, when an issue number is ambiguous")
        decide.add_argument("id", help="inbox item id (item_...), issue number, or issue URL")

    dispatch_parser = subparsers.add_parser("dispatch", help="dispatch approved (ready) issues once")
    dispatch_parser.add_argument("--manifest", type=Path)
    dispatch_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    dispatch_parser.add_argument("--project", help="only this project")
    dispatch_parser.add_argument("--dry-run", action="store_true", help="print actions without starting sessions")
    dispatch_parser.add_argument("--offline", action="store_true", help="preview without checking existing issues")
    dispatch_parser.add_argument("--show-prompt", action="store_true", help="print each session prompt")

    serve_parser = subparsers.add_parser("serve", help="serve the local incidents UI and request bridge")
    serve_parser.add_argument("--manifest", type=Path)
    serve_parser.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    handoff = subparsers.add_parser("handoff", help="hand one task to a local coding CLI")
    handoff.add_argument("--agent", choices=("codex", "claude-code"))
    handoff.add_argument("--repo-path", type=Path)
    handoff.add_argument("--base")
    handoff.add_argument("--project", help="use this project's repo, checkout path, base branch and agent")
    handoff.add_argument("--manifest", type=Path)
    handoff.add_argument("--projects", type=Path, help=PROJECTS_HELP)
    handoff.add_argument("--model")
    handoff.add_argument("--no-push", action="store_true")
    handoff.add_argument("--detach", action="store_true")
    handoff.add_argument("task")
    status = subparsers.add_parser("handoff-status", help="recover and poll a detached local run")
    status.add_argument("run_id")

    service_parser = subparsers.add_parser("service", help="run Garage in the background as a macOS LaunchAgent")
    services = service_parser.add_subparsers(dest="service_command", required=True)
    install = services.add_parser("install", help="write and load the LaunchAgent")
    install.add_argument("--projects", type=Path, help="projects file (default: ~/.garage/projects.toml)")
    install.add_argument("--data-dir", type=Path, help="Garage data directory (default: ~/.garage)")
    install.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    install.add_argument("--garage-bin", type=Path, help="installed Garage executable")
    install.add_argument("--dry-run", action="store_true", help="print the plist without touching launchd")
    uninstall = services.add_parser("uninstall", help="unload and delete the LaunchAgent")
    uninstall.add_argument("--dry-run", action="store_true")
    services.add_parser("status", help="show launchd's view of the LaunchAgent")
    services.add_parser("restart", help="restart the LaunchAgent")
    update_parser = subparsers.add_parser("update", help="update the installed Garage tool and restart its service")
    update_parser.add_argument("--source", default=os.getenv("GARAGE_INSTALL_SOURCE", service.DEFAULT_SOURCE))
    update_parser.add_argument("--ref", default=os.getenv("GARAGE_INSTALL_REF", service.DEFAULT_REF))
    update_parser.add_argument("--no-restart", action="store_true")

    args = parser.parse_args(argv)
    log_level = logging.getLevelNamesMapping().get(os.getenv("GARAGE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "handoff":
            return asyncio.run(_handoff(args))
        if args.command == "handoff-status":
            return asyncio.run(_handoff_status(args))
        if args.command == "service":
            if args.service_command == "status":
                return service.status()
            if args.service_command == "restart":
                service.restart()
            elif args.service_command == "uninstall":
                service.uninstall(service.resolve_paths(), dry_run=args.dry_run)
            else:
                paths = service.resolve_paths(
                    garage_bin=args.garage_bin,
                    projects_file=args.projects,
                    data_dir=args.data_dir,
                    port=args.port,
                )
                service.install(paths, dry_run=args.dry_run)
            return 0
        if args.command == "update":
            service.update(args.source, args.ref, restart_service=not args.no_restart)
            return 0
        if args.command == "schemas":
            args.out.mkdir(parents=True, exist_ok=True)
            for name, schema in json_schemas().items():
                (args.out / f"{name}.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
            return 0
        projects, from_file = _load(args)
        settings = projects[0]
        if args.command == "projects":
            _print_projects(projects, args.json)
            return 0
        if args.command == "client":
            store = Store(settings.data_dir / "garage.db")
            if args.client_command == "add":
                default = only(projects, args.default_project)[0].project if args.default_project else None
                print(store.add_client(args.name, ClientKind(args.kind), args.default_repo, default))
            elif args.client_command == "remove":
                store.remove_client(args.name)
            else:
                print(json.dumps(store.clients(), indent=2))
            return 0
        if args.command == "items":
            project_filter = (
                UNASSIGNED if args.unassigned else (only(projects, args.project)[0].project if args.project else None)
            )
            items = Store(settings.data_dir / "garage.db").items(args.state, project_filter)
            if args.json:
                print(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
            else:
                for item in items:
                    print(f"{item.id} {item.state} [{item.project or '-'}] {item.submitted_by}: {item.text}")
            return 0
        if args.command == "assign":
            return asyncio.run(_assign(only(projects, args.project)[0], args.item_id))
        if args.command == "ask":
            chosen = only(projects, args.project)
            if len(chosen) != 1:
                print("several projects: name one with --project", file=sys.stderr)
                return 2
            return asyncio.run(_ask(chosen[0], args.text))
        if args.command == "log":
            return _log(projects, args.id, args.project, args.json)
        if args.command == "labels":
            apply = args.apply and not args.dry_run
            prefix = "" if apply else "[dry run] "
            for project in only(projects, args.project):
                actions = asyncio.run(_migrate_labels(project, apply, args.include_closed))
                for action in actions:
                    print(f"{prefix}{project.repo}{action}")
                if not actions:
                    print(f"{prefix}no factory:* labels left in {project.repo}")
            if not apply:
                print("Nothing changed. Run again with --apply to relabel.")
            return 0
        if args.command in ("approve", "decline"):
            if args.project:
                only(projects, args.project)
            return asyncio.run(_decide(projects, args.id, args.project, args.command == "approve"))
        if args.command in ("report", "dispatch", "intake"):
            chosen = only(projects, args.project)
            dry_run = args.dry_run or args.offline
            if args.command == "report" and not dry_run and not settings.github_token:
                print("GARAGE_GITHUB_TOKEN is not set; use --dry-run or add the token to .env", file=sys.stderr)
                return 2
            if args.offline:
                print("Offline mode: existing issues aren't checked.")
            prefix = "[dry run] " if dry_run else ""
            for project in chosen:
                if len(chosen) > 1:
                    print(f"== project {project.project} ({project.repo}) ==")
                if args.command == "intake":
                    actions = asyncio.run(_intake(project, dry_run, args.since))
                    empty = f"{prefix}nothing new in {project.repo}"
                elif args.command == "report":
                    if project.events_dir is None and from_file:
                        print(f"{prefix}no [signals].path for {project.project}; skipped")
                        continue
                    actions = asyncio.run(_report(project, args.since, dry_run, offline=args.offline))
                    empty = f"{prefix}nothing to post for {project.repo}"
                else:
                    dispatcher = build_dispatcher(project, dry_run=dry_run, offline=args.offline)
                    if dispatcher is None:
                        print(
                            "needs GARAGE_GITHUB_TOKEN and agent credentials (Devin: DEVIN_API_KEY and DEVIN_ORG_ID); or --dry-run",
                            file=sys.stderr,
                        )
                        return 2
                    actions = asyncio.run(_dispatch_once(dispatcher, args.show_prompt))
                    empty = f"{prefix}nothing to dispatch for {project.repo}"
                for action in actions:
                    print(f"{prefix}{action}")
                if not actions:
                    print(empty)
            return 0
        host = args.host if args.host is not None else settings.host
        port = args.port if args.port is not None else settings.port
        return _serve(projects, host, port, require_signals=not from_file)
    except Exception as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
