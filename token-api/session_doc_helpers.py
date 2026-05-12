"""Session doc frontmatter read/write utility.

Hybrid approach:
- Batch frontmatter mutations use PyYAML (parse, update N fields, write once)
- Single-property ops and note read/append/create use the obsidian CLI

All Obsidian note interactions should go through this module.
"""

import asyncio
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_IMPERIUM_ROOT = Path(os.environ.get("IMPERIUM", "/Volumes/Imperium"))
if not _IMPERIUM_ROOT.exists():
    _IMPERIUM_ROOT = Path.home()
_VAULT_ROOT = _IMPERIUM_ROOT / "Imperium-ENV"
TERRA_SESSIONS_DIR = _VAULT_ROOT / "Terra" / "Sessions"
MARS_SESSIONS_DIR = _VAULT_ROOT / "Mars" / "Sessions"
DAILY_NOTES_DIR = _IMPERIUM_ROOT / "Imperium-ENV" / "Terra" / "Journal" / "Daily"


class _ObsidianDumper(yaml.SafeDumper):
    """YAML dumper that doesn't quote Obsidian wikilinks or colons in strings."""

    pass


def _str_representer(dumper, data):
    """Represent strings without unnecessary quoting.

    PyYAML's SafeDumper quotes strings containing [ ] : etc.
    Obsidian wikilinks like [[Note Name]] need to stay unquoted.
    We use literal style only for multiline, and plain style where safe.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    # Let PyYAML decide, but prefer double-quote over single-quote when needed
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ObsidianDumper.add_representer(str, _str_representer)


def read_frontmatter(file_path: Path) -> tuple[dict[str, Any], str]:
    """Read a markdown file and return (frontmatter_dict, body_content).

    Returns ({}, full_content) if no frontmatter fences found.
    """
    content = file_path.read_text(encoding="utf-8")
    return parse_frontmatter(content)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse frontmatter from a markdown string.

    Returns (frontmatter_dict, body_content).
    Body includes everything after the closing --- fence (with its leading newline stripped once).
    """
    if not content.startswith("---"):
        return {}, content

    # Find closing fence — must be on its own line
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return {}, content

    yaml_block = content[3:end_idx].strip()
    # Body starts after the closing ---\n
    body_start = end_idx + 4  # skip \n---
    if body_start < len(content) and content[body_start] == "\n":
        body_start += 1  # skip the newline after closing ---

    body = content[body_start:]

    try:
        fm = yaml.safe_load(yaml_block)
        if not isinstance(fm, dict):
            return {}, content
    except yaml.YAMLError:
        return {}, content

    return fm, body


def serialize_frontmatter(fm: dict[str, Any], body: str) -> str:
    """Serialize frontmatter dict + body back into a markdown string.

    Preserves body content exactly. Uses yaml.dump with settings tuned
    for Obsidian-compatible output (no trailing ..., flow style for short lists).
    """
    yaml_str = yaml.dump(
        fm,
        Dumper=_ObsidianDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=200,
    ).rstrip("\n")

    # Reassemble
    if body and not body.startswith("\n"):
        return f"---\n{yaml_str}\n---\n\n{body}"
    return f"---\n{yaml_str}\n---\n{body}"


