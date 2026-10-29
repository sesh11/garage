"""Resource identity stamps for events."""

import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Resource(BaseModel):
    """Identify the application installation that emitted an event."""

    model_config = ConfigDict(frozen=True)

    app: str
    version: str
    install: str


def version_stamp(version: str, commit: str | None) -> str:
    """Append a short commit identifier when one is available."""
    if commit is None:
        return version
    return f"{version}+{commit[:7]}"


def install_id(path: Path) -> str:
    """Read or atomically create a stable random installation identifier."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        return uuid.uuid4().hex[:8]

    identifier = uuid.uuid4().hex[:8]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(identifier + "\n")
        return identifier
    except FileExistsError:
        try:
            existing = path.read_text(encoding="utf-8").strip()
            return existing or identifier
        except (OSError, UnicodeError):
            return identifier
    except OSError:
        return identifier
