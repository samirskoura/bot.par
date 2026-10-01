#!/usr/bin/env python3
"""Commit and push encrypted headline reservations before a Snapchat edit.

Called by the worker on GitHub Actions. No credentials, plaintext state, Git
remote URLs or subprocess output are printed. A failed push prevents the edit.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from state_crypto import encrypt


class CheckpointError(RuntimeError):
    pass


def git(root: Path, *arguments: str, allow_failure: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True, timeout=30, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode and not allow_failure:
        raise CheckpointError("Git operation failed")
    return result


def checkpoint(root: Path) -> None:
    root = root.resolve()
    source = root / "state.json"
    destination = root / "state.json.enc"
    if source.is_symlink() or not source.is_file() or destination.is_symlink():
        raise CheckpointError("Invalid state file")
    repository_root = git(root, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if Path(repository_root).resolve() != root:
        raise CheckpointError("State must be in the repository root")
    tracked = git(root, "ls-files", "--error-unmatch", "--", "state.json", allow_failure=True)
    if tracked.returncode != 1:
        raise CheckpointError("Plaintext state must not be tracked")
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.decode().strip()
    if not branch:
        raise CheckpointError("A checked-out branch is required")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".state-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        encrypt(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    git(root, "add", "--", "state.json.enc")
    # --only ensures even unrelated files already staged by someone else are
    # excluded from this commit. Never force-push or resolve conflicts here.
    git(root, "commit", "--only", "-m", "Reserve encrypted Snapchat headline history", "--", "state.json.enc")
    for attempt in range(3):
        try:
            result = git(root, "push", "origin", f"HEAD:refs/heads/{branch}", allow_failure=True)
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        if attempt < 2:
            time.sleep(attempt + 1)
    raise CheckpointError("Encrypted history push could not be confirmed")


def main() -> int:
    try:
        checkpoint(Path.cwd())
    except Exception:
        print("Encrypted history checkpoint failed; no subsequent Snapchat edit is authorized.", file=sys.stderr)
        return 90
    print("Encrypted history checkpoint saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