def update_frontmatter(
    file_path: Path,
    updates: dict[str, Any],
    delete_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Read a session doc, merge updates into frontmatter, write back.

    Args:
        file_path: Path to the markdown file.
        updates: Key-value pairs to set/overwrite in frontmatter.
        delete_keys: Keys to remove from frontmatter (applied after updates).

    Returns the updated frontmatter dict.
    Raises FileNotFoundError if the file doesn't exist.
    """
    fm, body = read_frontmatter(file_path)
    fm.update(updates)
    if delete_keys:
        for key in delete_keys:
            fm.pop(key, None)
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


def update_victory_frontmatter(
    file_path: Path,
    victory_reason: str,
    end_time: str,
    deliverables: list[str] | None = None,
) -> dict[str, Any]:
    """Specialized victory update: sets victory fields and computes duration.

    Args:
        file_path: Path to the session doc markdown file.
        victory_reason: Why victory was declared.
        end_time: ISO 8601 timestamp for session end.
        deliverables: Optional list of deliverable descriptions.

    Returns the updated frontmatter dict.
    """
    fm, body = read_frontmatter(file_path)

    updates = {
        "victory": "declared",
        "victory_reason": victory_reason,
        "end_time": end_time,
        "status": "completed",
    }

    # Compute duration if start_time is present
    start_time = fm.get("start_time")
    if start_time:
        try:
            from datetime import datetime

            if isinstance(start_time, str):
                # Handle both with and without timezone
                st = datetime.fromisoformat(start_time)
                et = datetime.fromisoformat(end_time)
                delta = et - st
                updates["duration_minutes"] = round(delta.total_seconds() / 60)
        except (ValueError, TypeError):
            pass  # Can't compute, skip

    if deliverables is not None:
        updates["deliverables"] = deliverables

    fm.update(updates)
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


# ============ Obsidian CLI Wrappers ============
# For single-property ops, note reads, appends, creates — thin wrappers
# around the obsidian CLI. These shell out to the CLI which handles
# cross-platform differences (WSL proxies to Obsidian.exe, macOS uses filesystem).


def _obsidian_cmd(vault: str, command: str, **kwargs) -> list[str]:
    """Build an obsidian CLI command list."""
    cmd = ["obsidian", f"vault={vault}", command]
    for key, value in kwargs.items():
        cmd.append(f"{key}={value}")
    return cmd


def obsidian_property_set(vault: str, path: str, prop: str, value: str) -> bool:
    """Set a single frontmatter property via the obsidian CLI (sync)."""
    try:
        result = subprocess.run(
            _obsidian_cmd(vault, "property:set", path=path, property=prop, value=value),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"obsidian property:set failed: {e}")
        return False


def obsidian_property_read(vault: str, path: str, prop: str) -> str | None:
    """Read a single frontmatter property via the obsidian CLI (sync)."""
    try:
        result = subprocess.run(
            _obsidian_cmd(vault, "property:read", path=path, property=prop),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        logger.warning(f"obsidian property:read failed: {e}")
        return None


def obsidian_read(vault: str, path: str) -> str | None:
    """Read a note's full content via the obsidian CLI (sync)."""
    try:
        result = subprocess.run(
            _obsidian_cmd(vault, "read", path=path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception as e:
        logger.warning(f"obsidian read failed: {e}")
        return None


def obsidian_append(vault: str, path: str, content: str) -> bool:
    """Append content to a note's body via the obsidian CLI (sync)."""
    try:
        result = subprocess.run(
            _obsidian_cmd(vault, "append", path=path, content=content),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"obsidian append failed: {e}")
        return False


def obsidian_create(vault: str, path: str, content: str) -> bool:
    """Create a new note via the obsidian CLI (sync). Returns False if it already exists."""
    try:
        result = subprocess.run(
            _obsidian_cmd(vault, "create", path=path, content=content),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"obsidian create failed: {e}")
        return False


# Async variants — run CLI calls off the event loop


async def async_obsidian_property_set(vault: str, path: str, prop: str, value: str) -> bool:
    """Set a single frontmatter property via obsidian CLI (async)."""
    return await asyncio.to_thread(obsidian_property_set, vault, path, prop, value)


async def async_obsidian_property_read(vault: str, path: str, prop: str) -> str | None:
    """Read a single frontmatter property via obsidian CLI (async)."""
    return await asyncio.to_thread(obsidian_property_read, vault, path, prop)


async def async_obsidian_read(vault: str, path: str) -> str | None:
    """Read a note's full content via obsidian CLI (async)."""
    return await asyncio.to_thread(obsidian_read, vault, path)


async def async_obsidian_append(vault: str, path: str, content: str) -> bool:
    """Append content to a note body via obsidian CLI (async)."""
    return await asyncio.to_thread(obsidian_append, vault, path, content)


async def async_obsidian_create(vault: str, path: str, content: str) -> bool:
    """Create a new note via obsidian CLI (async)."""
    return await asyncio.to_thread(obsidian_create, vault, path, content)


# ============ Session Doc File Management ============


def create_session_doc_file(
    file_path: Path, title: str, doc_id: int, project: str = None, primarch_name: str = None
) -> None:
    """Create the markdown file for a session document."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    project_line = f"\nproject: {project}" if project else ""
    primarch_line = f"\nprimarch: {primarch_name}" if primarch_name else ""
    content = f"""---
session_doc_id: {doc_id}
created: {today}{project_line}
agents: []
instance_ids: []{primarch_line}
status: active
type: session
start_time: null
end_time: null
duration_minutes: null
pool: null
legion: null
faction: null
victory_conditions: []
victory: pending
victory_reason: null
deliverables: []
instance_type: one_off
zealotry: 4
---

# Session: {title}

## Plan

_No plan defined yet._

## Activity Log

"""
    file_path.write_text(content)


async def _update_doc_agents_list(db, doc_id: int) -> None:
    """Update the agents list, instance_ids, and primarch in a session doc's YAML frontmatter."""
    cursor = await db.execute(
        "SELECT id, tab_name FROM claude_instances WHERE session_doc_id = ? AND status IN ('processing', 'idle')",
        (doc_id,),
    )
    rows = await cursor.fetchall()
    agents = [r[1] for r in rows if r[1]]
    instance_ids = [r[0] for r in rows if r[0]]

    cursor = await db.execute(
        "SELECT file_path, primarch_name FROM session_documents WHERE id = ?", (doc_id,)
    )
    doc_row = await cursor.fetchone()
    if not doc_row:
        return

    fp = Path(doc_row[0])
    if not fp.exists():
        return

    primarch_name = doc_row[1]

    updates = {
        "agents": agents,
        "instance_ids": instance_ids,
    }
    if primarch_name:
        updates["primarch"] = primarch_name
    delete_keys = ["primarch"] if not primarch_name else None

    await asyncio.to_thread(update_frontmatter, fp, updates, delete_keys)


async def resolve_or_create_session_doc_for_path(db, file_path: Path) -> int | None:
    """Resolve a session_documents row for an existing markdown file.

    If the note already has a DB row, return it and backfill the frontmatter
    `session_doc_id` when missing or stale. If not, create a DB row from the
    note's existing frontmatter and then backfill the ID into the note.
    """
    fp = file_path.resolve()
    if not fp.exists():
        return None

    cursor = await db.execute("SELECT id FROM session_documents WHERE file_path = ?", (str(fp),))
    existing = await cursor.fetchone()
    if existing:
        doc_id = existing[0]
        fm, _ = await asyncio.to_thread(read_frontmatter, fp)
        if fm.get("session_doc_id") != doc_id:
            await asyncio.to_thread(update_frontmatter, fp, {"session_doc_id": doc_id})
        return doc_id

    fm, _ = await asyncio.to_thread(read_frontmatter, fp)
    doc_title = fm.get("title") or fp.stem.replace("-", " ")
    doc_project = fm.get("project")
    doc_status = fm.get("status") or "active"
    now_ts = datetime.now().isoformat()
    cursor = await db.execute(
        """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (doc_title, str(fp), doc_project, doc_status, now_ts, now_ts),
    )
    doc_id = cursor.lastrowid
    await asyncio.to_thread(update_frontmatter, fp, {"session_doc_id": doc_id})
    return doc_id


async def resolve_active_primarch_session_doc(db, primarch_name: str) -> int | None:
    """Return the currently linked session doc for a primarch, if any."""
    cursor = await db.execute(
        "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
        (primarch_name,),
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def resolve_today_daily_note_session_doc(db, date_str: str | None = None) -> int | None:
    """Return today's daily note as a session_documents row if the note exists."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return await resolve_or_create_session_doc_for_path(db, DAILY_NOTES_DIR / f"{date_str}.md")


async def resolve_session_doc_for_start(
    db,
    *,
    dispatch_session_doc_path: str | None,
    primarch_name: str | None,
    origin_type: str,
    cron_job_id: str | None,
    cron_job_name: str | None,
    working_dir: str | None,
    is_subagent: bool,
) -> tuple[int | None, str | None]:
    """Resolve launch-time session doc ownership with explicit precedence.

    Precedence:
    1. explicit dispatch doc
    2. Custodes daily note
    3. active primarch doc
    4. active cron doc (or create one)
    5. generic interactive doc (top-level only)
    """
    if dispatch_session_doc_path:
        fp = Path(dispatch_session_doc_path)
        if not fp.is_absolute():
            fp = _VAULT_ROOT / dispatch_session_doc_path
        doc_id = await resolve_or_create_session_doc_for_path(db, fp)
        if doc_id:
            return doc_id, "dispatch_explicit"

    if primarch_name == "custodes":
        doc_id = await resolve_today_daily_note_session_doc(db)
        if doc_id:
            return doc_id, "daily_note_custodes"
        logger.warning("Custodes launch had no daily note to bind; falling through to other policy")

    if primarch_name:
        doc_id = await resolve_active_primarch_session_doc(db, primarch_name)
        if doc_id:
            return doc_id, "primarch_active"

    if origin_type == "cron":
        if cron_job_id:
            cursor = await db.execute(
                "SELECT id FROM session_documents WHERE cron_job_id = ? AND status = 'active'",
                (cron_job_id,),
            )
            existing = await cursor.fetchone()
            if existing:
                return existing[0], "cron_active"

        now_ts = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")
        doc_title = cron_job_name or "cron"
        slug = doc_title.lower().replace(" ", "-")[:50]
        fp = MARS_SESSIONS_DIR / f"{today}-{slug}.md"
        counter = 1
        while fp.exists():
            fp = MARS_SESSIONS_DIR / f"{today}-{slug}-{counter}.md"
            counter += 1
        cursor = await db.execute(
            """INSERT INTO session_documents (title, file_path, project, cron_job_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (doc_title, str(fp), None, cron_job_id, now_ts, now_ts),
        )
        doc_id = cursor.lastrowid
        create_session_doc_file(fp, doc_title, doc_id)
        return doc_id, "cron_created"

    if is_subagent:
        return None, None

    today = datetime.now().strftime("%Y-%m-%d")
    now_ts = datetime.now().isoformat()
    cwd_basename = Path(working_dir).name if working_dir else "session"
    doc_title = f"{cwd_basename} {today}"
    slug = doc_title.lower().replace(" ", "-")[:50]
    fp = TERRA_SESSIONS_DIR / f"{today}-{slug}.md"
    counter = 1
    while fp.exists():
        fp = TERRA_SESSIONS_DIR / f"{today}-{slug}-{counter}.md"
        counter += 1
    cursor = await db.execute(
        """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (doc_title, str(fp), None, now_ts, now_ts),
    )
    doc_id = cursor.lastrowid
    create_session_doc_file(fp, doc_title, doc_id)
    return doc_id, "interactive_auto"
