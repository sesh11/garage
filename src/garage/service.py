"""Run Garage as a macOS LaunchAgent and update the installed tool."""

import os
import sys
import shutil
import string
import platform
import plistlib
import subprocess
from pathlib import Path
from dataclasses import dataclass
from xml.sax.saxutils import escape
from importlib.resources import files


LABEL = "ai.garage.serve"
DEFAULT_PORT = 8770
DEFAULT_SOURCE = "git+https://github.com/sesh11/garage"
DEFAULT_REF = "main"

_UV_CANDIDATES = (Path("~/.local/bin/uv"), Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"))


@dataclass(frozen=True)
class ServicePaths:
    """Machine-specific values substituted into the LaunchAgent template."""

    garage_bin: Path
    projects_file: Path
    data_dir: Path
    port: int
    home: Path

    @property
    def log_dir(self) -> Path:
        """Return the LaunchAgent log directory."""
        return self.home / "Library" / "Logs" / "garage"

    @property
    def plist_path(self) -> Path:
        """Return where the LaunchAgent plist is installed."""
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def path(self) -> str:
        """Return PATH for the agent so git, uv, claude and codex resolve."""
        entries = [
            str(self.garage_bin.parent),
            str(self.home / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        return ":".join(dict.fromkeys(entries))


def resolve_paths(
    garage_bin: Path | None = None,
    projects_file: Path | None = None,
    data_dir: Path | None = None,
    port: int = DEFAULT_PORT,
    home: Path | None = None,
) -> ServicePaths:
    """Resolve absolute paths for the LaunchAgent."""
    resolved_home = (home or Path.home()).expanduser().resolve()
    return ServicePaths(
        garage_bin=(garage_bin or Path(sys.executable).parent / "garage").expanduser().resolve(),
        projects_file=(projects_file or resolved_home / ".garage" / "projects.toml").expanduser().resolve(),
        data_dir=(data_dir or resolved_home / ".garage").expanduser().resolve(),
        port=port,
        home=resolved_home,
    )


def render_plist(paths: ServicePaths) -> str:
    """Render the LaunchAgent plist and check that it parses as a property list."""
    template = files("garage").joinpath("launchd", f"{LABEL}.plist").read_text(encoding="utf-8")
    values = {
        "label": LABEL,
        "garage_bin": str(paths.garage_bin),
        "projects_file": str(paths.projects_file),
        "path": paths.path,
        "home": str(paths.home),
        "data_dir": str(paths.data_dir),
        "port": str(paths.port),
        "log_dir": str(paths.log_dir),
    }
    content = string.Template(template).substitute({key: escape(value) for key, value in values.items()})
    plistlib.loads(content.encode("utf-8"))
    return content


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _checked(args: list[str], cwd: Path | None = None) -> str:
    result = _run(args, cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"`{' '.join(args)}` failed: {detail}")
    return result.stdout.strip()


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("LaunchAgents are macOS-only; use --dry-run to preview the plist")


def install(paths: ServicePaths, *, dry_run: bool = False) -> None:
    """Write the plist and (re)load it into launchd."""
    content = render_plist(paths)
    if dry_run:
        print(f"# {paths.plist_path}\n{content}", end="")
        return
    _require_macos()
    if not paths.garage_bin.is_file():
        raise FileNotFoundError(f"{paths.garage_bin} not found; run install.sh; see README")
    if not paths.projects_file.is_file():
        raise FileNotFoundError(
            f"{paths.projects_file} not found; create the projects file or run install.sh; see README"
        )
    env_file = paths.home / ".garage" / ".env"
    if not env_file.is_file():
        print(f"warning: {env_file} not found; Garage will run without credentials", file=sys.stderr)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    paths.plist_path.parent.mkdir(parents=True, exist_ok=True)
    paths.plist_path.write_text(content, encoding="utf-8")
    _run(["launchctl", "bootout", _target()])
    _checked(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(paths.plist_path)])
    print(f"installed {LABEL} -> {paths.plist_path}")
    print(f"serving http://127.0.0.1:{paths.port}; logs: {paths.log_dir / 'serve.log'}")


def uninstall(paths: ServicePaths, *, dry_run: bool = False) -> None:
    """Stop the agent and delete its plist."""
    if dry_run:
        print(f"would run `launchctl bootout {_target()}` and remove {paths.plist_path}")
        return
    _require_macos()
    _run(["launchctl", "bootout", _target()])
    paths.plist_path.unlink(missing_ok=True)
    print(f"removed {LABEL}")


def status() -> int:
    """Print launchd's view of the agent."""
    _require_macos()
    result = _run(["launchctl", "print", _target()])
    print(result.stdout or result.stderr, end="")
    return result.returncode


def restart() -> None:
    """Restart the installed LaunchAgent."""
    _require_macos()
    _checked(["launchctl", "kickstart", "-k", _target()])
    print(f"restarted {LABEL}")


def find_uv() -> str:
    """Locate uv on PATH or in its usual install locations."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in _UV_CANDIDATES:
        if candidate.expanduser().is_file():
            return str(candidate.expanduser())
    raise FileNotFoundError("uv not found; install uv first")


def update(source: str, ref: str, *, restart_service: bool = True) -> None:
    """Reinstall the Garage tool from its source and optionally restart its agent."""
    _checked([find_uv(), "tool", "install", "--reinstall", f"{source}@{ref}"])
    if not restart_service:
        print("not restarting; restart Garage to run the new code")
    elif platform.system() == "Darwin" and resolve_paths().plist_path.is_file():
        restart()
    else:
        print(f"{LABEL} is not installed here; restart Garage yourself")
