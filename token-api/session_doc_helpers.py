"""Session doc frontmatter read/write utility.

Hybrid approach:
- Batch frontmatter mutations use PyYAML (parse, update N fields, write once)
- Single-property ops and note read/append/create use the obsidian CLI

All Obsidian note interactions should go through this module.
"""

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from billable import WorkClass, classify_work_class
from pane_surface import is_placeholder_tab_name
from personas import resolve_persona

logger = logging.getLogger(__name__)

# The live Obsidian vaults. Tests MUST NOT write here; the chokepoint guard below
# hard-fails any session-doc creation that targets either tree while under pytest.
# Imperium = personal-infra vault; Pax-ENV = civic (day-job / askCivic) vault.
LIVE_VAULT_ROOT = Path("/Volumes/Imperium/Imperium-ENV")
LIVE_CIVIC_VAULT_ROOT = Path("/Volumes/Civic/Pax-ENV")

OBSIDIAN_SYNC_ILLEGAL_FILENAME_CHARS = r'<>:"/\\|?*'


def vault_root() -> Path:
    """Resolve the Obsidian vault root at CALL time (never frozen at import).

    Freezing this at import is what let test runs pollute the live vault: when
    IMPERIUM_ENV was unset and /Volumes/Imperium was mounted, the module bound the
    live vault before any test fixture could redirect it.  Reading the environment
    on each call lets the isolation fixture point writes at a temp dir.
    """
    env = os.environ.get("IMPERIUM_ENV")
    if env:
        return Path(env)
    imperium = Path(os.environ.get("IMPERIUM", "/Volumes/Imperium"))
    if not imperium.exists():
        imperium = Path.home()
    return imperium / "Imperium-ENV"


def civic_vault_root() -> Path:
    """Resolve the civic (Pax-ENV) vault root at CALL time — mirrors vault_root().

    Civic / askCivic day-job work binds its session docs under the Pax-ENV vault
    (``/Volumes/Civic/Pax-ENV``), NOT the personal Imperium vault. Read the
    environment on each call (never frozen at import) so the isolation fixture can
    redirect civic writes to a temp dir via ``CIVIC_ENV``.
    """
    env = os.environ.get("CIVIC_ENV")
    if env:
        return Path(env)
    civic = Path(os.environ.get("CIVIC", "/Volumes/Civic"))
    if not civic.exists():
        civic = Path.home()
    return civic / "Pax-ENV"


def vault_root_for(working_dir: str | None = None, legion: str | None = None) -> Path:
    """Route to the vault that owns this work-class.

    Civic (BILLABLE) work — askCivic worktrees, ``/Volumes/Civic``, legion
    ``civic``/``pax`` — binds under Pax-ENV; everything else (PERSONAL **and**
    UNKNOWN) binds under Imperium. Keying on UNKNOWN→Imperium preserves the
    pre-existing mono-vault behavior for any launch we cannot confidently place.

    Routing keys on work-class (``working_dir`` + ``legion``), never on
    ``koronus:*`` pane labels, so it survives the council pane migration.
    """
    if classify_work_class(working_dir, legion) == WorkClass.BILLABLE:
        return civic_vault_root()
    return vault_root()


def terra_sessions_dir() -> Path:
    return vault_root() / "Terra" / "Sessions"


def mars_sessions_dir() -> Path:
    return vault_root() / "Mars" / "Sessions"


def daily_notes_dir_for(working_dir: str | None = None, legion: str | None = None) -> Path:
    """Daily-notes dir for this work-class.

    Civic daily notes live at ``Pax-ENV/Journal/Daily`` (no ``Terra/`` segment,
    created by ``morning_session.py``); Imperium daily notes live at
    ``Terra/Journal/Daily``. Structural divergence is intentional — see
    ``create_daily_note_file``.
    """
    if classify_work_class(working_dir, legion) == WorkClass.BILLABLE:
        return civic_vault_root() / "Journal" / "Daily"
    return vault_root() / "Terra" / "Journal" / "Daily"


def daily_notes_dir() -> Path:
    # One source of truth — the no-arg (Imperium) case of daily_notes_dir_for.
    # Still referenced by tests/test_vault_isolation.py.
    return daily_notes_dir_for()


