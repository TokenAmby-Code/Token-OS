"""CIVIC (Pax-ENV) vault routing for session-doc resolution.

Session-doc resolution was hardwired to the single Imperium vault. Civic work
(``/Volumes/Civic/Pax-ENV``, ``~/worktrees/askCivic/*``, legion ``civic``/``pax``)
must bind its docs under the Pax-ENV vault instead. Routing keys on work-class
(``working_dir`` + ``legion``), never on ``koronus:*`` pane labels, so it survives
the council pane migration.

Covered here:
- ``vault_root_for`` / ``daily_notes_dir_for`` route civic vs Imperium by work-class.
- Regression for the dispatched-civic-worker bug (relative ``Sessions/<file>.md``
  resolved under Imperium where the file does not exist → ``unresolved_dispatch``).
- Civic daily-note resolution (pre-existing morning note + missing-note fallback),
  with the Imperium/Custodes path unchanged.
- pax/orchestrator persona seeds carry ``default_session_doc='daily_note'`` and the
  upsert backfills the live row.
- The live-vault tripwire guards the civic vault too.
"""

import os
from pathlib import Path

import aiosqlite
import pytest

LIVE_IMPERIUM = Path("/Volumes/Imperium/Imperium-ENV")
LIVE_CIVIC = Path("/Volumes/Civic/Pax-ENV")


def _askcivic_worktree() -> str:
    """An absolute path under the askCivic worktree parent (~/worktrees/askCivic)."""
    return os.path.join(os.path.expanduser("~"), "worktrees", "askCivic", "wt-some-bug")


def _under(child: Path, parent: Path) -> bool:
    try:
        resolved = Path(child).resolve()
    except OSError:
        resolved = Path(child)
    return resolved == parent or parent in resolved.parents


# ── Unit: vault_root_for / daily_notes_dir_for routing ───────────────────────


def test_vault_root_for_routes_civic_paths_to_pax_env():
    import session_doc_helpers as sdh

    civic = sdh.civic_vault_root()
    assert sdh.vault_root_for("/Volumes/Civic/Pax-ENV/Sessions") == civic
    assert sdh.vault_root_for(_askcivic_worktree()) == civic
    assert sdh.vault_root_for(None, "civic") == civic
    assert sdh.vault_root_for(None, "pax") == civic


def test_vault_root_for_routes_personal_and_unknown_to_imperium():
    import session_doc_helpers as sdh

    imperium = sdh.vault_root()
    assert sdh.vault_root_for("/Volumes/Imperium/Imperium-ENV/Terra") == imperium
    assert sdh.vault_root_for(None, "custodes") == imperium
    # UNKNOWN (no working_dir, no legion) → Imperium: the safe default that
    # preserves the pre-existing mono-vault behavior.
    assert sdh.vault_root_for(None, None) == imperium


def test_vault_root_for_path_beats_personal_legion():
    """The off-page-worker case: a civic worktree with a personal legion still
    routes civic, because billable-by-path wins in classify_work_class."""
    import session_doc_helpers as sdh

    assert sdh.vault_root_for("/Volumes/Civic/x", "astartes") == sdh.civic_vault_root()


def test_daily_notes_dir_for_civic_has_no_terra_segment():
    import session_doc_helpers as sdh

    civic = sdh.daily_notes_dir_for(None, "civic")
    assert civic.parts[-2:] == ("Journal", "Daily")
    assert civic.parent.name == "Journal"
    assert str(civic).endswith(os.path.join("Pax-ENV", "Journal", "Daily"))
    assert "Terra" not in civic.parts
    # Resolves under the isolated temp civic vault, never the live one.
    assert not _under(civic, LIVE_CIVIC)


def test_daily_notes_dir_for_personal_keeps_terra_segment():
    import session_doc_helpers as sdh

    personal = sdh.daily_notes_dir_for(None, None)
    assert personal.parts[-3:] == ("Terra", "Journal", "Daily")
    assert not _under(personal, LIVE_IMPERIUM)
    # daily_notes_dir() is the no-arg (Imperium) case — one source of truth.
    assert sdh.daily_notes_dir() == personal


# ── Integration: original dispatched-civic-worker bug (regression) ───────────


@pytest.mark.asyncio
async def test_dispatched_civic_worker_resolves_under_pax_env(app_env, tmp_path):
    """A civic worker records a vault-relative ``Sessions/<file>.md`` while working
    out of an askCivic worktree. Pre-fix it resolved under Imperium (file absent) →
    ``unresolved_dispatch`` and a NULL session_doc_id. It must now resolve under the
    Pax-ENV vault, where the file lives."""
    helpers = __import__("session_doc_helpers")
    civic_vault = helpers.civic_vault_root()  # CIVIC_ENV → tmp/Pax-ENV (app_env)

    note = civic_vault / "Sessions" / "foo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntitle: Foo\n---\n", encoding="utf-8")

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path="Sessions/foo.md",
            primarch_name=None,
            origin_type="dispatch",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=_askcivic_worktree(),
            is_subagent=False,
            legion="astartes",
        )
        await db.commit()

        assert reason == "dispatch_explicit"
        assert doc_id is not None
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()
        assert row[0] == str(note.resolve())

    # session_doc_id backfilled into the note frontmatter.
    fm, _ = helpers.read_frontmatter(note)
    assert fm.get("session_doc_id") == doc_id


