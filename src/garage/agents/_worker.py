"""Detached, standard-library-only supervisor for a local CLI run."""

import os
import sys
import json
import time
import signal
import subprocess
from pathlib import Path


def record_exit(directory: Path, code: int) -> None:
    """Publish a complete exit marker atomically to concurrent readers."""
    temporary = directory / "exit_code.tmp"
    temporary.write_text(str(code))
    temporary.replace(directory / "exit_code")


def main() -> None:
    """Run the persisted command and always record its exit when possible."""
    directory = Path(sys.argv[1])
    config = json.loads((directory / "run.json").read_text())
    signal.signal(signal.SIGINT, lambda *_: None)
    signal.signal(signal.SIGTERM, lambda *_: None)
    with (directory / "events.jsonl").open("ab", buffering=0) as stdout:
        with (directory / "stderr.log").open("ab", buffering=0) as stderr:
            try:
                process = subprocess.Popen(config["command"], cwd=config["worktree"], stdout=stdout, stderr=stderr)
                remaining = max(0.01, config["timeout_s"] - (time.time() - config["started_at"]))
                try:
                    code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    (directory / "expired").touch()
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        os.killpg(os.getpgrp(), sig)
                        try:
                            process.wait(timeout=config["grace_s"])
                        except subprocess.TimeoutExpired:
                            pass
                    record_exit(directory, process.poll() or -9)
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                    return
            except OSError as exc:
                stderr.write(f"Could not start CLI: {exc}\n".encode())
                code = 127
            record_exit(directory, code)


if __name__ == "__main__":
    main()