def _assert_not_live_vault(path: Path) -> None:
    """Tripwire: under pytest, never create a session doc inside the live vault.

    Converts silent live-vault pollution into a loud failure so a regression is
    caught by the suite instead of by a human noticing 1,600 junk docs.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for live_root in (LIVE_VAULT_ROOT, LIVE_CIVIC_VAULT_ROOT):
        if resolved == live_root or live_root in resolved.parents:
            raise RuntimeError(
                f"Test attempted to write a session doc into the LIVE vault: {resolved}. "
                "Tests must target an isolated vault (the autouse isolate_vault fixture "
                "sets IMPERIUM_ENV and CIVIC_ENV to temp dirs)."
            )


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


def human_filename_stem(value: str, fallback: str = "needs-session-name", max_len: int = 90) -> str:
    """Return an Obsidian Sync-safe, hyphenated filename stem.

    Dates belong in frontmatter, not generated session-doc filenames.  File
    stems are lower-kebab for schema consistency; human display belongs in the
    note title/frontmatter.
    """
    stem = re.sub(r"[\x00-\x1f\x7f]", "", value or "")
    stem = re.sub(f"[{re.escape(OBSIDIAN_SYNC_ILLEGAL_FILENAME_CHARS)}]", " ", stem)
    stem = re.sub(r"[_\s-]+", "-", stem.lower()).strip("- .")
    if stem.lower().endswith(".md"):
        stem = stem[:-3].strip(" .")
    stem = stem or fallback
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip(" .")
    return stem or fallback


def unique_human_path(directory: Path, title: str, fallback: str = "needs-session-name") -> Path:
    """Create a non-date-prefixed, hyphenated path with numeric collision suffixes."""
    _assert_not_live_vault(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = human_filename_stem(title, fallback=fallback)
    candidate = directory / f"{stem}.md"
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}.md"
        if not candidate.exists():
            return candidate
        counter += 1


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


def _serialize_yaml_block(fm: dict[str, Any]) -> str:
    """Serialize a frontmatter dict to the YAML text that sits between the fences."""
    return yaml.dump(
        fm,
        Dumper=_ObsidianDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=200,
    ).rstrip("\n")


def _has_valid_frontmatter(content: str) -> bool:
    """True iff ``parse_frontmatter`` recognized a real frontmatter dict.

    ``parse_frontmatter`` returns ``({}, full_content)`` both when there is no
    leading fence AND when the fenced YAML doesn't parse to a dict (empty /
    malformed). In those cases there is no YAML region to splice surgically, so
    we must fall back to the prepend path rather than mis-detect a fence.
    """
    if not content.startswith("---"):
        return False
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return False
    try:
        loaded = yaml.safe_load(content[3:end_idx].strip())
    except yaml.YAMLError:
        return False
    return isinstance(loaded, dict)


def splice_frontmatter(content: str, fm: dict[str, Any]) -> str:
    """Replace ONLY the YAML region of ``content`` with the serialized ``fm``.

    Surgical write: the body (the closing ``---`` fence and everything after it)
    is copied through byte-for-byte from ``content`` — it is never re-parsed or
    re-serialized. A frontmatter-only rewrite physically cannot clobber body
    appends made between a read and this write.

    When ``content`` has no parseable frontmatter dict, falls back to
    ``serialize_frontmatter`` over the whole content as body, so behavior matches
    the pre-fix writer exactly for those (rare) inputs.
    """
    if _has_valid_frontmatter(content):
        end_idx = content.find("\n---", 3)
        # Tail = the closing fence and everything after it, preserved verbatim.
        tail = content[end_idx + 1 :]  # starts at "---" of the closing fence
        yaml_str = _serialize_yaml_block(fm)
        return f"---\n{yaml_str}\n{tail}"

    # No valid frontmatter: identical to the pre-fix path (whole content as body).
    return serialize_frontmatter(fm, content)


def update_frontmatter(
    file_path: Path,
    updates: dict[str, Any] | None = None,
    delete_keys: list[str] | None = None,
    *,
    transform: Callable[[dict[str, Any]], None] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Surgically merge updates into a note's frontmatter, atomically.

    Args:
        file_path: Path to the markdown file.
        updates: Key-value pairs to set/overwrite in frontmatter.
        delete_keys: Keys to remove from frontmatter (applied after updates).
        transform: Optional callback mutating the FRESHLY-read frontmatter dict
            in place, run *inside* the locked retry loop after updates/delete_keys.
            Use this for read-modify-write of a nested field (e.g. one rubric
            subkey) so a concurrent writer touching a *different* subkey of the
            same dict can't last-write-win — every attempt re-derives from the
            on-disk state. Must be idempotent across retries.
        max_attempts: mtime-conflict retries before raising.

    Race-safety (P0 2026-06-17 — the daily-note timer write-race):
      - **Surgical**: only the YAML block between the ``---`` fences is rewritten;
        the body is spliced through byte-for-byte (see ``splice_frontmatter``), so
        an external ``obsidian append`` / ``Edit`` landing during this update is
        never lost, and untouched keys (``agents``/``instance_ids``) are preserved.
      - **Locked**: serialized against the callout writer and any other in-process
        frontmatter writer via the shared per-file lock.
      - **Atomic + mtime-guarded**: temp file + ``os.replace``; if the file changed
        between our read and write (e.g. an external append slipped past the lock),
        retry up to ``max_attempts`` so we re-read the fresh body before writing.

    Returns the updated frontmatter dict.
    Raises FileNotFoundError if the file doesn't exist.
    """
    # Imported here (not at module top) to keep this module FastAPI/dependency
    # light at import time and avoid any import-order surprise; the helpers are
    # plain stdlib-backed primitives.
    from dailynote_callout import (
        CalloutConflictError,
        _atomic_write,
        file_write_lock,
    )
    from vault_lock import file_flock

    # Lock ordering: cross-process flock → in-process threading lock → RMW, so
    # the timer frontmatter writer and the `obsidian` CLI can never interleave.
    with file_flock(file_path), file_write_lock(file_path):
        last_conflict: CalloutConflictError | None = None
        for _attempt in range(max_attempts):
            stat = file_path.stat()  # FileNotFoundError bubbles intentionally.
            content = file_path.read_text(encoding="utf-8")
            fm, _body = parse_frontmatter(content)
            if updates:
                fm.update(updates)
            if delete_keys:
                for key in delete_keys:
                    fm.pop(key, None)
            if transform is not None:
                transform(fm)
            new_content = splice_frontmatter(content, fm)
            # Write-skip: byte-identical frontmatter splice ⇒ no write, so mtime
            # holds steady and the 30s timer poll is a no-op off the grid boundary.
            if new_content == content:
                return fm
            try:
                _atomic_write(file_path, new_content, stat.st_mtime_ns)
                return fm
            except CalloutConflictError as exc:
                last_conflict = exc
                continue

    raise last_conflict or CalloutConflictError(
        f"frontmatter target changed during write: {file_path}"
    )


def update_session_doc_worktrees(
    file_path: Path,
    *,
    action: str,
    path: str,
    branch: str | None = None,
    port: Any = None,
    claimed_at: str | None = None,
) -> list[dict]:
    """Mutate a session doc's `worktrees` registry, holding the one-active invariant.

    Entry shape: {path, branch, port, status: active|archived, claimed_at}.

    - action="claim": demote any prior active entry to archived, then add (or
      refresh) an active entry for `path`. Exactly one entry stays active.
    - action="archive": flip the entry matching `path` from active → archived.
      Archived entries are RETAINED, never deleted.

    The token-api endpoint also holds an asyncio.Lock across calls to keep the
    one-active invariant under interleaving claims. The registry mutation itself
    is applied inside update_frontmatter's locked retry loop (via ``transform``)
    over the freshly-read list, so the read-modify-write is atomic against the
    file even independent of that endpoint lock.
    """
    if action not in ("claim", "archive"):
        raise ValueError(f"unknown action: {action!r}")
    if not path:
        raise ValueError(f"{action} requires path")

    captured: dict[str, list[dict]] = {}

    def _mutate(fm: dict[str, Any]) -> None:
        wts = fm.get("worktrees")
        if not isinstance(wts, list):
            wts = []
        # Keep only well-formed dict entries; drop anything malformed.
        wts = [dict(w) for w in wts if isinstance(w, dict)]

        if action == "claim":
            for w in wts:
                if w.get("status") == "active":
                    w["status"] = "archived"
            existing = next((w for w in wts if w.get("path") == path), None)
            if existing is not None:
                existing.update(
                    {"branch": branch, "port": port, "status": "active", "claimed_at": claimed_at}
                )
            else:
                wts.append(
                    {
                        "path": path,
                        "branch": branch,
                        "port": port,
                        "status": "active",
                        "claimed_at": claimed_at,
                    }
                )
        else:  # archive
            for w in wts:
                if w.get("path") == path and w.get("status") == "active":
                    w["status"] = "archived"

        fm["worktrees"] = wts
        captured["wts"] = wts

    update_frontmatter(file_path, transform=_mutate)
    return captured.get("wts", [])


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
    fm, _body = read_frontmatter(file_path)

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

    # Surgical + atomic + locked write (see update_frontmatter).
    return update_frontmatter(file_path, updates)


