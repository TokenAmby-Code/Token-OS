"""Resolve a merged PR's branch to its session doc and extract review material.

The mapping is heuristic — session docs carry no direct ``branch:`` field. We
slugify the PR ``headRefName`` (port of ``slugify_branch`` at
``cli-tools/bin/dispatch:951``) and match it against the ``session_documents``
table's ``file_path`` / ``project`` columns (worktree dispatch stores the project
as ``wt-<branch>``). Frontmatter is parsed the same way as
``token-api/session_doc_helpers.parse_frontmatter``.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# Headings the assembler transcludes per thread, when present in the doc.
KEY_FILES_HEADINGS = ("Key Files", "Key files")
CORE_CHANGE_HEADINGS = ("Changes Made", "Implementation", "Changes", "What Changed")


def slugify_branch(value: str) -> str:
    """Port of ``slugify_branch`` (cli-tools/bin/dispatch:951)."""
    slug = value.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:48]


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter_dict, body)``.

    Mirrors ``token-api/session_doc_helpers.parse_frontmatter`` — same fence
    handling so behaviour matches the rest of the system.
    """
    if not content.startswith("---"):
        return {}, content
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return {}, content
    yaml_block = content[3:end_idx].strip()
    body_start = end_idx + 4  # skip "\n---"
    if body_start < len(content) and content[body_start] == "\n":
        body_start += 1
    body = content[body_start:]
    try:
        fm = yaml.safe_load(yaml_block)
        if not isinstance(fm, dict):
            return {}, content
    except yaml.YAMLError:
        return {}, content
    return fm, body


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    return parse_frontmatter(content)


