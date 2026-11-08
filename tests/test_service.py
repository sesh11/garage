# mypy: disable-error-code="no-untyped-def"

import plistlib
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from garage import cli, service
from garage.settings import settings_from_env


ROOT = Path(__file__).parents[1]


def _paths(tmp_path: Path, checkout: Path | None = None) -> service.ServicePaths:
    root = checkout or tmp_path / "garage & co"
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'garage'\n")
    return service.resolve_paths(root, data_dir=tmp_path / "data", home=tmp_path / "home")


def test_garage_manifest_runs_without_signals_or_rules(monkeypatch) -> None:
    for name in ("GARAGE_EVENTS_DIR", "GARAGE_REPO", "GARAGE_LOCAL_REPO_PATH"):
        monkeypatch.delenv(name, raising=False)
    settings = settings_from_env(ROOT / "garage.toml")
    assert settings.repo == "sesh11/garage"
    assert settings.events_dir is None and settings.rules_path is None
    assert settings.manifest.dispatch.agent == "devin"
    assert "uv run pytest -q" in settings.manifest.build.check
    assert any("env -u GH_TOKEN git push" in rule for rule in settings.manifest.dispatch.prompt_rules)


def test_serve_without_signals_skips_incident_detection(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GARAGE_EVENTS_DIR", raising=False)
    monkeypatch.setenv("GARAGE_DATA_DIR", str(tmp_path))
    settings = settings_from_env(ROOT / "garage.toml")
    app = SimpleNamespace(router=SimpleNamespace(on_startup=[], on_shutdown=[]), state=SimpleNamespace())
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "configure", lambda _: None)
    monkeypatch.setattr(cli, "build_reporters", lambda _: (object(), None))
    monkeypatch.setattr(cli, "create_app", lambda **kwargs: captured.update(kwargs) or app)
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)

    assert cli._serve(settings, "127.0.0.1", 8771) == 0

    assert captured["event_log"] is None and captured["detector"] is None and captured["reporter"] is None
    assert app.state.monitor is None


def test_render_plist_is_valid_and_strips_python_path(tmp_path) -> None:
    paths = _paths(tmp_path)
    parsed = plistlib.loads(service.render_plist(paths).encode())
    assert parsed["Label"] == "ai.garage.serve"
    assert parsed["ProgramArguments"][:5] == ["/usr/bin/env", "-u", "PYTHONPATH", "-u", "PYTHONHOME"]
    assert parsed["ProgramArguments"][5:] == [
        str(paths.checkout / ".venv/bin/garage"),
        "serve",
        "--manifest",
        str(paths.checkout / "garage.toml"),
    ]
    assert parsed["WorkingDirectory"] == str(paths.checkout)
    env = parsed["EnvironmentVariables"]
    assert "PYTHONPATH" not in env
    assert env["GARAGE_DATA_DIR"] == str(tmp_path / "data")
    assert env["GARAGE_PORT"] == str(service.DEFAULT_PORT)
    assert env["PATH"].split(":")[0] == str(paths.checkout / ".venv/bin")
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["StandardOutPath"] == str(tmp_path / "home/Library/Logs/garage/serve.log")


def test_install_dry_run_touches_nothing(tmp_path, capsys) -> None:
    paths = _paths(tmp_path)
    service.install(paths, dry_run=True)
    assert "<string>ai.garage.serve</string>" in capsys.readouterr().out
    assert not paths.plist_path.exists()


def test_install_and_uninstall_drive_launchctl(monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    paths.garage_bin.parent.mkdir(parents=True)
    paths.garage_bin.write_text("")
    paths.manifest.write_text("")
    calls: list[list[str]] = []
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def run(args, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_run", run)

    service.install(paths)
    assert plistlib.loads(paths.plist_path.read_bytes())["Label"] == "ai.garage.serve"
    assert [call[:2] for call in calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]

    service.uninstall(paths)
    assert not paths.plist_path.exists()
    assert calls[-1][:2] == ["launchctl", "bootout"]


def test_install_refuses_off_macos(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="macOS-only"):
        service.install(_paths(tmp_path))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _clone_with_upstream(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    (upstream / "pyproject.toml").write_text("[project]\nname = 'garage'\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "first")
    _git(tmp_path, "clone", "-q", str(upstream), "running")
    return upstream, tmp_path / "running"


def _fake_tools(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = service._run

    def run(args, cwd=None):
        if args[0] == "git":
            return real(args, cwd)
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "find_uv", lambda: "uv")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    return calls


def test_update_pulls_syncs_and_restarts(monkeypatch, tmp_path) -> None:
    upstream, running = _clone_with_upstream(tmp_path)
    (upstream / "new.txt").write_text("new\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "second")
    calls = _fake_tools(monkeypatch)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    plist = service.resolve_paths(running).plist_path
    plist.parent.mkdir(parents=True)
    plist.write_text("")

    service.update(running)

    assert (running / "new.txt").exists()
    assert calls[0] == ["uv", "sync", "--locked"]
    assert calls[1][:3] == ["launchctl", "kickstart", "-k"]


def test_update_does_nothing_when_current(monkeypatch, tmp_path, capsys) -> None:
    _, running = _clone_with_upstream(tmp_path)
    calls = _fake_tools(monkeypatch)
    service.update(running)
    assert calls == []
    assert "already up to date" in capsys.readouterr().out


def test_update_refuses_a_dirty_checkout(monkeypatch, tmp_path) -> None:
    _, running = _clone_with_upstream(tmp_path)
    (running / "pyproject.toml").write_text("edited\n")
    _fake_tools(monkeypatch)
    with pytest.raises(RuntimeError, match="local changes"):
        service.update(running)


def test_cli_service_install_dry_run(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    assert cli.main(["service", "install", "--dry-run", "--checkout", str(ROOT), "--port", "8780"]) == 0
    parsed = plistlib.loads(capsys.readouterr().out.split("\n", 1)[1].encode())
    assert parsed["EnvironmentVariables"]["GARAGE_PORT"] == "8780"
