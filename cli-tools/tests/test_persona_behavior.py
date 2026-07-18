from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import persona_behavior  # noqa: E402


def make_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE personas (slug TEXT, display_name TEXT, default_rank TEXT)")
    conn.executemany("INSERT INTO personas VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_imperium_persona_and_rank_resolve_from_token_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = tmp_path / "Token-Fleet"
    personas = fleet / "shared" / "personas"
    (personas / "Ranks").mkdir(parents=True)
    (personas / "Custodes.md").write_text("custodes", encoding="utf-8")
    (personas / "Ranks" / "Overseer.md").write_text("overseer", encoding="utf-8")
    db = tmp_path / "agents.db"
    make_db(db, [("custodes", "Custodes", "overseer")])
    monkeypatch.setenv("TOKEN_FLEET_CHECKOUT", str(fleet))

    row = persona_behavior.resolve_persona("custodes", db)
    assert row is not None
    assert persona_behavior.resolve_behavior_file("custodes", db) == personas / "Custodes.md"
    assert persona_behavior.rank_file_for(row) == personas / "Ranks" / "Overseer.md"
    assert persona_behavior.invariant_issues(db) == []


def test_missing_persona_doc_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = tmp_path / "Token-Fleet"
    (fleet / "shared" / "personas" / "Ranks").mkdir(parents=True)
    db = tmp_path / "agents.db"
    make_db(db, [("missing", "Missing", "astartes")])
    monkeypatch.setenv("TOKEN_FLEET_CHECKOUT", str(fleet))

    issues = persona_behavior.invariant_issues(db)
    assert any("persona behavior file missing: slug=missing" in issue for issue in issues)
    assert any("persona rank doc missing: slug=missing" in issue for issue in issues)


def test_fleet_persona_root_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = tmp_path / "Token-Fleet"
    monkeypatch.setenv("TOKEN_FLEET_CHECKOUT", str(fleet))
    assert persona_behavior._imperium_root() == fleet / "shared" / "personas"


def test_administratum_resolves_scribe_rank_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = tmp_path / "Token-Fleet"
    personas = fleet / "shared" / "personas"
    (personas / "Ranks").mkdir(parents=True)
    (personas / "Administratum.md").write_text("recorder", encoding="utf-8")
    (personas / "Ranks" / "Scribe.md").write_text("scribe", encoding="utf-8")
    db = tmp_path / "agents.db"
    make_db(db, [("administratum", "Administratum", "scribe")])
    monkeypatch.setenv("TOKEN_FLEET_CHECKOUT", str(fleet))

    row = persona_behavior.resolve_persona("administratum", db)
    assert row is not None
    assert persona_behavior.rank_file_for(row) == personas / "Ranks" / "Scribe.md"
    assert persona_behavior.system_doc_for("administratum", db) == "scribe\n\n---\n\nrecorder"
    assert persona_behavior.invariant_issues(db) == []
