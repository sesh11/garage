from conftest import example_manifest

# mypy: disable-error-code="no-untyped-def"
from garage import cli
from garage.settings import settings_from_env


def test_report_cli_dry_run_and_token_guard(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "settings_from_env", lambda manifest: settings_from_env(example_manifest()))
    calls: list[tuple[str, bool]] = []

    async def fake_report(settings, since, dry_run):
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


def test_dispatch_dry_run_needs_no_secrets_or_network(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GARAGE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    monkeypatch.delenv("DEVIN_ORG_ID", raising=False)
    assert cli.main(["dispatch", "--dry-run", "--manifest", str(example_manifest())]) == 0
    assert "nothing to dispatch" in capsys.readouterr().out


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
