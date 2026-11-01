"""Garage command-line entry point."""

import os
import sys
import json
import asyncio
import logging
import argparse
import ipaddress
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections.abc import Callable, Sequence

import uvicorn
from dotenv import load_dotenv

from garage.ui import create_app
from garage_sdk import configure
from garage.store import Store
from garage.github import GitHubIssues
from garage.report import READY, DEFAULT_ATTRIBUTION, DEFAULT_REQUEST_ATTRIBUTION, RequestLogger, IncidentReporter
from garage.tracker import Tracker, IssueRecord
from garage_sdk.api import ItemKind, IssueLink, ItemState, ClientKind, SubmitWork, json_schemas
from garage.dispatch import Dispatcher, build_fix_prompt
from garage.requests import RequestBridge
from garage.settings import GarageSettings, settings_from_env
from garage_sdk.events import Event, EventLog
from garage.agents.devin import DevinClient
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

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        raise RuntimeError("offline tracker cannot create issues")

    async def update_issue(
        self, number: int, *, title: str | None = None, body: str | None = None, state: str | None = None
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
        ),
        RequestLogger(
            issues,
            max_per_day=cap,
            attribution=settings.manifest.report.request_attribution or DEFAULT_REQUEST_ATTRIBUTION,
        ),
    )


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
    missing = [
        name
        for name, value in (
            ("GARAGE_GITHUB_TOKEN", settings.github_token),
            ("DEVIN_API_KEY", settings.devin_api_key),
            ("DEVIN_ORG_ID", settings.devin_org_id),
        )
        if not value
    ]
    if missing and not dry_run:
        logger.warning("GARAGE_DISPATCH is on but %s is not set; not dispatching.", ", ".join(missing))
        return None
    tracker: Tracker = (
        _OfflineTracker(settings.repo) if offline else GitHubIssues(settings.repo, settings.github_token)
    )
    agent = DevinClient(
        settings.devin_api_key or "",
        settings.devin_org_id or "",
        base_url=settings.devin_api_base_url,
        max_acu=settings.devin_max_acu,
    )
    return Dispatcher(
        tracker,
        agent,
        settings.data_dir / "dispatch.json",
        manifest=settings.manifest,
        interval_s=settings.dispatch_interval_s,
        dry_run=dry_run,
        legacy_ledger_path=_legacy_ledger_path(settings),
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
        item, _ = store.submit("garage-monitor", ClientKind.agent, work, work.repo)
        item.issue = IssueLink(number=issue.number, url=issue.url)
        item.state = ItemState.approved if ready else ItemState.new
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


def _serve(settings: GarageSettings, host: str, port: int) -> int:
    store = Store(settings.data_dir / "garage.db")
    source = _event_log(settings)
    sink = EventLog(settings.data_dir / "events")
    configure(sink)
    detector = build_detector(settings.detector, settings.rules_path)
    monitor = IncidentMonitor(
        detector,
        source,
        sink=sink,
        lookback=timedelta(seconds=settings.lookback_s),
    )
    reporter, request_logger = build_reporters(settings)
    if reporter is not None:
        reporter.on_filed = _incident_sink(settings)
        monitor.add_listener(reporter.notify)
    dispatcher = build_dispatcher(settings)
    bridge = RequestBridge(
        request_logger,
        bridge_token=settings.bridge_token,
        on_filed=dispatcher.poke if dispatcher is not None else None,
        status=dispatcher.request_status if dispatcher is not None else None,
    )
    app = create_app(event_log=source, detector=detector, reporter=reporter, bridge=bridge, store=store)
    app.state.monitor = monitor

    tasks: list[asyncio.Task[None]] = []

    async def poll_events() -> None:
        previous = _event_signature(source.directory)
        while True:
            await asyncio.sleep(settings.poll_interval_s)
            current = _event_signature(source.directory)
            if current != previous:
                previous = current
                monitor.poke()

    async def start_background() -> None:
        tasks.append(asyncio.create_task(monitor.run()))
        tasks.append(asyncio.create_task(poll_events()))
        if reporter is not None:
            tasks.append(asyncio.create_task(reporter.run()))
        if dispatcher is not None:
            tasks.append(asyncio.create_task(dispatcher.run()))

    async def stop_background() -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        configure(None)

    app.router.on_startup.append(start_background)
    app.router.on_shutdown.append(stop_background)
    if not _loopback_host(host):
        logger.warning("Garage is binding to non-loopback host %s; the request bridge remains loopback-only.", host)
    uvicorn.run(app, host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the Garage CLI."""
    load_dotenv()
    parser = argparse.ArgumentParser(prog="garage", description="Local incident monitoring and dispatch.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    schemas_parser = subparsers.add_parser("schemas", help="write v1 JSON schemas")
    schemas_parser.add_argument("--out", type=Path, default=Path("schemas/v1"))
    client_parser = subparsers.add_parser("client", help="manage inbox client tokens")
    client_parser.add_argument("--manifest", type=Path)
    clients = client_parser.add_subparsers(dest="client_command", required=True)
    add = clients.add_parser("add")
    add.add_argument("name")
    add.add_argument("--kind", choices=["owner", "app", "agent"], required=True)
    add.add_argument("--default-repo")
    clients.add_parser("list")
    remove = clients.add_parser("remove")
    remove.add_argument("name")
    items_parser = subparsers.add_parser("items", help="read the local inbox")
    items_parser.add_argument("--manifest", type=Path)
    items_parser.add_argument("--state", type=ItemState)
    items_parser.add_argument("--json", action="store_true")

    report_parser = subparsers.add_parser("report", help="report incidents once")
    report_parser.add_argument("--manifest", type=Path)
    report_parser.add_argument("--dry-run", action="store_true", help="print actions without changing issues")
    report_parser.add_argument("--offline", action="store_true", help="preview without checking existing issues")
    report_parser.add_argument("--since", default="24h", help="lookback such as 6h or 7d")

    dispatch_parser = subparsers.add_parser("dispatch", help="dispatch ready issues once")
    dispatch_parser.add_argument("--manifest", type=Path)
    dispatch_parser.add_argument("--dry-run", action="store_true", help="print actions without starting sessions")
    dispatch_parser.add_argument("--offline", action="store_true", help="preview without checking existing issues")
    dispatch_parser.add_argument("--show-prompt", action="store_true", help="print each session prompt")

    serve_parser = subparsers.add_parser("serve", help="serve the local incidents UI and request bridge")
    serve_parser.add_argument("--manifest", type=Path)
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    log_level = logging.getLevelNamesMapping().get(os.getenv("GARAGE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "schemas":
            args.out.mkdir(parents=True, exist_ok=True)
            for name, schema in json_schemas().items():
                (args.out / f"{name}.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
            return 0
        settings = settings_from_env(args.manifest)
        if args.command == "client":
            store = Store(settings.data_dir / "garage.db")
            if args.client_command == "add":
                print(store.add_client(args.name, ClientKind(args.kind), args.default_repo))
            elif args.client_command == "remove":
                store.remove_client(args.name)
            else:
                print(json.dumps(store.clients(), indent=2))
            return 0
        if args.command == "items":
            items = Store(settings.data_dir / "garage.db").items(args.state)
            if args.json:
                print(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
            else:
                for item in items:
                    print(f"{item.id} {item.state} {item.submitted_by}: {item.text}")
            return 0
        if args.command == "report":
            dry_run = args.dry_run or args.offline
            if not dry_run and not settings.github_token:
                print("GARAGE_GITHUB_TOKEN is not set; use --dry-run or add the token to .env", file=sys.stderr)
                return 2
            if args.offline:
                print("Offline mode: existing issues aren't checked.")
            actions = asyncio.run(_report(settings, args.since, dry_run, offline=args.offline))
            prefix = "[dry run] " if dry_run else ""
            for action in actions:
                print(f"{prefix}{action}")
            if not actions:
                print(f"{prefix}nothing to post for {settings.repo}")
            return 0
        if args.command == "dispatch":
            dry_run = args.dry_run or args.offline
            if args.offline:
                print("Offline mode: existing issues aren't checked.")
            dispatcher = build_dispatcher(settings, dry_run=dry_run, offline=args.offline)
            if dispatcher is None:
                print(
                    "needs GARAGE_GITHUB_TOKEN, DEVIN_API_KEY and DEVIN_ORG_ID in .env (or use --dry-run)",
                    file=sys.stderr,
                )
                return 2
            actions = asyncio.run(_dispatch_once(dispatcher, args.show_prompt))
            prefix = "[dry run] " if dry_run else ""
            for action in actions:
                print(f"{prefix}{action}")
            if not actions:
                print(f"{prefix}nothing to dispatch for {settings.repo}")
            return 0
        host = args.host if args.host is not None else settings.host
        port = args.port if args.port is not None else settings.port
        return _serve(settings, host, port)
    except Exception as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
