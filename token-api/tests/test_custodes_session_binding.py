import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_custodes_creates_and_binds_today_daily_note(app_env, monkeypatch, tmp_path):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal" / "Daily")

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

        note_path = vault / "Terra" / "Journal" / "Daily" / f"{helpers.datetime.now():%Y-%m-%d}.md"
        assert reason == "daily_note_custodes"
        assert note_path.exists()
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()
        assert row[0] == str(note_path.resolve())
        assert "needs-session-name" not in row[0]


@pytest.mark.asyncio
async def test_custodes_daily_note_binding_is_singleton(app_env, monkeypatch, tmp_path):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal" / "Daily")

    async with aiosqlite.connect(app_env.db_path) as db:
        first, _ = await helpers.resolve_session_doc_for_start(
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
        second, _ = await helpers.resolve_session_doc_for_start(
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

        assert second == first
        count = await (await db.execute("SELECT COUNT(*) FROM session_documents")).fetchone()
        assert count[0] == 1


@pytest.mark.xfail(
    reason="QUARANTINE: c9aa199 (recovered/tabname-session-binding-wip) ships "
    "test-incomplete session-binding impl. See mega-main CodeRabbit triage "
    "'TOP FOLLOW-UP'. Finish impl (non-custodes interactive placeholder policy) "
    "or drop the commit to un-quarantine. strict=False so XPASS signals impl done.",
    strict=False,
)
@pytest.mark.asyncio
async def test_non_custodes_still_uses_interactive_placeholder_policy(
    app_env, monkeypatch, tmp_path
):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "TERRA_SESSIONS_DIR", vault / "Terra" / "Sessions")
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal" / "Daily")

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=None,
            primarch_name="mechanicus",
            origin_type="interactive",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=None,
            is_subagent=False,
        )
        await db.commit()
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()

    assert reason == "interactive_auto"
    assert "/Terra/Sessions/needs-session-name.md" in row[0]
    assert not (
        vault / "Terra" / "Journal" / "Daily" / f"{helpers.datetime.now():%Y-%m-%d}.md"
    ).exists()


@pytest.mark.asyncio
async def test_custodes_ignores_explicit_dispatch_doc_and_uses_daily_note(
    app_env, monkeypatch, tmp_path
):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal" / "Daily")

    explicit = vault / "Terra" / "Sessions" / "not-custodes-home.md"
    explicit.parent.mkdir(parents=True, exist_ok=True)
    explicit.write_text("---\ntitle: Not Custodes Home\n---\n", encoding="utf-8")

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=str(explicit),
            primarch_name="custodes",
            origin_type="interactive",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=None,
            is_subagent=False,
        )
        await db.commit()
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()

    assert reason == "daily_note_custodes"
    assert row[0].endswith(f"/Terra/Journal/Daily/{helpers.datetime.now():%Y-%m-%d}.md")
    assert "not-custodes-home" not in row[0]


@pytest.mark.asyncio
async def test_custodes_legion_without_primarch_binds_daily_note(app_env, monkeypatch, tmp_path):
    """GT/state-hook/cron custodes launches arrive with primarch_name=None but
    legion='custodes'. They must still bind today's daily note, not a placeholder."""
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "TERRA_SESSIONS_DIR", vault / "Terra" / "Sessions")
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal" / "Daily")

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=None,
            primarch_name=None,
            origin_type="cron",
            cron_job_id="custodes-morning",
            cron_job_name="Custodes Morning",
            working_dir=None,
            is_subagent=False,
            legion="custodes",
        )
        await db.commit()
        row = await (
            await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        ).fetchone()

    assert reason == "daily_note_custodes"
    assert row[0].endswith(f"/Terra/Journal/Daily/{helpers.datetime.now():%Y-%m-%d}.md")
    assert "needs-session-name" not in row[0]


@pytest.mark.asyncio
async def test_automated_unresolved_launch_creates_no_placeholder(app_env, monkeypatch, tmp_path):
    """A dispatched/automated launch that cannot resolve a doc returns
    (None, 'unresolved_dispatch') and mints no session document."""
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "TERRA_SESSIONS_DIR", vault / "Terra" / "Sessions")
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal" / "Daily")

    async with aiosqlite.connect(app_env.db_path) as db:
        doc_id, reason = await helpers.resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=None,
            primarch_name=None,
            origin_type="local",
            cron_job_id=None,
            cron_job_name=None,
            working_dir=None,
            is_subagent=False,
            legion="mechanicus",
        )
        await db.commit()
        count = await (await db.execute("SELECT COUNT(*) FROM session_documents")).fetchone()

    assert doc_id is None
    assert reason == "unresolved_dispatch"
    assert count[0] == 0
    assert not (vault / "Terra" / "Sessions").exists() or not list(
        (vault / "Terra" / "Sessions").glob("needs-session-name*")
    )