# ============ Generic Rubric Machinery ============
# A "rubric" is a typed completion checklist embedded in a markdown note's
# frontmatter. It powers the Golden Throne accountability check: GT reads
# the rubric, finds the first unmet condition, and pings the agent with a
# specific accusation instead of a generic "resume" poke.
#
# The pattern is intentionally generic so it works for session docs (rubric_key
# defaults to "victory"), aspirants (rubric_key="dispatch"), and any future
# per-primarch or per-persona schema. The rubric_key declares which dict-shaped
# frontmatter field to evaluate; siblings are derived by suffix:
#
#   <rubric_key>                 → dict of {condition_key: bool}
#   <rubric_key>_skip            → list of inapplicable condition keys
#   <rubric_key>_notified_at     → ISO ts: first-touch Emperor notify
#   <rubric_key>_acknowledged_at → ISO ts: Emperor's final ack (archives doc)
#   <rubric_key>_reason          → free-text written at ack time
#
# Legacy support: if <rubric_key> is a scalar string (e.g. "pending",
# "declared"), the evaluator returns legacy_string=True so callers can fall
# back to old behavior without forcing migration.

DEFAULT_RUBRIC_KEY = "victory"

# Default rubric seeded into freshly-created session docs. Open-keyed by design:
# adding a new SOP key here is the only change needed — the engine iterates the
# dict and discovers it automatically.
DEFAULT_SESSION_DOC_RUBRIC: dict[str, bool] = {
    # Starts True at creation; UserPromptSubmit hook flips to False each turn,
    # and any Write/Edit on the doc flips it back True. Silent guard: agents
    # that end a turn without touching their session doc trip GT next cycle.
    "session_doc_up_to_date": True,
    "extensively_validated": False,  # services restarted, redeployed, live endpoints pinged
    "vault_searched": False,  # related vault docs reviewed for staleness
    "committed": False,
    "pushed": False,
    "pr_opened": False,
    "coderabbit_passed": False,
    # Derived in evaluate_rubric from fm['sanguinius_is']: True iff state is terminal.
    "sanguinius_satisfied": False,
    # Derived in evaluate_rubric from fm['commentary']: True iff commentary is None.
    "commentary_resolved": True,
    # Derived in evaluate_rubric from fm['_instance_tab_names'] (injected by the
    # victory-ack caller): True iff a linked instance has a non-placeholder name.
    # A stale `needs-name` blocks victory; absent surface => non-blocking True.
    "instance_named": False,
}


# Beautifier state machine. Frontmatter surface is a themed self-report string
# (Sanguinius' perspective: "I am ___"). Internally we map to an integer so
# comparisons don't rely on string parsing and so the same state machine can be
# reused under a different label in other vaults (e.g. Pax-ENV uses `designer_is`
# with unthemed strings; the integers are identical).
#
# Convention: any frontmatter key matching the regex r'^[a-z_]+_is$' is a
# beautifier state. The prefix selects the registry (sanguinius_is →
# SANGUINIUS_STATES; designer_is → DESIGNER_STATES, etc.). State >= 3 is terminal.
SANGUINIUS_STATES: dict[str, int] = {
    "yet to take wing": 0,  # never invoked
    "at the easel": 1,  # drafting now
    "hovering at your shoulder": 2,  # drafts presented, awaiting Emperor's eye
    "folding my wings": 3,  # Emperor has chosen; Sang rests (terminal)
}

# Persona-name → state-map registry. Add more as other vaults port the pattern.
BEAUTIFIER_STATE_REGISTRIES: dict[str, dict[str, int]] = {
    "sanguinius": SANGUINIUS_STATES,
}
BEAUTIFIER_TERMINAL_INT = 3
_IS_FIELD_RE = re.compile(r"^([a-z_]+)_is$")


def resolve_beautifier_state(fm: dict) -> tuple[str | None, int | None]:
    """Find a `<persona>_is` key in frontmatter and resolve it to (persona, state_int).

    Returns (None, None) if no matching field exists or the registry is unknown.
    Returns (persona, None) if the field exists but the value isn't in the registry.
    """
    for key, raw in fm.items():
        m = _IS_FIELD_RE.match(key)
        if not m:
            continue
        persona = m.group(1)
        registry = BEAUTIFIER_STATE_REGISTRIES.get(persona)
        if registry is None:
            continue
        if isinstance(raw, int):
            return persona, raw
        if isinstance(raw, str):
            return persona, registry.get(raw.strip().lower())
        return persona, None
    return None, None


# --- Derived rubric subconditions --------------------------------------------
#
# Some rubric keys are NOT read from their literal frontmatter value: they are
# recomputed from a canonical surface every time the rubric is evaluated, so an
# Emperor edit to that surface gates victory immediately. The literal value (if
# any) is overwritten by the derived computation — editing it by hand has no
# effect. Keeping the compute + explain functions in one registry lets
# evaluate_rubric() and the diagnostic surfaces stay in lockstep (a missing
# derived condition can then say *why* it is unmet, instead of looking like a
# stale/cached read).


def _derive_sanguinius_satisfied(fm: dict) -> bool:
    _, state_int = resolve_beautifier_state(fm)
    return state_int is not None and state_int >= BEAUTIFIER_TERMINAL_INT


