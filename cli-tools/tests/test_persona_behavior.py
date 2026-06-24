"""Unit tests for the rank+persona system-doc staple builder.

The staple is the infra-level invariant: every managed fleet instance is born
with a `rank doc + persona doc` system briefing, rank doc FIRST. These tests pin
the resolution, extraction, staple order, and the fail-closed invariant so the
launch paths (workers, codex, singletons) can all share one code path.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from persona_behavior import (  # noqa: E402
    PersonaRow,
    extract_body,
    invariant_issues,
    rank_file_for,
    system_doc_for,
)


def _persona_db(tmp_path: pathlib.Path, rows: list[tuple[str, str, str]]) -> pathlib.Path:
    db = tmp_path / "agents.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE personas (id TEXT PRIMARY KEY, slug TEXT UNIQUE, "
            "display_name TEXT, default_rank TEXT)"
        )
        for idx, (slug, display, rank) in enumerate(rows):
            conn.execute(
                "INSERT INTO personas (id, slug, display_name, default_rank) VALUES (?, ?, ?, ?)",
                (f"p{idx}", slug, display, rank),
            )
    return db


@pytest.fixture
def imperium(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A temp Imperium vault root with the Personas + Personas/Ranks layout."""
    root = tmp_path / "vaults" / "Imperium" / "Imperium-ENV"
    (root / "Personas" / "Ranks").mkdir(parents=True)
    civic = tmp_path / "vaults" / "Civic" / "Pax-ENV"
    (civic / "Personas" / "Ranks").mkdir(parents=True)
    monkeypatch.setenv("IMPERIUM", str(tmp_path / "vaults" / "Imperium"))
    monkeypatch.setenv("CIVIC", str(tmp_path / "vaults" / "Civic"))
    return root


def _write_rank(root: pathlib.Path, name: str, body: str) -> None:
    (root / "Personas" / "Ranks" / f"{name}.md").write_text(body, encoding="utf-8")


def _write_persona(root: pathlib.Path, name: str, body: str) -> None:
    (root / "Personas" / f"{name}.md").write_text(body, encoding="utf-8")


# --- rank_file_for ----------------------------------------------------------


@pytest.mark.parametrize(
    ("rank", "expected"),
    [
        ("overseer", "Overseer.md"),
        ("astartes", "Astartes.md"),
        ("primarch", "Primarch.md"),
    ],
)
def test_rank_file_for_resolves_each_rank(imperium, rank, expected) -> None:
    _write_rank(imperium, expected[:-3], f"{rank} rank doctrine")
    row = PersonaRow("custodes", "Custodes", rank)
    resolved = rank_file_for(row)
    assert resolved is not None
    assert resolved.name == expected


def test_rank_file_for_is_case_insensitive(imperium) -> None:
    _write_rank(imperium, "Overseer", "overseer rank doctrine")
    # default_rank stored upper/mixed case still resolves Overseer.md
    row = PersonaRow("custodes", "Custodes", "OVERSEER")
    resolved = rank_file_for(row)
    assert resolved is not None
    assert resolved.name == "Overseer.md"


def test_rank_file_for_returns_none_when_missing(imperium) -> None:
    row = PersonaRow("custodes", "Custodes", "overseer")
    assert rank_file_for(row) is None


def test_rank_file_for_uses_pax_root_for_orchestrator(imperium, tmp_path) -> None:
    civic = tmp_path / "vaults" / "Civic" / "Pax-ENV"
    (civic / "Personas" / "Ranks" / "Overseer.md").write_text("pax overseer", encoding="utf-8")
    row = PersonaRow("pax", "Pax", "overseer")
    resolved = rank_file_for(row)
    assert resolved is not None
    assert "Pax-ENV" in str(resolved)


# --- extract_body -----------------------------------------------------------


def test_extract_body_prefers_system_prompt_section(tmp_path) -> None:
    p = tmp_path / "x.md"
    p.write_text(
        "# Heading\nintro\n\n## System Prompt\nthe real prompt\n\n## Other\nignored\n",
        encoding="utf-8",
    )
    assert extract_body(p) == "the real prompt"


def test_extract_body_strips_frontmatter_when_no_marker(tmp_path) -> None:
    p = tmp_path / "x.md"
    p.write_text(
        "---\ntitle: Custodes\ntags: [a]\n---\n\n# Custodes\nbody line\n",
        encoding="utf-8",
    )
    body = extract_body(p)
    assert "title: Custodes" not in body
    assert "# Custodes" in body
    assert "body line" in body


def test_extract_body_whole_body_when_no_marker_no_frontmatter(tmp_path) -> None:
    p = tmp_path / "rank.md"
    p.write_text("## Overseer Mandate\nrank doctrine here\n", encoding="utf-8")
    assert extract_body(p) == "## Overseer Mandate\nrank doctrine here"


# --- system_doc_for ---------------------------------------------------------


def test_system_doc_for_staples_rank_first(imperium, tmp_path, monkeypatch) -> None:
    db = _persona_db(tmp_path, [("custodes", "Custodes", "overseer")])
    monkeypatch.setenv("TOKEN_API_DB", str(db))
    _write_rank(imperium, "Overseer", "RANK DOCTRINE BODY")
    _write_persona(imperium, "Custodes", "---\ntitle: Custodes\n---\n\nPERSONA DOCTRINE BODY")

    doc = system_doc_for("custodes")
    assert doc is not None
    assert "RANK DOCTRINE BODY" in doc
    assert "PERSONA DOCTRINE BODY" in doc
    # rank-first: rank body comes before persona body, separated by a divider
    assert doc.index("RANK DOCTRINE BODY") < doc.index("PERSONA DOCTRINE BODY")
    assert "\n\n---\n\n" in doc


def test_system_doc_for_returns_none_for_unknown_persona(imperium, tmp_path, monkeypatch) -> None:
    db = _persona_db(tmp_path, [("custodes", "Custodes", "overseer")])
    monkeypatch.setenv("TOKEN_API_DB", str(db))
    assert system_doc_for("nonexistent-persona") is None


def test_system_doc_for_returns_none_when_rank_doc_missing(imperium, tmp_path, monkeypatch) -> None:
    db = _persona_db(tmp_path, [("custodes", "Custodes", "overseer")])
    monkeypatch.setenv("TOKEN_API_DB", str(db))
    # persona behavior file present, but no Ranks/Overseer.md
    _write_persona(imperium, "Custodes", "PERSONA BODY")
    assert system_doc_for("custodes") is None


# --- invariant_issues -------------------------------------------------------


def test_invariant_fires_on_missing_rank_doc(imperium, tmp_path, monkeypatch) -> None:
    db = _persona_db(tmp_path, [("custodes", "Custodes", "overseer")])
    monkeypatch.setenv("TOKEN_API_DB", str(db))
    # behavior file present so the OLD invariant would pass; rank doc absent
    _write_persona(imperium, "Custodes", "PERSONA BODY")
    issues = invariant_issues(db)
    assert any("rank doc missing" in issue for issue in issues), issues


def test_invariant_passes_when_both_present(imperium, tmp_path, monkeypatch) -> None:
    db = _persona_db(tmp_path, [("custodes", "Custodes", "overseer")])
    monkeypatch.setenv("TOKEN_API_DB", str(db))
    _write_persona(imperium, "Custodes", "PERSONA BODY")
    _write_rank(imperium, "Overseer", "RANK BODY")
    assert invariant_issues(db) == []
