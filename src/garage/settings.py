"""Garage runtime configuration."""

import os
from pathlib import Path
from dataclasses import dataclass

from garage_sdk.manifest import Manifest, load_manifest
from garage.incidents.base import parse_duration


@dataclass(frozen=True)
class GarageSettings:
    """Resolved manifest and environment settings for Garage."""

    manifest: Manifest
    manifest_dir: Path
    repo: str
    events_dir: Path | None
    retention_days: int
    rules_path: Path | None
    detector: str
    data_dir: Path
    report: bool
    dispatch: bool
    github_token: str | None
    max_new_issues_per_day: int
    devin_api_key: str | None
    devin_org_id: str | None
    devin_api_base_url: str
    devin_max_acu: int
    dispatch_interval_s: float
    outpost: str | None
    lookback_s: float
    poll_interval_s: float
    host: str
    port: int
    bridge_token: str | None


def _duration_env(name: str, default: str) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        value = default
    try:
        return parse_duration(value.strip()).total_seconds()
    except ValueError as exc:
        raise ValueError(f"{name} must be a duration such as 30m, 1h or 6h") from exc


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def _manifest_relative_path(value: str | None, manifest_dir: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else manifest_dir / path


def settings_from_env(manifest_path: Path | None = None) -> GarageSettings:
    """Load settings from a manifest and GARAGE environment variables."""
    selected_manifest = manifest_path or Path(os.getenv("GARAGE_MANIFEST") or "./garage.toml")
    selected_manifest = selected_manifest.expanduser().resolve()
    manifest = load_manifest(selected_manifest)
    manifest_dir = selected_manifest.parent

    events_value = os.getenv("GARAGE_EVENTS_DIR") or manifest.signals.path
    if events_value is None:
        events_dir = None
    else:
        events_dir = _manifest_relative_path(events_value, manifest_dir)

    data_dir = Path(os.getenv("GARAGE_DATA_DIR", "~/.garage")).expanduser()
    rules_path = _manifest_relative_path(manifest.detect.rules, manifest_dir)
    lookback_s = _duration_env("GARAGE_LOOKBACK", "24h")
    poll_interval_s = _duration_env("GARAGE_POLL_INTERVAL", "30s")

    return GarageSettings(
        manifest=manifest,
        manifest_dir=manifest_dir,
        repo=os.getenv("GARAGE_REPO") or manifest.app.repo,
        events_dir=events_dir,
        retention_days=manifest.signals.retention_days,
        rules_path=rules_path,
        detector=manifest.detect.detector,
        data_dir=data_dir,
        report=_bool_env("GARAGE_REPORT", False),
        dispatch=_bool_env("GARAGE_DISPATCH", False),
        github_token=os.getenv("GARAGE_GITHUB_TOKEN") or None,
        max_new_issues_per_day=_int_env("GARAGE_MAX_NEW_ISSUES_PER_DAY", manifest.report.max_new_issues_per_day),
        devin_api_key=os.getenv("DEVIN_API_KEY") or None,
        devin_org_id=os.getenv("DEVIN_ORG_ID") or None,
        devin_api_base_url=os.getenv("DEVIN_API_BASE_URL", "https://api.devin.ai"),
        devin_max_acu=_int_env("GARAGE_DEVIN_MAX_ACU", 5),
        dispatch_interval_s=_duration_env("GARAGE_DISPATCH_INTERVAL", manifest.dispatch.interval),
        outpost=manifest.dispatch.outpost,
        lookback_s=lookback_s,
        poll_interval_s=poll_interval_s,
        host=os.getenv("GARAGE_HOST", "127.0.0.1"),
        port=_int_env("GARAGE_PORT", 8770),
        bridge_token=os.getenv("GARAGE_BRIDGE_TOKEN") or None,
    )
