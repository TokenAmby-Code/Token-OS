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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from pane_surface import is_placeholder_tab_name

logger = logging.getLogger(__name__)

_VAULT_ROOT = Path(os.environ.get("IMPERIUM_ENV", "")) if os.environ.get("IMPERIUM_ENV") else None
_IMPERIUM_ROOT = Path(os.environ.get("IMPERIUM", "/Volumes/Imperium"))
if not _IMPERIUM_ROOT.exists():
    _IMPERIUM_ROOT = Path.home()
if _VAULT_ROOT is None:
    _VAULT_ROOT = _IMPERIUM_ROOT / "Imperium-ENV"
TERRA_SESSIONS_DIR = _VAULT_ROOT / "Terra" / "Sessions"
MARS_SESSIONS_DIR = _VAULT_ROOT / "Mars" / "Sessions"
DAILY_NOTES_DIR = _VAULT_ROOT / "Terra" / "Journal" / "Daily"
OBSIDIAN_SYNC_ILLEGAL_FILENAME_CHARS = r'<>:"/\\|?*'


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

    The caller MUST serialize concurrent calls (the token-api endpoint holds an
    asyncio.Lock) — read-modify-write on the file is not atomic on its own, and
    the one-active invariant only holds if claims don't interleave.
    """
    if action not in ("claim", "archive"):
        raise ValueError(f"unknown action: {action!r}")
    if not path:
        raise ValueError(f"{action} requires path")

    fm, _body = read_frontmatter(file_path)
    wts = fm.get("worktrees")
    if not isinstance(wts, list):
        wts = []
    # Keep only well-formed dict entries; drop anything malformed.
    wts = [w for w in wts if isinstance(w, dict)]

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

    update_frontmatter(file_path, {"worktrees": wts})
    return wts


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
    # surfaces so Emperor edits gate victory immediately.
    derived_dirty = False

    # `commentary_resolved`: True iff fm['commentary'] is None. Emperor sets
    # `commentary: "..."` as an inbox; the next Astartes reconciles it and
    # clears the field.
    if "commentary_resolved" in rubric_value:
        if not derived_dirty:
            rubric_value = dict(rubric_value)
            derived_dirty = True
        rubric_value["commentary_resolved"] = fm.get("commentary") is None

    # `sanguinius_satisfied`: True iff a beautifier `<persona>_is` field
    # resolves to its terminal integer (>=3, "folding my wings"). Generic so
    # the same gate works under `designer_is` in vaults that swap the theme.
    if "sanguinius_satisfied" in rubric_value:
        if not derived_dirty:
            rubric_value = dict(rubric_value)
            derived_dirty = True
        _, state_int = resolve_beautifier_state(fm)
        rubric_value["sanguinius_satisfied"] = (
            state_int is not None and state_int >= BEAUTIFIER_TERMINAL_INT
        )

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


def update_rubric_field(
    file_path: Path,
    key: str,
    value: Any,
    rubric_key: str | None = None,
) -> dict:
    """Atomically set rubric[key] = value in the file's frontmatter.

    If the rubric is missing or legacy-scalar, the field is upgraded to a dict
    containing only the supplied key. Returns the updated frontmatter dict.
    """
    fm, body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    existing = fm.get(rk)
    if not isinstance(existing, dict):
        existing = {}
    upgraded = dict(existing)
    upgraded[key] = value
    fm[rk] = upgraded
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


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
    fm, body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    fm[_rubric_sibling(rk, "notified_at")] = datetime.now().isoformat()
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


def clear_rubric_notified(file_path: Path, rubric_key: str | None = None) -> dict:
    """Clear <rubric_key>_notified_at — used when a previously-complete rubric regresses."""
    fm, body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    fm[_rubric_sibling(rk, "notified_at")] = None
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


def mark_rubric_acknowledged(
    file_path: Path,
    reason: str,
    rubric_key: str | None = None,
) -> dict:
    """Stamp <rubric_key>_acknowledged_at + <rubric_key>_reason — final Emperor ack."""
    fm, body = read_frontmatter(file_path)
    rk = rubric_key or fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    now = datetime.now().isoformat()
    fm[_rubric_sibling(rk, "acknowledged_at")] = now
    fm[_rubric_sibling(rk, "reason")] = reason
    new_content = serialize_frontmatter(fm, body)
    file_path.write_text(new_content, encoding="utf-8")
    return fm


def bump_session_doc_up_to_date(file_path: Path, value: bool) -> dict | None:
    """Flip the session_doc_up_to_date inverse flag on a session doc rubric.

    Convention: UserPromptSubmit hook sets this False each turn; a Write/Edit
    that targets the doc flips it back True. Agents that end a turn without
    touching their doc trip GT next cycle. Silent guard on the happy path.

    Returns None if the doc has no rubric (legacy/string victory or missing).
    """
    try:
        fm, body = read_frontmatter(file_path)
    except FileNotFoundError:
        return None
    rk = fm.get("rubric_key") or DEFAULT_RUBRIC_KEY
    existing = fm.get(rk)
    if not isinstance(existing, dict):
        return None
    if existing.get("session_doc_up_to_date") == value:
        return fm
    upgraded = dict(existing)
    upgraded["session_doc_up_to_date"] = value
    fm[rk] = upgraded
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
    """Create the markdown file for a session document.

    Seeds the typed victory rubric (see DEFAULT_SESSION_DOC_RUBRIC) so the
    Golden Throne accountability engine can read specific unmet conditions
    instead of firing a generic resume prompt.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    fm: dict[str, Any] = {
        "session_doc_id": doc_id,
        "created": today,
    }
    if project:
        fm["project"] = project
    fm["agents"] = []
    fm["instance_ids"] = []
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


