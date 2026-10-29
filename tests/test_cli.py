# mypy: disable-error-code="no-untyped-def"

from datetime import datetime, timezone

from conftest import FakeAgent, FakeTracker, example_manifest

from garage import cli
from garage.report import READY, INCIDENT
from garage.tracker import IssueRecord
from garage.settings import settings_from_env
from garage_sdk.events import EventLog


def _emit_rule_events(monkeypatch, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    events_dir = tmp_path / "events"
    event_log = EventLog(events_dir, clock=lambda: now)
    for _ in range(3):
        event_log.emit("mic.check", "mic_check", verdict="silent")
    monkeypatch.setenv("GARAGE_EVENTS_DIR", str(events_dir))
    monkeypatch.delenv("GARAGE_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)


def test_report_cli_dry_run_and_token_guard(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "settings_from_env", lambda manifest: settings_from_env(example_manifest()))
    calls: list[tuple[str, bool]] = []

    async def fake_report(settings, since, dry_run, offline=False):
        calls.append((since, dry_run))
        return ["open [factory:incident]: Runtime error"]

    monkeypatch.setattr(cli, "_report", fake_report)
    assert cli.main(["report", "--dry-run", "--since", "6h", "--manifest", str(example_manifest())]) == 0
    assert calls == [("6h", True)]
    assert "open [factory:incident]" in capsys.readouterr().out

    settings = settings_from_env(example_manifest())
    monkeypatch.setattr(cli, "settings_from_env", lambda manifest: settings)
    monkeypatch.setattr(cli, "_report", lambda *args: (_ for _ in ()).throw(AssertionError("called")))
    assert cli.main(["report"]) == 2


def test_report_dry_run_reads_existing_issue_without_mutating(monkeypatch, tmp_path, capsys) -> None:
    _emit_rule_events(monkeypatch, tmp_path)
    tracker = FakeTracker()
    tracker.issues.append(
        IssueRecord(
            number=17,
            url="https://github.com/sesh11/ambient-home/issues/17",
            state="open",
            title="Microphone delivers only zeros",
            body="<!-- factory-fingerprint: mic_all_zero:da39a3ee5e -->\nold body",
            labels=[INCIDENT, READY],
            created_at=datetime.now(timezone.utc),
        )
    )
    monkeypatch.setattr(cli, "GitHubIssues", lambda repo, token: tracker)

    assert cli.main(["report", "--dry-run", "--manifest", str(example_manifest())]) == 0

    output = capsys.readouterr().out
    assert "update #17" in output
    assert "open [factory:incident" not in output
    assert tracker.calls == ["list_issues:factory:incident"]
    assert tracker.mutations == []


def test_dispatch_dry_run_lists_ready_issue_and_prompt_without_agent_calls(monkeypatch, tmp_path, capsys) -> None:
    tracker = FakeTracker()
    tracker.issues.append(
        IssueRecord(
            number=23,
            url="https://github.com/sesh11/ambient-home/issues/23",
            state="open",
            title="Fix a bug",
            body="The service is failing.",
            labels=[READY],
            created_at=datetime.now(timezone.utc),
        )
    )
    agent = FakeAgent()
    monkeypatch.setattr(cli, "GitHubIssues", lambda repo, token: tracker)
    monkeypatch.setattr(cli, "DevinClient", lambda *args, **kwargs: agent)
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GARAGE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("DEVIN_ORG_ID", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)

    assert cli.main(["dispatch", "--dry-run", "--show-prompt", "--manifest", str(example_manifest())]) == 0

    output = capsys.readouterr().out
    assert "start session for #23" in output
    assert "----- prompt for #23 -----" in output
    assert "Fix GitHub issue #23" in output
    assert tracker.calls == ["list_issues:factory:ready-for-fix", "list_issues:factory:ready-for-fix"]
    assert tracker.mutations == []
    assert agent.calls == []


def test_offline_report_and_dispatch_do_not_construct_github_client(monkeypatch, tmp_path, capsys) -> None:
    _emit_rule_events(monkeypatch, tmp_path)

    def forbidden_github_client(*args, **kwargs):
        raise AssertionError("offline mode attempted to construct a GitHub client")

    monkeypatch.setattr(cli, "GitHubIssues", forbidden_github_client)
    agent = FakeAgent()
    monkeypatch.setattr(cli, "DevinClient", lambda *args, **kwargs: agent)

    assert cli.main(["report", "--offline", "--manifest", str(example_manifest())]) == 0
    report_output = capsys.readouterr().out
    assert "existing issues aren't checked" in report_output
    assert "[dry run] open [factory:incident" in report_output

    assert cli.main(["dispatch", "--offline", "--manifest", str(example_manifest())]) == 0
    dispatch_output = capsys.readouterr().out
    assert "existing issues aren't checked" in dispatch_output
    assert "[dry run] nothing to dispatch" in dispatch_output
    assert agent.calls == []


def test_load_dotenv_runs_before_environment_resolution(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "load_dotenv", lambda: calls.append("dotenv"))

    def settings_after_dotenv(manifest):
        calls.append("settings")
        return None

    monkeypatch.setattr(cli, "settings_from_env", settings_after_dotenv)
    monkeypatch.setattr(cli, "_report", lambda *args: None)
    assert cli.main(["report", "--dry-run"]) == 1
    assert calls == ["dotenv", "settings"]