def _explain_sanguinius_satisfied(fm: dict) -> dict:
    persona, state_int = resolve_beautifier_state(fm)
    is_field = f"{persona}_is" if persona else "<persona>_is"
    raw = fm.get(is_field) if persona else None
    if state_int is not None:
        detail = (
            f"derived from '{is_field}' (currently {raw!r} → {state_int}); "
            f"needs ≥ {BEAUTIFIER_TERMINAL_INT} ('folding my wings'). "
            f"Editing the literal 'sanguinius_satisfied' has no effect — "
            f"advance '{is_field}' instead."
        )
    else:
        detail = (
            "derived from a beautifier '<persona>_is' field that is absent or "
            "unrecognized (e.g. set sanguinius_is: 'folding my wings'). "
            "Editing the literal 'sanguinius_satisfied' has no effect."
        )
    return {
        "derived_from": is_field,
        "source_value": raw,
        "resolved_int": state_int,
        "terminal_int": BEAUTIFIER_TERMINAL_INT,
        "detail": detail,
    }


def _derive_commentary_resolved(fm: dict) -> bool:
    return fm.get("commentary") is None


def _explain_commentary_resolved(fm: dict) -> dict:
    val = fm.get("commentary")
    if val is not None:
        detail = (
            "derived: True iff frontmatter 'commentary' is empty — currently "
            "set. Reconcile and clear the 'commentary' field to satisfy this."
        )
    else:
        detail = "derived: 'commentary' is empty (satisfied)."
    return {"derived_from": "commentary", "source_value": val, "detail": detail}


# field name → (compute effective bool from fm). Only fields PRESENT in a
# rubric dict are derived; declaring the key is what opts it in.
DERIVED_RUBRIC_FIELDS: dict[str, Callable[[dict], bool]] = {
    "sanguinius_satisfied": _derive_sanguinius_satisfied,
    "commentary_resolved": _derive_commentary_resolved,
}

# field name → (human-facing provenance dict). Mirrors DERIVED_RUBRIC_FIELDS.
DERIVED_RUBRIC_EXPLAINERS: dict[str, Callable[[dict], dict]] = {
    "sanguinius_satisfied": _explain_sanguinius_satisfied,
    "commentary_resolved": _explain_commentary_resolved,
}


@dataclass
class RubricStatus:
    """Evaluated rubric state. Returned by evaluate_rubric/read_rubric."""

    rubric_key: str
    complete: bool
    missing: list[str]
    skipped: list[str]
    notified_at: str | None
    acknowledged_at: str | None
    reason: str | None
    rubric: dict
    legacy_string: bool = False
    present: bool = True


def _rubric_sibling(rubric_key: str, suffix: str) -> str:
    """Build sibling field name by convention: 'victory' + 'skip' → 'victory_skip'."""
    return f"{rubric_key}_{suffix}"


def evaluate_rubric(fm: dict, rubric_key: str | None = None) -> RubricStatus:
    """Evaluate the rubric in a frontmatter dict.

    Resolves rubric_key with precedence: explicit arg > fm['rubric_key'] > 'victory'.
    Supports three rubric shapes:
      - dict of bools: modern typed rubric (the happy path)
      - scalar string: legacy ('pending', 'declared'). Returns legacy_string=True.
      - missing/unknown: returns present=False, treated as effectively complete
        so legacy docs without any rubric don't trip GT.
    """
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    rubric_value = fm.get(rk)
    skip_raw = fm.get(_rubric_sibling(rk, "skip")) or []
    skip = [s for s in skip_raw if isinstance(s, str)]
    notified_at = fm.get(_rubric_sibling(rk, "notified_at"))
    acknowledged_at = fm.get(_rubric_sibling(rk, "acknowledged_at"))
    reason = fm.get(_rubric_sibling(rk, "reason"))

    if rubric_value is None:
        return RubricStatus(
            rubric_key=rk,
            complete=True,
            missing=[],
            skipped=[],
            notified_at=notified_at,
            acknowledged_at=acknowledged_at,
            reason=reason,
            rubric={},
            legacy_string=False,
            present=False,
        )

    if isinstance(rubric_value, str):
        complete = rubric_value.lower() in {"declared", "true", "achieved", "complete"}
        missing = [] if complete else [f"legacy:{rubric_value}"]
        return RubricStatus(
            rubric_key=rk,
            complete=complete,
            missing=missing,
            skipped=[],
            notified_at=notified_at,
            acknowledged_at=acknowledged_at,
            reason=reason,
            rubric={rk: rubric_value},
            legacy_string=True,
            present=True,
        )

    if isinstance(rubric_value, bool):
        complete = bool(rubric_value)
        missing = [] if complete else [rk]
        return RubricStatus(
            rubric_key=rk,
            complete=complete,
            missing=missing,
            skipped=[],
            notified_at=notified_at,
            acknowledged_at=acknowledged_at,
            reason=reason,
            rubric={rk: rubric_value},
            legacy_string=False,
            present=True,
        )

    if not isinstance(rubric_value, dict):
        return RubricStatus(
            rubric_key=rk,
            complete=True,
            missing=[],
            skipped=[],
            notified_at=notified_at,
            acknowledged_at=acknowledged_at,
            reason=reason,
            rubric={},
            legacy_string=False,
            present=False,
        )

    # Derived subconditions: always recomputed from canonical frontmatter
    # surfaces so Emperor edits gate victory immediately. The literal value in
    # the rubric dict is overwritten — see DERIVED_RUBRIC_FIELDS for the source
    # surfaces (e.g. `sanguinius_satisfied` ← beautifier `<persona>_is` state;
    # `commentary_resolved` ← `commentary` being empty).
    derived_dirty = False
    for dkey, compute in DERIVED_RUBRIC_FIELDS.items():
        if dkey in rubric_value:
            if not derived_dirty:
                rubric_value = dict(rubric_value)
                derived_dirty = True
            rubric_value[dkey] = compute(fm)

    # `instance_named`: True iff at least one linked instance carries a real
    # (non-placeholder) tab name. The live name(s) are injected by the evaluator
    # caller as fm['_instance_tab_names'] (e.g. the victory-ack chokepoint).
    # When that surface is absent — a legacy/un-enriched read, or a doc with no
    # linked instance — this derives True so it never falsely blocks; the gate
    # only bites where the caller enriches the surface.
    if "instance_named" in rubric_value:
        if not derived_dirty:
            rubric_value = dict(rubric_value)
            derived_dirty = True
        names = fm.get("_instance_tab_names")
        # `n and ...`: a None/empty name is *absence of a name*, not a real one —
        # is_placeholder_tab_name(None) is False, so without the truthy guard a
        # NULL tab_name would wrongly satisfy the criterion (and mask a sibling
        # placeholder). Require a truthy, non-placeholder name.
        rubric_value["instance_named"] = (not names) or any(
            n and not is_placeholder_tab_name(n) for n in names
        )

    skip_set = set(skip)
    missing = [k for k, v in rubric_value.items() if not bool(v) and k not in skip_set]
    skipped = [k for k in rubric_value.keys() if k in skip_set]
    return RubricStatus(
        rubric_key=rk,
        complete=(len(missing) == 0),
        missing=missing,
        skipped=skipped,
        notified_at=notified_at,
        acknowledged_at=acknowledged_at,
        reason=reason,
        rubric=dict(rubric_value),
        legacy_string=False,
        present=True,
    )


