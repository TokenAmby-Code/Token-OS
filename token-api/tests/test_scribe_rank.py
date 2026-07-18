from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO_ROOT / "tmuxctld" / "lib"))

from tmuxctl.assertions import EXPECTED_PERSONA_RANKS  # noqa: E402

import db_schema  # noqa: E402
import instance_registry  # noqa: E402
import personas  # noqa: E402
from instance_mutation import insert_instance_sync  # noqa: E402


def test_scribe_is_a_valid_registry_and_schema_rank(tmp_path: Path) -> None:
    assert instance_registry.normalize_rank("scribe") == "scribe"
    db_path = tmp_path / "agents.db"
    db_schema.init_database_sync(db_path)
    conn = sqlite3.connect(db_path)
    try:
        persona_id = personas.persona_id_for_slug("administratum")
        insert_instance_sync(
            conn,
            values={
                "id": "scribe-instance",
                "device_id": "test",
                "persona_id": persona_id,
                "rank": "scribe",
            },
            mutation_type="test_seed",
            write_source="test_scribe_rank",
            actor="pytest",
        )
    finally:
        conn.close()


def test_administratum_is_seeded_and_asserted_as_scribe_not_overseer() -> None:
    seed = personas.SEED_BY_SLUG["administratum"]
    assert seed.default_rank == "scribe"
    assert seed.default_rank != "overseer"
    assert EXPECTED_PERSONA_RANKS["administratum"] == "scribe"
    assert "'scribe'" in personas.persona_schema_sql()


def test_legacy_rank_constraints_rebuild_without_losing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-agents.db"
    db_schema.init_database_sync(db_path)
    conn = sqlite3.connect(db_path)
    try:
        insert_instance_sync(
            conn,
            values={"id": "preserved", "device_id": "test", "rank": "overseer"},
            mutation_type="test_seed",
            write_source="test_scribe_rank",
            actor="pytest",
        )
        conn.commit()
        conn.execute("PRAGMA writable_schema=ON")
        for table in ("personas", "instances"):
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            legacy_sql = sql.replace("'scribe',", "")
            conn.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                (legacy_sql, table),
            )
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
    finally:
        conn.close()

    db_schema.init_database_sync(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT rank FROM instances WHERE id='preserved'").fetchone() == (
            "overseer",
        )
        assert conn.execute(
            "SELECT default_rank FROM personas WHERE slug='administratum'"
        ).fetchone() == ("scribe",)
        assert all(
            "'scribe'"
            in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            for table in ("personas", "instances")
        )
        insert_instance_sync(
            conn,
            values={"id": "accepted-scribe", "device_id": "test", "rank": "scribe"},
            mutation_type="test_assertion",
            write_source="test_scribe_rank",
            actor="pytest",
        )
    finally:
        conn.close()