# ── Integration: civic daily-note resolution ────────────────────────────────


@pytest.mark.asyncio
async def test_pax_binds_existing_civic_daily_note(app_env):
    """Common case: morning_session already created today's Pax-ENV daily note.
    The pax seat binds it (DB row + session_doc_id backfill), and the note is NOT
    rewritten with the Imperium/Custodes shape (no ``legion: custodes``)."""
    helpers = __import__("session_doc_helpers")
    civic_vault = helpers.civic_vault_root()
    date_str = f"{helpers.datetime.now():%Y-%m-%d}"

    note = civic_vault / "Journal" / "Daily" / f"{date_str}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        f"---\ntitle: {date_str}\ntype: daily\ntags: [daily, civic]\n---\n# {date_str}\n\n## Log\n",
        encoding="utf-8",
    )

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=None,
            primarch_name="pax",
            origin_type="interactive",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=None,
            is_subagent=False,
            legion="civic",
        )
        await db.commit()

        assert reason == "daily_note"
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()
        assert row[0] == str(note.resolve())

    fm, _ = helpers.read_frontmatter(note)
    assert fm.get("session_doc_id") == doc_id
    assert fm.get("type") == "daily"
    assert fm.get("legion") != "custodes"
    assert "legion" not in fm


@pytest.mark.asyncio
async def test_orchestrator_creates_civic_daily_note_when_missing(app_env):
    """Fallback: no morning note yet → create_daily_note_file(civic=True) writes the
    minimal Pax-ENV shape (``type: daily``, ``tags: [daily, civic]``, no
    ``legion``/``agents``)."""
    helpers = __import__("session_doc_helpers")
    civic_vault = helpers.civic_vault_root()
    date_str = f"{helpers.datetime.now():%Y-%m-%d}"
    note = civic_vault / "Journal" / "Daily" / f"{date_str}.md"
    assert not note.exists()

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=None,
            primarch_name="orchestrator",
            origin_type="interactive",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=None,
            is_subagent=False,
            legion="civic",
        )
        await db.commit()
        assert reason == "daily_note"

    assert note.exists()
    fm, _ = helpers.read_frontmatter(note)
    assert fm.get("session_doc_id") == doc_id
    assert fm.get("type") == "daily"
    assert "civic" in fm.get("tags", [])
    assert "legion" not in fm
    assert "agents" not in fm


@pytest.mark.asyncio
async def test_custodes_daily_note_unchanged_by_civic_routing(app_env):
    """Imperium regression: Custodes with a personal cwd still binds the Imperium
    daily note in the Custodes shape (``type: daily-note``), under Terra/Journal."""
    helpers = __import__("session_doc_helpers")
    imperium_vault = helpers.vault_root()
    date_str = f"{helpers.datetime.now():%Y-%m-%d}"

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=None,
            primarch_name="custodes",
            origin_type="interactive",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=None,
            is_subagent=False,
        )
        await db.commit()
        assert reason == "daily_note"
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()

    note = imperium_vault / "Terra" / "Journal" / "Daily" / f"{date_str}.md"
    assert row[0] == str(note.resolve())
    fm, _ = helpers.read_frontmatter(note)
    assert fm.get("type") == "daily-note"
    assert fm.get("legion") == "custodes"


# ── Seed application ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", ["pax", "orchestrator"])
@pytest.mark.asyncio
async def test_civic_seats_seed_daily_note_default(app_env, slug):
    import personas

    async with aiosqlite.connect(app_env.db_path) as db:
        persona = await personas.resolve_persona(db, slug)
    assert persona is not None
    assert persona["default_session_doc"] == "daily_note"


@pytest.mark.asyncio
async def test_seed_personas_backfills_daily_note_onto_live_row(app_env):
    """Load-bearing: a pre-existing pax row at the canonical uuid5 id with a NULL
    default_session_doc must be UPDATED to 'daily_note' by the upsert (ON
    CONFLICT(id) DO UPDATE), proving the seed change reaches live rows on reboot."""
    import personas

    pax_id = personas.persona_id_for_slug("pax")
    async with aiosqlite.connect(app_env.db_path) as db:
        await db.execute("UPDATE personas SET default_session_doc = NULL WHERE id = ?", (pax_id,))
        await db.commit()
        pre = await (
            await db.execute("SELECT default_session_doc FROM personas WHERE id = ?", (pax_id,))
        ).fetchone()
        assert pre[0] is None

        await personas.seed_personas(db)
        await db.commit()

        post = await (
            await db.execute("SELECT default_session_doc FROM personas WHERE id = ?", (pax_id,))
        ).fetchone()
    assert post[0] == "daily_note"


# ── Tripwire: civic vault is guarded too ─────────────────────────────────────


def test_create_civic_daily_note_into_live_vault_raises():
    """The live-vault tripwire now guards the civic vault: writing a civic daily
    note into /Volumes/Civic/Pax-ENV under pytest must raise, not pollute."""
    import session_doc_helpers as sdh

    live_target = LIVE_CIVIC / "Journal" / "Daily" / "pytest-tripwire.md"
    with pytest.raises(RuntimeError, match="LIVE vault"):
        sdh.create_daily_note_file(live_target, "2026-06-24", 1, civic=True)
    assert not live_target.exists()
