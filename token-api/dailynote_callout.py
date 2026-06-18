"""Atomic Obsidian daily-note callout region writer.

This module is intentionally FastAPI-free so widgets and tests can use the same
bounded rewrite primitive as the HTTP route.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

# ── Shared per-file write lock ────────────────────────────────────────────────
# Every in-process writer to a given Obsidian note (the 60s callout writer and
# the 30s timer-frontmatter writer both hit today's daily note) must serialize,
# or a read-modify-write from one can clobber the other's update. mtime-guard +
# retry only protects against a *detectable* concurrent change; it does not
# prevent two threads from interleaving their own read→write windows. A process-
# wide lock keyed by the resolved path is the serializer. Both writers run via
# asyncio.to_thread (real OS threads), so a threading lock is the right primitive.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def file_write_lock(path: str | Path) -> threading.Lock:
    """Return the process-wide write lock for ``path`` (canonicalized).

    Callers acquire this around any read-modify-write of the note so the
    callout writer and the frontmatter writer cannot interleave-destroy each
    other. Keyed by the resolved path string so two Path objects for the same
    file share one lock.
    """
    try:
        key = str(Path(path).resolve())
    except OSError:
        key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


CALLOUT_ID_RE = re.compile(r"^[a-z0-9_-]+$")
ALLOWED_CALLOUT_TYPES = {
    "info",
    "success",
    "warning",
    "note",
    "tip",
    "abstract",
    "example",
}
MAX_CONTENT_BYTES = 10 * 1024


class CalloutError(ValueError):
    """Base validation error for callout writer input."""


class CalloutConflictError(RuntimeError):
    """Raised when the target note changes during both write attempts."""


@dataclass(frozen=True)
class CalloutWriteResult:
    action: str
    bytes_written: int
    path: Path


def validate_callout(callout_id: str, content: str, callout_type: str) -> None:
    if not CALLOUT_ID_RE.fullmatch(callout_id or ""):
        raise CalloutError("callout_id must match [a-z0-9_-]+")
    if callout_type not in ALLOWED_CALLOUT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_CALLOUT_TYPES))
        raise CalloutError(f"callout_type must be one of: {allowed}")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise CalloutError(f"content exceeds {MAX_CONTENT_BYTES} bytes")


def render_callout_block(
    callout_id: str,
    content: str,
    title: str | None = None,
    callout_type: str = "info",
) -> str:
    """Render a managed Obsidian callout block including invisible markers."""
    title = title or callout_id.upper()
    validate_callout(callout_id, content, callout_type)

    body_lines = content.splitlines()
    quoted_lines = [f"> [!{callout_type}]+ {title}"]
    for line in body_lines:
        quoted_lines.append(">" if line == "" else f"> {line}")

    return "\n".join(
        [
            f"<!-- callout:{callout_id} BEGIN -->",
            *quoted_lines,
            f"<!-- callout:{callout_id} END -->",
        ]
    )


def _replace_or_append(existing: str, callout_id: str, block: str) -> tuple[str, str]:
    begin = f"<!-- callout:{callout_id} BEGIN -->"
    end = f"<!-- callout:{callout_id} END -->"
    begin_idx = existing.find(begin)
    end_idx = existing.find(end)

    if begin_idx >= 0 and end_idx >= 0 and end_idx > begin_idx:
        end_idx += len(end)
        updated = existing[:begin_idx] + block + existing[end_idx:]
        return updated, "replaced"

    if begin_idx >= 0 or end_idx >= 0:
        raise CalloutError(f"malformed callout marker pair for {callout_id!r}")

    sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
    if existing.endswith("\n") and not existing.endswith("\n\n"):
        sep = "\n"
    updated = f"{existing}{sep}{block}\n"
    return updated, "appended"


def _atomic_write(path: Path, content: str, expected_mtime_ns: int) -> int:
    current_mtime_ns = path.stat().st_mtime_ns
    if current_mtime_ns != expected_mtime_ns:
        raise CalloutConflictError("daily note changed during callout write")

    encoded = content.encode("utf-8")
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
            tmp_name = tmp.name
            tmp.write(encoded)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        return len(encoded)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def apply_callout(
    file_path: str | Path,
    callout_id: str,
    content: str,
    title: str | None = None,
    callout_type: str = "info",
    *,
    max_attempts: int = 2,
) -> CalloutWriteResult:
    """Replace or append a managed callout block in ``file_path`` atomically.

    The file must already exist. If it changes between read and replace, the
    operation retries once by default, then raises ``CalloutConflictError``.
    """
    path = Path(file_path)
    block = render_callout_block(callout_id, content, title, callout_type)

    # Lazy import keeps import-time light (mirrors update_frontmatter's pattern).
    from vault_lock import file_flock

    # Lock ordering: cross-process flock → in-process threading lock → RMW. The
    # flock serializes against other processes (the `obsidian` CLI); the threading
    # lock serializes against other in-process writers to this note.
    with file_flock(path), file_write_lock(path):
        last_conflict: CalloutConflictError | None = None
        for _attempt in range(max_attempts):
            stat = path.stat()  # FileNotFoundError intentionally bubbles to the API as 404.
            existing = path.read_text(encoding="utf-8")
            updated, action = _replace_or_append(existing, callout_id, block)
            # Write-skip: if the rendered output is byte-identical to what's on
            # disk, return without touching the file so mtime never moves (no
            # spurious Obsidian-sync re-trigger / collision window).
            if updated == existing:
                return CalloutWriteResult(action="unchanged", bytes_written=0, path=path)
            try:
                bytes_written = _atomic_write(path, updated, stat.st_mtime_ns)
                return CalloutWriteResult(action=action, bytes_written=bytes_written, path=path)
            except CalloutConflictError as exc:
                last_conflict = exc
                continue

    raise last_conflict or CalloutConflictError("daily note changed during callout write")