def read_rubric(file_path: Path, rubric_key: str | None = None) -> RubricStatus:
    """Read frontmatter from disk and evaluate the rubric."""
    fm, _ = read_frontmatter(file_path)
    return evaluate_rubric(fm, rubric_key)


def describe_rubric(fm: dict, rubric_key: str | None = None) -> dict:
    """Rich, human-facing breakdown of a rubric: effective values + provenance.

    Returns the evaluated status plus, per declared field, whether it is derived
    and from what surface — so an operator can see (e.g.) that
    `sanguinius_satisfied` is computed from the beautifier `<persona>_is` state
    and that editing the literal field does nothing. Pure: never touches disk
    (caller supplies fm) and never mutates.
    """
    status = evaluate_rubric(fm, rubric_key)
    rubric = status.rubric if isinstance(status.rubric, dict) else {}
    skip_set = set(status.skipped)
    fields: dict[str, dict] = {}
    for key, value in rubric.items():
        entry: dict[str, Any] = {
            "value": bool(value),
            "derived": key in DERIVED_RUBRIC_FIELDS,
            "skipped": key in skip_set,
            "unmet": (not bool(value)) and key not in skip_set,
        }
        explainer = DERIVED_RUBRIC_EXPLAINERS.get(key)
        if explainer is not None:
            entry.update(explainer(fm))
        fields[key] = entry
    return {
        "rubric_key": status.rubric_key,
        "present": status.present,
        "legacy_string": status.legacy_string,
        "complete": status.complete,
        "missing": status.missing,
        "skipped": status.skipped,
        "fields": fields,
        "acknowledged_at": status.acknowledged_at,
        "notified_at": status.notified_at,
        "reason": status.reason,
    }


def explain_unmet(fm: dict, missing: list[str], rubric_key: str | None = None) -> list[dict]:
    """Build per-field explanations for unmet rubric conditions.

    Used by the victory-ack 409 path so a derived condition surfaces *why* it is
    unmet (and that editing its literal won't help) rather than reading like a
    stale file.
    """
    diag = describe_rubric(fm, rubric_key)
    out: list[dict] = []
    for name in missing:
        field = diag["fields"].get(name, {})
        out.append(
            {
                "field": name,
                "derived": field.get("derived", False),
                "detail": field.get("detail"),
            }
        )
    return out


def update_rubric_field(
    file_path: Path,
    key: str,
    value: Any,
    rubric_key: str | None = None,
) -> dict:
    """Atomically set rubric[key] = value in the file's frontmatter.

    If the rubric is missing or legacy-scalar, the field is upgraded to a dict
    containing only the supplied key. Returns the updated frontmatter dict.

    The rubric dict is read-modified *inside* update_frontmatter's locked retry
    loop (via ``transform``) so a concurrent writer flipping a different subkey
    of the same rubric can't last-write-win — each attempt re-derives from the
    freshly-read frontmatter.
    """

    def _set_subkey(fm: dict[str, Any]) -> None:
        rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
        existing = fm.get(rk)
        upgraded = dict(existing) if isinstance(existing, dict) else {}
        upgraded[key] = value
        fm[rk] = upgraded

    return update_frontmatter(file_path, transform=_set_subkey)


# --- CodeRabbit review reconciliation (pure, network-free) -------------------
#
# CodeRabbit posts PR review comments minutes after a PR opens. We fold its
# *actionable* findings into the session-doc victory rubric as per-comment bool
# keys so the already-proven Golden Throne walker drives the worker through each
# one. All rich data (path, line, body) lives in a sibling array; rubric values
# stay strictly bool so evaluate_rubric() keeps treating them as gate conditions.

# Category markers, matched case-insensitively against the comment body. Order is
# precedence: the first marker that matches wins. Anything substantive matching
# none of these is treated as 'actionable' (the conservative default).
_CODERABBIT_CATEGORY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("outside_diff", ("outside diff range",)),
    ("duplicate", ("duplicate comment",)),
    ("nitpick", ("nitpick",)),
)

# Per-comment rubric keys live under this prefix; the batch nitpick key and the
# aggregate pass key are fixed names. Rich data goes in the sibling fields.
CODERABBIT_KEY_PREFIX = "coderabbit_"
CODERABBIT_NITPICK_KEY = "coderabbit_nitpicks"
CODERABBIT_PASSED_KEY = "coderabbit_passed"
CODERABBIT_COMMENTS_FIELD = "coderabbit_comments"
CODERABBIT_REVIEW_STATE_FIELD = "coderabbit_review_state"

# Cap stored comment bodies so frontmatter stays small; the full suggestion
# always lives on the PR. Render-time truncation shortens further for pokes/TTS.
_CODERABBIT_BODY_STORE_CAP = 600


def classify_coderabbit_comment(body: str) -> str:
    """Classify a CodeRabbit comment body.

    Returns one of 'actionable' | 'nitpick' | 'duplicate' | 'outside_diff'.
    Actionable findings each earn their own rubric key; nitpicks collapse into a
    single batch key; duplicate / outside-diff are stored but never keyed. A
    substantive inline comment matching no marker defaults to 'actionable'.
    """
    text = (body or "").lower()
    for category, markers in _CODERABBIT_CATEGORY_MARKERS:
        if any(marker in text for marker in markers):
            return category
    return "actionable"


