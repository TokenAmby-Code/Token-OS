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


def test_scribe_is_a_valid_registry_and_schema_rank(tmp_path: Path) -> None:
    assert instance_registry.normalize_rank("scribe") == "scribe"
    db_path = tmp_path / "agents.db"
    db_schema.init_database_sync(db_path)
    conn = sqlite3.connect(db_path)
    try:
        persona_id = personas.persona_id_for_slug("administratum")
        conn.execute(
            "INSERT INTO instances (id, device_id, persona_id, rank) VALUES (?, ?, ?, ?)",
            ("scribe-instance", "test", persona_id, "scribe"),
        )
    finally:
        conn.close()


def test_administratum_is_seeded_and_asserted_as_scribe_not_overseer() -> None:
    seed = personas.SEED_BY_SLUG["administratum"]
    assert seed.default_rank == "scribe"
    assert seed.default_rank != "overseer"
    assert EXPECTED_PERSONA_RANKS["administratum"] == "scribe"
    assert "'scribe'" in personas.persona_schema_sql()