def resolve_doc_for_branch(db_path: Path, head_ref: str) -> str | None:
    """Find the session doc matching a branch slug. Returns abs file_path or None.

    Prefers an exact ``project`` match (``<slug>`` or ``wt-<slug>``), else the
    most-recently-created substring match across ``file_path``/``project``.
    """
    slug = slugify_branch(head_ref)
    if not slug:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        like = f"%{slug}%"
        rows = conn.execute(
            """
            SELECT file_path, project, id
            FROM session_documents
            WHERE (file_path LIKE ? OR project LIKE ?)
              AND COALESCE(status, '') != 'deleted'
            ORDER BY id DESC
            """,
            (like, like),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    for row in rows:
        project = (row["project"] or "").lower()
        if project in (slug, f"wt-{slug}"):
            return row["file_path"]
    return rows[0]["file_path"]


def extract_headings(body: str) -> list[str]:
    """All ``##``+ heading texts (the text after the ``#`` run)."""
    headings = []
    for line in body.splitlines():
        match = re.match(r"^#{2,6}\s+(.*\S)\s*$", line)
        if match:
            headings.append(match.group(1).strip())
    return headings


def section_body(body: str, heading_text: str) -> str:
    """Body lines under ``heading_text`` up to the next same-or-higher heading."""
    out: list[str] = []
    capturing = False
    capture_level = 0
    for line in body.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            level = len(match.group(1))
            text = match.group(2).strip()
            if capturing and level <= capture_level:
                break
            if not capturing and text == heading_text:
                capturing = True
                capture_level = level
                continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def first_present(headings: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {h.lower(): h for h in headings}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def extract_key_files(body: str) -> list[str]:
    """File paths called out under the doc's ``## Key Files`` section, in order."""
    heading = first_present(extract_headings(body), KEY_FILES_HEADINGS)
    if not heading:
        return []
    section = section_body(body, heading)
    paths: list[str] = []
    for token in re.findall(r"`([^`]+)`", section):
        token = token.strip()
        if " " in token:  # inline code like `_try_discord_injection`, not a path
            continue
        if "/" in token or re.search(r"\.\w{1,5}$", token):
            paths.append(token)
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _wikilink_targets(body: str) -> list[str]:
    """Bare ``[[target]]`` / ``[[target|alias]]`` targets in the body (no embeds)."""
    targets = []
    for raw in re.findall(r"(?<!\!)\[\[([^\]]+)\]\]", body):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def _resolve_vault_note(reference: str, vault_root: Path) -> Path | None:
    """Best-effort resolve a wikilink/path target to a vault .md file."""
    ref = str(reference).strip().strip("[]")
    if not ref:
        return None
    candidates = []
    direct = vault_root / (ref if ref.endswith(".md") else f"{ref}.md")
    candidates.append(direct)
    if direct.exists():
        return direct
    # Fall back to a basename search across the session dirs.
    stem = Path(ref).stem
    for base in ("Mars/Sessions", "Terra/Sessions"):
        hit = vault_root / base / f"{stem}.md"
        if hit.exists():
            return hit
    return None


_SESSION_DIRS = ("Mars/Sessions", "Terra/Sessions")


DIAGRAM_SCAN_TIMEOUT_S = 60


def _reference_doc_candidates(vault_root: Path) -> list[Path] | None:
    """Fast prefilter for ``type: reference`` docs via grep.

    The vault holds thousands of session docs; reference (diagram) docs are a
    handful. grep scans them in one C-level pass instead of opening every file
    from Python (which is brutal over SMB/NAS). Returns ``None`` if grep is
    unavailable so the caller can fall back to a full glob scan. A wedged NAS
    mount can pin grep in uninterruptible I/O forever — on timeout we skip
    diagram enrichment entirely (``[]``, NOT the glob fallback, which would
    hang against the same mount) so the build still lands.
    """
    dirs = [str(vault_root / base) for base in _SESSION_DIRS if (vault_root / base).is_dir()]
    if not dirs:
        return []
    try:
        proc = subprocess.run(
            ["grep", "-rlE", r"^type:[[:space:]]*reference[[:space:]]*$", "--include=*.md", *dirs],
            capture_output=True,
            text=True,
            timeout=DIAGRAM_SCAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[daily-build] warning: vault diagram scan exceeded {DIAGRAM_SCAN_TIMEOUT_S}s "
            "(wedged NAS mount?) — skipping diagram enrichment for this build.",
            file=sys.stderr,
        )
        return []
    except OSError:
        return None
    if proc.returncode not in (0, 1):  # 0 = matches, 1 = no matches (both fine)
        return None
    return [Path(line) for line in proc.stdout.splitlines() if line.strip()]


def build_diagram_index(vault_root: Path) -> dict[str, list[Path]]:
    """Map ``session_doc_stem -> [reference-doc paths]`` via reverse links.

    Sanguinius diagram sets are ``type: reference`` docs that list their source
    session doc in ``related_session_docs`` (e.g.
    ``Mars/Sessions/2026-06-06-gt-harness-diagrams.md``). A grep prefilter finds
    the reference docs, then we parse only those to build the reverse index.
    """
    candidates = _reference_doc_candidates(vault_root)
    if candidates is None:  # grep unavailable — full glob scan fallback
        candidates = []
        for base in _SESSION_DIRS:
            directory = vault_root / base
            if directory.is_dir():
                candidates.extend(directory.glob("*.md"))

    index: dict[str, list[Path]] = {}
    for path in candidates:
        fm, _ = read_frontmatter(path)
        if str(fm.get("type") or "").lower() != "reference":
            continue
        related = fm.get("related_session_docs") or []
        if isinstance(related, str):
            related = [related]
        for rel in related:
            stem = Path(str(rel)).stem
            index.setdefault(stem, []).append(path)
    return index


def diagram_docs_for(
    doc_path: Path,
    fm: dict[str, Any],
    body: str,
    vault_root: Path,
    reverse_index: dict[str, list[Path]],
) -> list[Path]:
    """Reference (diagram) docs related to a session doc.

    Union of: forward links in ``related_session_docs`` + body wikilinks that
    resolve to ``type: reference`` docs + the reverse index (diagram → session).
    """
    found: dict[str, Path] = {}

    forward = fm.get("related_session_docs") or []
    if isinstance(forward, str):
        forward = [forward]
    for rel in list(forward) + _wikilink_targets(body):
        cand = _resolve_vault_note(rel, vault_root)
        if cand and cand.exists():
            rfm, _ = read_frontmatter(cand)
            if str(rfm.get("type") or "").lower() == "reference":
                found[cand.stem] = cand

    for cand in reverse_index.get(doc_path.stem, []):
        found[cand.stem] = cand

    return list(found.values())


def diagram_sections(diagram_path: Path) -> list[str]:
    """Heading texts of a diagram doc whose section embeds an image/diagram.

    Filters to sections that actually contain ``![[...]]`` embeds or a mermaid
    block, so we transclude the diagrams (``## I``, ``## II``, …) and skip the
    prose intro.
    """
    _, body = read_frontmatter(diagram_path)
    out = []
    for heading in extract_headings(body):
        section = section_body(body, heading)
        if "![[" in section or "```mermaid" in section:
            out.append(heading)
    return out