def _coderabbit_comment_key(category: str, comment_id: Any) -> str | None:
    """Rubric key for a comment, or None if its category is never keyed."""
    if category == "actionable":
        return f"{CODERABBIT_KEY_PREFIX}{comment_id}"
    if category == "nitpick":
        return CODERABBIT_NITPICK_KEY
    return None


def reconcile_coderabbit_comments(
    fm: dict,
    fetched: list[dict],
    *,
    review_terminal: bool,
    rubric_key: str | None = None,
) -> dict:
    """Pure reconciler: fold fetched CodeRabbit comments into rubric mutations.

    Pure function of (current frontmatter, fetched comments). Never performs I/O
    and never mutates ``fm``. Returns a flat dict of frontmatter mutations
    (``victory`` whole dict + ``coderabbit_comments`` + ``coderabbit_review_state``)
    intended for ONE atomic ``update_frontmatter`` write. The caller writes only
    if a mutation actually differs from current frontmatter.

    Rules:
      - Dedup by comment ``id``. Each NEW actionable comment adds
        ``coderabbit_<id>: false``. Nitpicks collapse into one
        ``coderabbit_nitpicks: false`` (added iff >=1 nitpick present). Duplicate
        / outside-diff / summary comments are stored only, never keyed.
      - Existing rubric values are PRESERVED (a worker's ``true`` is durable; we
        never reset true->false and never remove a key here except the terminal
        prune below).
      - Worker-flipped keys are mirrored into each entry's ``addressed``.
      - If ``review_terminal`` and no per-comment key remains unmet, set
        ``coderabbit_passed: true`` and prune the per-comment keys.

    The caller must only invoke this with a *successful* fetch; on a fetch error
    it should skip reconciliation so a transient empty result never wipes the
    stored comment array.
    """
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    existing_rubric = fm.get(rk)
    if not isinstance(existing_rubric, dict):
        # No typed rubric to fold into — nothing this reconciler can safely do.
        return {}
    victory = dict(existing_rubric)
    # Once the aggregate pass is set, the loop is closed: never re-key comments
    # that are still physically on the PR. Keeps reconcile idempotent post-pass
    # independent of when the poller disarms.
    already_passed = bool(victory.get(CODERABBIT_PASSED_KEY))

    comments: list[dict] = []
    have_nitpick = False
    seen_ids: set = set()
    for raw in fetched:
        if not isinstance(raw, dict):
            continue
        cid = raw.get("id")
        if cid is None or cid in seen_ids:
            continue
        seen_ids.add(cid)
        comment_type = raw.get("comment_type", "inline")
        if comment_type == "summary":
            category = "summary"
        else:
            category = classify_coderabbit_comment(raw.get("body", ""))
        if category == "nitpick":
            have_nitpick = True
        comments.append(
            {
                "id": cid,
                "key": _coderabbit_comment_key(category, cid),
                "category": category,
                "path": raw.get("path"),
                "line": raw.get("line"),
                "body": (raw.get("body") or "")[:_CODERABBIT_BODY_STORE_CAP],
                "addressed": False,  # recomputed below from rubric values
            }
        )

    # Add keys for NEW actionable comments; preserve all existing rubric values.
    for entry in comments:
        key = entry["key"]
        if not already_passed and entry["category"] == "actionable" and key not in victory:
            victory[key] = False
    if not already_passed and have_nitpick and CODERABBIT_NITPICK_KEY not in victory:
        victory[CODERABBIT_NITPICK_KEY] = False

    # Mirror worker-flipped rubric values into each entry's ``addressed``.
    for entry in comments:
        key = entry["key"]
        entry["addressed"] = bool(key and victory.get(key))

    # Derive review state for the (network-free) poke renderer.
    if review_terminal:
        review_state = "complete"
    elif comments:
        review_state = "reviewing"
    else:
        review_state = "pending"

    # Terminal + every per-comment key addressed → pass + prune the per-comment
    # keys (coderabbit_passed now stands in for them).
    per_comment_keys = [
        k for k in victory if k.startswith(CODERABBIT_KEY_PREFIX) and k != CODERABBIT_PASSED_KEY
    ]
    if review_terminal and all(bool(victory.get(k)) for k in per_comment_keys):
        victory[CODERABBIT_PASSED_KEY] = True
        for k in per_comment_keys:
            victory.pop(k, None)

    return {
        rk: victory,
        CODERABBIT_COMMENTS_FIELD: comments,
        CODERABBIT_REVIEW_STATE_FIELD: review_state,
    }


def mark_rubric_notified(file_path: Path, rubric_key: str | None = None) -> dict:
    """Stamp <rubric_key>_notified_at = now() — first-touch Emperor notify."""
    fm, _body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    return update_frontmatter(
        file_path, {_rubric_sibling(rk, "notified_at"): datetime.now().isoformat()}
    )


def clear_rubric_notified(file_path: Path, rubric_key: str | None = None) -> dict:
    """Clear <rubric_key>_notified_at — used when a previously-complete rubric regresses."""
    fm, _body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    return update_frontmatter(file_path, {_rubric_sibling(rk, "notified_at"): None})


def mark_rubric_acknowledged(
    file_path: Path,
    reason: str,
    rubric_key: str | None = None,
) -> dict:
    """Stamp <rubric_key>_acknowledged_at + <rubric_key>_reason — final Emperor ack."""
    fm, _body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    now = datetime.now().isoformat()
    return update_frontmatter(
        file_path,
        {
            _rubric_sibling(rk, "acknowledged_at"): now,
            _rubric_sibling(rk, "reason"): reason,
        },
    )


