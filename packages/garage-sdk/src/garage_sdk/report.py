"""Test-run report schemas and persistence."""

import os
import tempfile
from typing import Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict


CheckStatus = Literal["pass", "fail", "skip"]
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_COULD_NOT_RUN = 2


class CheckResult(BaseModel):
    """Outcome and timing for one test-kit check."""

    name: str
    status: CheckStatus
    detail: str = ""
    seconds: float = 0.0


class TestReport(BaseModel):
    """Structured result of a test-kit run."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    run_id: str
    version: str | None = None
    seconds: float
    checks: list[CheckResult]
    app_restored: bool | None = None


def exit_code(report: TestReport) -> int:
    """Map the report result to pass, fail, or could-not-run process status."""
    if not report.checks or all(check.status == "skip" for check in report.checks):
        return EXIT_COULD_NOT_RUN
    return EXIT_PASS if report.ok else EXIT_FAIL


def write_report(report: TestReport, path: Path) -> None:
    """Atomically write a JSON report to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(report.model_dump_json() + "\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_report(path: Path) -> TestReport:
    """Read a JSON report from path."""
    return TestReport.model_validate_json(path.read_text(encoding="utf-8"))
