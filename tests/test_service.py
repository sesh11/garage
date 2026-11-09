# mypy: disable-error-code="no-untyped-def"

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from garage import cli, service


def _paths(tmp_path: Path) -> service.ServicePaths:
    home = tmp_path / "home"
    return service.resolve_paths(
        garage_bin=home / ".local" / "bin" / "garage",
        projects_file=home / ".garage" / "projects.toml",
        data_dir=home / ".garage",
        home=home,
    )


def _make_install_inputs(paths: service.ServicePaths) -> None:
    paths.garage_bin.parent.mkdir(parents=True, exist_ok=True)
    paths.garage_bin.write_text("")
    paths.projects_file.parent.mkdir(parents=True, exist_ok=True)
    paths.projects_file.write_text("[[project]]\nname = 'garage'\n")


def test_render_plist_is_valid_and_uses_projects_install(tmp_path) -> None:
    paths = _paths(tmp_path)
    parsed = plistlib.loads(service.render_plist(paths).encode())
    assert parsed["Label"] == "ai.garage.serve"
    assert parsed["ProgramArguments"] == [
        "/usr/bin/env",
        "-u",
        "PYTHONPATH",
        "-u",
        "PYTHONHOME",
        str(paths.garage_bin),
        "serve",
        "--projects",
        str(paths.projects_file),
    ]
    assert parsed["WorkingDirectory"] == str(paths.data_dir)
    env = parsed["EnvironmentVariables"]
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["HOME"] == str(paths.home)
    assert env["GARAGE_DATA_DIR"] == str(paths.data_dir)
    assert env["GARAGE_PORT"] == "8770"
    assert env["GARAGE_PROJECTS"] == str(paths.projects_file)
    assert env["PATH"].split(":")[0] == str(paths.garage_bin.parent)
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["ThrottleInterval"] == 10
    assert parsed["StandardOutPath"] == str(tmp_path / "home/Library/Logs/garage/serve.log")


def test_install_dry_run_touches_nothing(tmp_path, capsys) -> None:
    paths = _paths(tmp_path)
    service.install(paths, dry_run=True)
    assert "<string>ai.garage.serve</string>" in capsys.readouterr().out
    assert not paths.plist_path.exists()


def test_install_and_uninstall_drive_launchctl_in_order(monkeypatch, tmp_path, capsys) -> None:
    paths = _paths(tmp_path)
    _make_install_inputs(paths)
    calls: list[list[str]] = []
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def run(args, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_run", run)

    service.install(paths)
    assert plistlib.loads(paths.plist_path.read_bytes())["Label"] == "ai.garage.serve"
    assert [call[:2] for call in calls] == [
        ["launchctl", "bootout"],
        ["launchctl", "bootstrap"],
    ]
    assert "warning:" in capsys.readouterr().err

    service.uninstall(paths)
    assert not paths.plist_path.exists()
    assert calls[-1][:2] == ["launchctl", "bootout"]


def test_install_requires_projects_file(monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    paths.garage_bin.parent.mkdir(parents=True)
    paths.garage_bin.write_text("")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    with pytest.raises(FileNotFoundError, match="create the projects file"):
        service.install(paths)


def test_install_refuses_off_macos(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="macOS-only"):
        service.install(_paths(tmp_path))


def test_run_strips_python_environment(monkeypatch) -> None:
    captured: dict[str, dict[str, str]] = {}

    def run(args, *, cwd, env, capture_output, text, check):
        captured["env"] = env
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted")
    monkeypatch.setenv("PYTHONHOME", "/tmp/untrusted")
    monkeypatch.setattr(subprocess, "run", run)
    service._run(["anything"])
    assert "PYTHONPATH" not in captured["env"]
    assert "PYTHONHOME" not in captured["env"]


def test_update_reinstalls_then_kickstarts(monkeypatch, tmp_path) -> None:
    paths = _paths(tmp_path)
    paths.plist_path.parent.mkdir(parents=True)
    paths.plist_path.write_text("")
    calls: list[list[str]] = []

    def run(args, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "find_uv", lambda: "uv")
    monkeypatch.setattr(service, "resolve_paths", lambda: paths)
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    service.update("git+https://github.com/sesh11/garage", "main")

    assert calls == [
        ["uv", "tool", "install", "--reinstall", "git+https://github.com/sesh11/garage@main"],
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.garage.serve"],
    ]


def test_update_no_restart_skips_launchctl(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(args, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "find_uv", lambda: "uv")
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    service.update("source", "ref", restart_service=False)

    assert calls == [["uv", "tool", "install", "--reinstall", "source@ref"]]


def test_cli_service_install_dry_run(capsys, monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    garage_bin = tmp_path / "bin" / "garage"
    projects = tmp_path / "projects.toml"
    assert (
        cli.main(
            [
                "service",
                "install",
                "--dry-run",
                "--garage-bin",
                str(garage_bin),
                "--projects",
                str(projects),
                "--port",
                "8780",
            ]
        )
        == 0
    )
    parsed = plistlib.loads(capsys.readouterr().out.split("\n", 1)[1].encode())
    assert parsed["EnvironmentVariables"]["GARAGE_PORT"] == "8780"
    assert parsed["ProgramArguments"][-1] == str(projects)


def test_dotenv_cwd_precedes_home_and_preserves_environment(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    (home / ".garage").mkdir(parents=True)
    cwd.mkdir()
    (cwd / ".env").write_text(
        "GARAGE_TEST_DOTENV_CWD=cwd\nGARAGE_TEST_DOTENV_ORDER=cwd\nGARAGE_TEST_DOTENV_EXISTING=from-file\n"
    )
    (home / ".garage" / ".env").write_text(
        "GARAGE_TEST_DOTENV_HOME=home\nGARAGE_TEST_DOTENV_ORDER=home\nGARAGE_TEST_DOTENV_EXISTING=from-home-file\n"
    )
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("GARAGE_TEST_DOTENV_CWD", raising=False)
    monkeypatch.delenv("GARAGE_TEST_DOTENV_HOME", raising=False)
    monkeypatch.delenv("GARAGE_TEST_DOTENV_ORDER", raising=False)
    monkeypatch.setenv("GARAGE_TEST_DOTENV_EXISTING", "process")

    try:
        assert cli.main(["schemas", "--out", str(tmp_path / "schemas")]) == 0
        assert os.environ["GARAGE_TEST_DOTENV_CWD"] == "cwd"
        assert os.environ["GARAGE_TEST_DOTENV_HOME"] == "home"
        assert os.environ["GARAGE_TEST_DOTENV_ORDER"] == "cwd"
        assert os.environ["GARAGE_TEST_DOTENV_EXISTING"] == "process"
    finally:
        for name in (
            "GARAGE_TEST_DOTENV_CWD",
            "GARAGE_TEST_DOTENV_HOME",
            "GARAGE_TEST_DOTENV_ORDER",
        ):
            os.environ.pop(name, None)
