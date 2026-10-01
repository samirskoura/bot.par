#!/usr/bin/env python3
"""Recover only the last saved encrypted state after repository files were replaced.

The workflow decrypts the recovered file before making any Snapchat request.
This helper never resets history, falls back past a saved but unreadable state,
or restores old executable code.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


STATE_NAME = "state.json.enc"


class RecoveryError(Exception):
    pass


def git_output(repository: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("Could not read repository history; stopping before Snapchat access.") from exc
    if result.returncode:
        raise RecoveryError("Could not read repository history; stopping before Snapchat access.")
    return result.stdout


def restore(repository: Path) -> bool:
    destination = repository / STATE_NAME
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise RecoveryError("Saved state must be a regular file.")
    if destination.is_file():
        print("Using the current encrypted state file.")
        return False

    if git_output(repository, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise RecoveryError("Full repository history is required to recover state; use fetch-depth: 0.")

    revisions = git_output(repository, "rev-list", "--topo-order", "HEAD", "--", STATE_NAME).decode("ascii").splitlines()
    if not revisions:
        print("No encrypted state exists in this repository's history; starting new history.")
        return False

    for revision in revisions:
        entry = git_output(repository, "ls-tree", "-z", "--full-tree", revision, "--", STATE_NAME)
        if not entry:
            # A deletion commit has no file to restore. The preceding saved
            # revision still belongs to the current branch's ancestry.
            continue
        metadata, separator, filename = entry.rstrip(b"\0").partition(b"\t")
        fields = metadata.split()
        if (
            not separator
            or filename != STATE_NAME.encode("ascii")
            or len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
        ):
            raise RecoveryError("The most recent saved state is not a regular file; stopping.")

        encrypted = git_output(repository, "show", f"{revision}:{STATE_NAME}")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=repository, prefix="state-recovery-", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(encrypted)
            if destination.exists() or destination.is_symlink():
                raise RecoveryError("State appeared during recovery; stopping without overwriting it.")
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        print("Encrypted state restored from repository history.")
        return True

    raise RecoveryError("State history was found but no saved file could be recovered; stopping.")


def main() -> int:
    try:
        restore(Path(__file__).resolve().parent)
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print(
            "ERROR: Encrypted-state recovery failed. Keep repository history and the original "
            "STATE_ENCRYPTION_KEY; no Snapchat request was made by this helper.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
