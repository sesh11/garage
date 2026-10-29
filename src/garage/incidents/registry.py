"""Detector registry and the single detector swap point."""

from pathlib import Path
from collections.abc import Callable

from garage.incidents.base import Detector
from garage.incidents.rules import RuleDetector


DETECTORS: dict[str, Callable[[Path | None], Detector]] = {
    "rules": lambda rules_path: RuleDetector.from_file(rules_path) if rules_path is not None else _missing_rules(),
}


def _missing_rules() -> Detector:
    raise ValueError("the rules detector needs [detect].rules in the manifest")


def build_detector(name: str, rules_path: Path | None) -> Detector:
    """Build the configured detector, reporting registered choices on failure."""
    factory = DETECTORS.get(name)
    if factory is None:
        raise ValueError(f"unknown detector {name!r}; valid names: {', '.join(sorted(DETECTORS))}")
    return factory(rules_path)
