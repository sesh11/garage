"""Garage SDK resource, manifest, and report tests."""

import re
import ast
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone

import pytest

from garage_sdk import EventLog, configure
from garage_sdk.report import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_COULD_NOT_RUN,
    CheckResult,
    exit_code,
    read_report,
    write_report,
)
from garage_sdk.report import (
    TestReport as GarageTestReport,
)
from garage_sdk.manifest import PolicySection, load_manifest
from garage_sdk.resource import install_id, version_stamp
from garage_sdk.logging_bridge import EventLoggingHandler


@pytest.fixture(autouse=True)
def reset_event_log():
    configure(None)
    yield
    configure(None)


def test_install_id_is_persisted(tmp_path) -> None:
    path = tmp_path / "state" / "install-id"

    first = install_id(path)
    second = install_id(path)

    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert second == first


def test_version_stamp_appends_short_commit() -> None:
    assert version_stamp("0.2.0", "8bc8ef5deadbeef") == "0.2.0+8bc8ef5"
    assert version_stamp("0.2.0", None) == "0.2.0"


def test_logging_bridge_default_and_custom_prefixes(tmp_path) -> None:
    event_log = EventLog(tmp_path / "events", clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))
    configure(event_log)
    default_handler = EventLoggingHandler()
    custom_handler = EventLoggingHandler(ignore_prefixes=("legacy",))

    default_handler.emit(logging.LogRecord("ambient_home.app", logging.ERROR, "", 1, "ignored", (), None))
    default_handler.emit(logging.LogRecord("garage_sdk.events", logging.ERROR, "", 1, "ignored", (), None))
    default_handler.emit(logging.LogRecord("third_party.client", logging.ERROR, "", 1, "included", (), None))
    custom_handler.emit(logging.LogRecord("ambient_home.app", logging.ERROR, "", 1, "custom", (), None))
    custom_handler.emit(logging.LogRecord("legacy.client", logging.ERROR, "", 1, "ignored", (), None))

    events = event_log.read(datetime(2025, 1, 1, tzinfo=timezone.utc))
    assert [event.component for event in events] == ["third_party.client", "ambient_home.app"]


def test_manifest_loads_ambient_home_example() -> None:
    root = Path(__file__).parents[3]

    manifest = load_manifest(root / "examples" / "ambient-home" / "garage.toml")

    assert manifest.app.id == "ambient-home"
    assert manifest.app.repo == "sesh11/ambient-home"
    assert manifest.signals.retention_days == 14
    assert manifest.detect.rules == "rules.toml"
    assert manifest.test_kit[0].name == "robot"
    assert manifest.test_kit[0].instructions is not None
    assert manifest.policy.needs_approval == ["triage", "plan", "release"]


@pytest.mark.parametrize(
    "policy",
    [
        {"needs_approval": ["triage", "plan", "release"], "auto_release": True},
        {"needs_approval": ["triage", "plan"], "auto_release": False},
        {"needs_approval": ["triage", "release"], "auto_release": False},
    ],
)
def test_policy_rejects_auto_release_or_missing_approval(policy) -> None:
    with pytest.raises(ValueError, match="v0 requires human approval for plan and release"):
        PolicySection.model_validate(policy)


def test_report_parses_ambient_hardware_test_payload(tmp_path) -> None:
    payload = {
        "ok": True,
        "run_id": "run-123",
        "seconds": 8.5,
        "app_restored": True,
        "checks": [{"name": "robot", "status": "pass", "detail": "all good", "seconds": 8.5}],
        "future_field": {"supported": True},
    }

    report = GarageTestReport.model_validate(payload)
    path = tmp_path / "reports" / "latest.json"
    write_report(report, path)

    restored = read_report(path)
    assert restored.app_restored is True
    assert restored.model_extra == {"future_field": {"supported": True}}
    assert exit_code(restored) == EXIT_PASS


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            GarageTestReport(ok=False, run_id="failed", seconds=1, checks=[CheckResult(name="x", status="fail")]),
            EXIT_FAIL,
        ),
        (GarageTestReport(ok=True, run_id="empty", seconds=0, checks=[]), EXIT_COULD_NOT_RUN),
        (
            GarageTestReport(
                ok=True,
                run_id="skipped",
                seconds=0,
                checks=[CheckResult(name="x", status="skip")],
            ),
            EXIT_COULD_NOT_RUN,
        ),
    ],
)
def test_report_exit_codes(report, expected) -> None:
    assert exit_code(report) == expected


def test_sdk_imports_stay_within_stdlib_and_pydantic() -> None:
    source_dir = Path(__file__).parents[1] / "src" / "garage_sdk"
    allowed_roots = set(sys.stdlib_module_names) | {"pydantic"}

    for path in source_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".", maxsplit=1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                imported = [(node.module or "").split(".", maxsplit=1)[0]]
            else:
                continue
            assert set(imported) <= allowed_roots, f"{path} imports outside the SDK boundary: {imported}"
