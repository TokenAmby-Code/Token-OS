"""Same-machine advisory file lock for vault writers.

A single ``fcntl.flock`` keyed per vault-note path, taken on a *local* sidecar
lockfile (never the NAS note itself — atomic ``os.replace`` swaps the inode and
SMB flock is unreliable). Both writers — token-api in-process and the separate
``obsidian`` CLI process — derive the lockfile from the same code here, so they
can never disagree on which lock to grab and are genuinely serialized.

Cross-machine coordination is explicitly out of scope: the lock is advisory and
same-host only. Obsidian Sync's merge owns the cross-device case once the vault
moves off the NAS.

Intentionally FastAPI-free, stdlib only, so the bash CLI can shell into the
``__main__`` entrypoint with no heavyweight imports.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_LOCK_DIR_NAME = "imperium-vault-locks"


def _lock_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), _LOCK_DIR_NAME)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def lock_path_for(path: str | Path) -> str:
    """Return the deterministic sidecar lockfile path for ``path``.

    Keyed by ``sha256(realpath(path))[:16]`` so two references to the same note
    (relative vs absolute, symlinked, …) resolve to one lockfile. The lock lives
    under the local tempdir, never beside the NAS note.
    """
    resolved = os.path.realpath(str(path))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_lock_dir(), f"{digest}.lock")


@contextmanager
def file_flock(path: str | Path):
    """Hold an exclusive same-machine flock for ``path`` for the block's life.

    Opens (creating if needed) the sidecar lockfile and takes ``LOCK_EX``,
    blocking until acquired, then releases on exit. Used by token-api writers
    in-process; the bash CLI uses the ``__main__`` entrypoint below.
    """
    lock_file = lock_path_for(path)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _main(argv: list[str]) -> int:
    """CLI: ``vault_lock.py <target-path> -- <cmd> [args…]``.

    Acquire the flock for ``<target-path>``, run ``<cmd>`` to completion with the
    lock held, release, and propagate the child's exit code. This is what the
    ``obsidian`` bash CLI execs into so the lockfile derivation is literally the
    same code as token-api uses in-process.
    """
    if "--" not in argv:
        print(
            "usage: vault_lock.py <target-path> -- <cmd> [args…]",
            file=sys.stderr,
        )
        return 2
    sep = argv.index("--")
    targets = argv[:sep]
    cmd = argv[sep + 1 :]
    if len(targets) != 1 or not cmd:
        print(
            "usage: vault_lock.py <target-path> -- <cmd> [args…]",
            file=sys.stderr,
        )
        return 2

    with file_flock(targets[0]):
        return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