def bump_session_doc_up_to_date(file_path: Path, value: bool) -> dict | None:
    """Flip the session_doc_up_to_date inverse flag on a session doc rubric.

    Convention: UserPromptSubmit hook sets this False each turn; a Write/Edit
    that targets the doc flips it back True. Agents that end a turn without
    touching their doc trip GT next cycle. Silent guard on the happy path.

    Returns None if the doc has no rubric (legacy/string victory or missing).
    """
    try:
        fm, _body = read_frontmatter(file_path)
    except FileNotFoundError:
        return None
    rk = fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    existing = fm.get(rk)
    if not isinstance(existing, dict):
        return None
    if existing.get("session_doc_up_to_date") == value:
        # Already at target — no write needed (the pre-lock read is sufficient
        # for this fast path; the flag is single-writer-per-turn in practice).
        return fm

    # Re-derive the rubric inside the locked retry loop so a concurrent writer
    # flipping a different subkey can't be clobbered by a whole-dict overwrite.
    def _set_flag(fm: dict[str, Any]) -> None:
        cur = fm.get(rk)
        upgraded = dict(cur) if isinstance(cur, dict) else {}
        upgraded["session_doc_up_to_date"] = value
        fm[rk] = upgraded

    return update_frontmatter(file_path, transform=_set_flag)


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
    """Create the markdown file for a session document.

    Seeds the typed victory rubric (see DEFAULT_SESSION_DOC_RUBRIC) so the
    Golden Throne accountability engine can read specific unmet conditions
    instead of firing a generic resume prompt.
    """
    _assert_not_live_vault(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    fm: dict[str, Any] = {
        "session_doc_id": doc_id,
        "created": today,
    }
    if project:
        fm["project"] = project
    fm["agents"] = []  # append-only launch roster (see _update_doc_agents_list)
    if primarch_name:
        fm["primarch"] = primarch_name
    fm["status"] = "active"
    fm["type"] = "session"
    fm["rubric_key"] = DEFAULT_RUBRIC_KEY
    fm["start_time"] = None
    fm["end_time"] = None
    fm["duration_minutes"] = None
    fm["legion"] = None
    fm["faction"] = None
    fm["victory_conditions"] = []
    fm["commentary"] = None
    fm["sanguinius_is"] = "yet to take wing"
    fm["drafts"] = []
    fm["victory"] = dict(DEFAULT_SESSION_DOC_RUBRIC)
    fm["victory_skip"] = []
    fm["victory_notified_at"] = None
    fm["victory_acknowledged_at"] = None
    fm["victory_reason"] = None
    fm["deliverables"] = []
    fm["instance_type"] = "one_off"
    fm["zealotry"] = 4
    fm["pr_url"] = None
    # Worktree registry (Phase 3). List of {path, branch, port, status, claimed_at};
    # exactly one entry is status:active at a time, archived entries are retained.
    # NOT the dormant `worktrees` DB table. Mutated server-side via
    # update_session_doc_worktrees() to hold the one-active invariant.
    fm["worktrees"] = []

    body = (
        f"# Session: {title}\n\n"
        "<!-- visual:html BEGIN -->\n"
        '<div class="session-visual">\n'
        "  <em>Sanguinius is yet to take wing. Dispatch with "
        "<code>dispatch --persona sanguinius --session-doc &lt;name&gt;</code>.</em>\n"
        "</div>\n"
        "<!-- visual:html END -->\n\n"
        "## Drafts\n"
        "_Sanguinius stages alternates here while he hovers. On wings-fold the top kept draft is promoted into the canonical region above and these regions are cleared._\n\n"
        "## HTML Corrections Buffer\n"
        "<!-- corrections:html BEGIN -->\n"
        "<!-- corrections:html END -->\n\n"
        "## Scratchpad\n\n"
        "_Assumptions, decisions in flight, handoff state. One thread per ongoing concern; flat is fine for one-off docs._\n\n"
        "## Activity Log\n\n"
    )
    file_path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")


async def _update_doc_agents_list(db, doc_id: int) -> None:
    """Append newly-bound agents to a session doc's append-only ``agents:`` log.

    ``agents:`` is a monotonic, append-only roster of every agent ever bound to
    the doc — a pulse for fleet reconstruction, NOT live lifecycle state. We
    deliberately do **not** filter by status and never remove an entry: a
    stopped/archived agent stays in the log. New names are unioned with whatever
    is already in the note, so the roster survives even if the DB rows are later
    cleared (the note is the durable record). Combined with ``update_frontmatter``'s
    write-skip guard, a sync that adds no new agent is a byte-identical no-op, so
    the daily note's mtime never moves on routine status churn — only a genuine new
    launch writes (low-frequency, append-only). ``instance_ids`` is retired: it was
    never read for logic and implied a live "active instances" write-contract the
    daily-note cold-storage model deliberately does not hold.
    """
    cursor = await db.execute(
        "SELECT name FROM instances WHERE session_doc_id = ?",
        (doc_id,),
    )
    rows = await cursor.fetchall()
    bound_names = [r[0] for r in rows if r[0]]

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

    def _merge(fm: dict[str, Any]) -> None:
        existing = fm.get("agents")
        roster = list(existing) if isinstance(existing, list) else []
        seen = set(roster)
        for name in bound_names:
            if name not in seen:
                roster.append(name)
                seen.add(name)
        fm["agents"] = roster
        # Retire the live-lifecycle field wherever it still lingers.
        fm.pop("instance_ids", None)
        if primarch_name:
            fm["primarch"] = primarch_name
        else:
            fm.pop("primarch", None)

    await asyncio.to_thread(update_frontmatter, fp, transform=_merge)


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
    return await resolve_or_create_session_doc_for_path(db, daily_notes_dir() / f"{date_str}.md")


def create_daily_note_file(
    file_path: Path, date_str: str, doc_id: int, *, civic: bool = False
) -> None:
    """Create a daily-note home on demand (Custodes/Imperium or civic/Pax-ENV).

    Daily-note continuity is note-backed, not an interactive session-doc
    placeholder.  Keep the shape intentionally plain so existing journal notes
    remain valid Obsidian daily notes while still carrying session_doc_id for
    Token-API binding.

    Two shapes — a single chokepoint with a ``civic`` flag rather than a forked
    function, so the live-vault tripwire below guards both vaults at one place:

    - Imperium (``civic=False``): ``type: daily-note`` + ``legion: custodes`` +
      ``agents`` roster (the Custodes shape).
    - Civic (``civic=True``): the minimal Pax-ENV shape ``type: daily`` +
      ``tags: ["daily","civic"]``, NO ``legion``/``agents`` — matches what
      ``morning_session.py`` writes. This is the rare fallback; normally the
      morning note already exists and is merely backfilled with session_doc_id.
    """
    _assert_not_live_vault(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if civic:
        fm: dict[str, Any] = {
            "session_doc_id": doc_id,
            "title": date_str,
            "created": date_str,
            "date": date_str,
            "type": "daily",
            "status": "active",
            "tags": ["daily", "civic"],
        }
        body = f"# {date_str}\n\n## Log\n"
    else:
        fm = {
            "session_doc_id": doc_id,
            "created": date_str,
            "date": date_str,
            "type": "daily-note",
            "status": "active",
            "legion": "custodes",
            "agents": [],  # append-only launch roster (see _update_doc_agents_list)
        }
        body = f"# {date_str}\n\n## Custodes\n\n## Log\n"
    file_path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")


async def resolve_or_create_today_daily_note_session_doc(
    db,
    date_str: str | None = None,
    *,
    working_dir: str | None = None,
    legion: str | None = None,
) -> int:
    """Return today's daily note, creating note and DB row if absent.

    Routes by work-class: civic (askCivic worktree / legion civic|pax) binds the
    Pax-ENV daily note; everything else binds the Imperium Custodes daily note.
    New params are optional + default Imperium so the no-arg caller (the Custodes
    midnight rebind in ``routes/day_start.py``) is unchanged.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    is_civic = classify_work_class(working_dir, legion) == WorkClass.BILLABLE
    fp = (daily_notes_dir_for(working_dir, legion) / f"{date_str}.md").resolve()
    existing = await resolve_or_create_session_doc_for_path(db, fp)
    if existing:
        return existing

    now_ts = datetime.now().isoformat()
    project = "Pax Daily Note" if is_civic else "Custodes Daily Note"
    cursor = await db.execute(
        """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (date_str, str(fp), project, now_ts, now_ts),
    )
    doc_id = int(cursor.lastrowid)
    await asyncio.to_thread(create_daily_note_file, fp, date_str, doc_id, civic=is_civic)
    return doc_id


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
    legion: str | None = None,
) -> tuple[int | None, str | None]:
    """Resolve launch-time session doc ownership with explicit precedence.

    Precedence:
    1. persona-default daily note (``personas.default_session_doc == 'daily_note'``)
    2. explicit dispatch doc
    3. active primarch doc
    4. active cron doc (or create one)
    5. generic interactive doc (top-level only)

    Automated/dispatched launches (legion/primarch/cron/explicit-but-unresolved)
    never fall through to the placeholder factory; they return
    ``(None, "unresolved_dispatch")`` so the orchestrator surfaces the miss
    instead of accumulating blank ``needs-session-name-N.md`` docs.
    """
    # Persona-default daily-note binding — the generalization of the old
    # custodes-only special case. ANY persona whose ``personas.default_session_doc``
    # is ``'daily_note'`` (Custodes, Fabricator-General, Administratum) co-binds
    # today's single shared ``Terra/Journal/Daily/YYYY-MM-DD.md``: Custodes owns
    # its semantics, FG/Admin ride the same infra. Resolved by persona at stamp
    # time (never a stored path) so it re-points cleanly across midnight.
    #
    # Legion- AND primarch_name-aware so automated launches (cron, GT/state-hook
    # dispatch, resume) that never set TOKEN_API_PERSONA still resolve identity.
    # primarch_name is checked first so a primarch-shaped launch wins, then legion.
    for candidate in (primarch_name, legion):
        if not candidate:
            continue
        persona = await resolve_persona(db, candidate)
        if persona and persona.get("default_session_doc") == "daily_note":
            # Civic seats (pax/orchestrator, legion=civic) bind the Pax-ENV daily
            # note; Custodes/FG/Admin with a personal cwd stay Imperium. Routing is
            # by work-class, so it survives the koronus→council pane migration.
            doc_id = await resolve_or_create_today_daily_note_session_doc(
                db, working_dir=working_dir, legion=legion
            )
            return doc_id, "daily_note"

    if dispatch_session_doc_path:
        fp = Path(dispatch_session_doc_path)
        if not fp.is_absolute():
            # Route a vault-relative dispatch path to the vault that owns this
            # work-class: a civic worker (askCivic worktree / legion civic|pax)
            # records "Sessions/<file>.md" but the file lives under Pax-ENV, not
            # Imperium. Absolute dispatch paths short-circuit above and skip
            # routing entirely.
            fp = vault_root_for(working_dir, legion) / dispatch_session_doc_path
        doc_id = await resolve_or_create_session_doc_for_path(db, fp)
        if doc_id:
            return doc_id, "dispatch_explicit"

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
        doc_title = cron_job_name or "cron"
        fp = unique_human_path(mars_sessions_dir(), doc_title, fallback="needs-session-name")
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

    # Automated/dispatched launches must not mint a placeholder. The generic
    # interactive branch below is the "name your own doc" flow for genuine human
    # sessions only (no dispatch metadata, no legion, no primarch, not cron). A
    # dispatched launch that reaches here either failed to resolve its explicit
    # doc or is a primarch with no active doc — surface that, don't paper over it.
    if dispatch_session_doc_path or legion or primarch_name or origin_type == "cron":
        return None, "unresolved_dispatch"

    # Genuine interactive pane: defer the placeholder. Minting at SessionStart
    # pollutes the vault with "Needs Session Name" docs for every pane that is
    # opened and closed without a prompt (stray tests, failed dispatches, idle
    # panes). The doc is minted lazily on the first real prompt instead — see
    # create_deferred_interactive_session_doc(), called from handle_prompt_submit.
    return None, "interactive_deferred"


async def create_deferred_interactive_session_doc(db: aiosqlite.Connection) -> int:
    """Mint the placeholder session doc for a genuine interactive pane.

    Deferred from SessionStart (``resolve_session_doc_for_start`` returns
    ``(None, "interactive_deferred")``) to the first prompt so opened-but-unused
    panes leave no doc behind. Mirrors the old eager interactive fall-through:
    a "Needs Session Name" doc the session is expected to rename via
    ``session-doc-name``. Caller is responsible for binding the returned id onto
    the instance row (pragma-once). Does not commit.
    """
    now_ts = datetime.now().isoformat()
    doc_title = "Needs Session Name"
    fp = unique_human_path(terra_sessions_dir(), doc_title, fallback="needs-session-name")
    cursor = await db.execute(
        """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (doc_title, str(fp), None, now_ts, now_ts),
    )
    doc_id = int(cursor.lastrowid)
    create_session_doc_file(fp, doc_title, doc_id)
    return doc_id
