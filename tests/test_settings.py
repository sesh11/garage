# mypy: disable-error-code="no-untyped-def"

from pathlib import Path

import pytest
from conftest import example_manifest

from garage.settings import settings_from_env


def test_manifest_paths_and_garage_environment_override(monkeypatch) -> None:
    for name in ("GARAGE_REPO", "GARAGE_EVENTS_DIR", "GARAGE_DATA_DIR", "GARAGE_REPORT", "GARAGE_DISPATCH"):
        monkeypatch.delenv(name, raising=False)
    settings = settings_from_env(example_manifest())
    assert settings.repo == "sesh11/ambient-home"
    assert settings.rules_path == example_manifest().parent / "rules.toml"
    assert settings.events_dir is not None and settings.events_dir.is_absolute()
    assert settings.dispatch_interval_s == 3600
    monkeypatch.setenv("GARAGE_REPO", "example/service")
    monkeypatch.setenv("GARAGE_EVENTS_DIR", "events")
    monkeypatch.setenv("GARAGE_DATA_DIR", "~/garage-data")
    monkeypatch.setenv("GARAGE_REPORT", "yes")
    monkeypatch.setenv("GARAGE_DISPATCH", "1")
    monkeypatch.setenv("GARAGE_MAX_NEW_ISSUES_PER_DAY", "7")
    monkeypatch.setenv("GARAGE_DISPATCH_INTERVAL", "15m")
    overridden = settings_from_env(example_manifest())
    assert overridden.repo == "example/service"
    assert overridden.events_dir == example_manifest().parent / "events"
    assert overridden.data_dir == Path("~/garage-data").expanduser()
    assert overridden.report and overridden.dispatch
    assert overridden.max_new_issues_per_day == 7
    assert overridden.dispatch_interval_s == 900


def test_manifest_argument_precedes_environment_manifest(monkeypatch) -> None:
    monkeypatch.setenv("GARAGE_MANIFEST", "/not/a/manifest.toml")
    settings = settings_from_env(example_manifest())
    assert settings.manifest.app.id == "ambient-home"


def test_legacy_ambient_variables_do_not_configure_garage(monkeypatch) -> None:
    monkeypatch.setenv("AMBIENT_GITHUB_TOKEN", "not-used")
    monkeypatch.setenv("AMBIENT_REPORT", "true")
    monkeypatch.delenv("GARAGE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GARAGE_REPORT", raising=False)
    settings = settings_from_env(example_manifest())
    assert settings.github_token is None
    assert settings.report is False


def test_invalid_boolean_is_reported(monkeypatch) -> None:
    monkeypatch.setenv("GARAGE_REPORT", "maybe")
    with pytest.raises(ValueError, match="GARAGE_REPORT must be a boolean"):
        settings_from_env(example_manifest())