def create_daily_note_file(file_path: Path, date_str: str, doc_id: int) -> None:
    """Create the Custodes daily-note home on demand.

    Custodes continuity is daily-note backed, not an interactive session-doc
    placeholder.  Keep the shape intentionally plain so existing journal notes
    remain valid Obsidian daily notes while still carrying session_doc_id for
    Token-API binding.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict[str, Any] = {
        "session_doc_id": doc_id,
        "created": date_str,
        "date": date_str,
        "type": "daily-note",
        "status": "active",
        "legion": "custodes",
        "agents": [],
        "instance_ids": [],
    }
    body = f"# {date_str}\n\n## Custodes\n\n## Log\n"
    file_path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")


async def resolve_or_create_today_daily_note_session_doc(db, date_str: str | None = None) -> int:
    """Return today's Custodes daily note, creating note and DB row if absent."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    fp = (DAILY_NOTES_DIR / f"{date_str}.md").resolve()
    existing = await resolve_or_create_session_doc_for_path(db, fp)
    if existing:
        return existing

    now_ts = datetime.now().isoformat()
    cursor = await db.execute(
        """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (date_str, str(fp), "Custodes Daily Note", now_ts, now_ts),
    )
    doc_id = int(cursor.lastrowid)
    await asyncio.to_thread(create_daily_note_file, fp, date_str, doc_id)
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
    1. Custodes daily note
    2. explicit dispatch doc
    3. active primarch doc
    4. active cron doc (or create one)
    5. generic interactive doc (top-level only)

    Automated/dispatched launches (legion/primarch/cron/explicit-but-unresolved)
    never fall through to the placeholder factory; they return
    ``(None, "unresolved_dispatch")`` so the orchestrator surfaces the miss
    instead of accumulating blank ``needs-session-name-N.md`` docs.
    """
    # Legion-aware so automated custodes launches (cron, GT/state-hook dispatch,
    # resume) that never set TOKEN_API_PRIMARCH still bind to the shared daily note.
    if primarch_name == "custodes" or legion == "custodes":
        doc_id = await resolve_or_create_today_daily_note_session_doc(db)
        return doc_id, "daily_note_custodes"

    if dispatch_session_doc_path:
        fp = Path(dispatch_session_doc_path)
        if not fp.is_absolute():
            fp = _VAULT_ROOT / dispatch_session_doc_path
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
        fp = unique_human_path(MARS_SESSIONS_DIR, doc_title, fallback="needs-session-name")
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

    now_ts = datetime.now().isoformat()
    # No cwd/date fallback names. A new interactive session gets a placeholder
    # and is expected to name its own session doc via `session-doc-name`.
    doc_title = "Needs Session Name"
    fp = unique_human_path(TERRA_SESSIONS_DIR, doc_title, fallback="needs-session-name")
    cursor = await db.execute(
        """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?)""",
        (doc_title, str(fp), None, now_ts, now_ts),
    )
    doc_id = cursor.lastrowid
    create_session_doc_file(fp, doc_title, doc_id)
    return doc_id, "interactive_auto"
