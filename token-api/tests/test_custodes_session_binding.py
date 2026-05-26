import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_custodes_creates_and_binds_today_daily_note(app_env, monkeypatch, tmp_path):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal")

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

        note_path = vault / "Terra" / "Journal" / f"{helpers.datetime.now():%Y-%m-%d}.md"
        assert reason == "daily_note_custodes"
        assert note_path.exists()
        row = await (await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))).fetchone()
        assert row[0] == str(note_path.resolve())
        assert "needs-session-name" not in row[0]


@pytest.mark.asyncio
async def test_custodes_daily_note_binding_is_singleton(app_env, monkeypatch, tmp_path):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal")

    async with aiosqlite.connect(app_env.db_path) as db:
        first, _ = await helpers.resolve_session_doc_for_start(
            db, dispatch_session_doc_path=None, primarch_name="custodes",
            origin_type="interactive", cron_job_id=None, cron_job_name=None,
            working_dir=None, is_subagent=False,
        )
        await db.commit()
        second, _ = await helpers.resolve_session_doc_for_start(
            db, dispatch_session_doc_path=None, primarch_name="custodes",
            origin_type="interactive", cron_job_id=None, cron_job_name=None,
            working_dir=None, is_subagent=False,
        )
        await db.commit()

        assert second == first
        count = await (await db.execute("SELECT COUNT(*) FROM session_documents")).fetchone()
        assert count[0] == 1


@pytest.mark.asyncio
async def test_non_custodes_still_uses_interactive_placeholder_policy(app_env, monkeypatch, tmp_path):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "TERRA_SESSIONS_DIR", vault / "Terra" / "Sessions")
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal")

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
        row = await (await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))).fetchone()

    assert reason == "interactive_auto"
    assert "/Terra/Sessions/needs-session-name.md" in row[0]
    assert not (vault / "Terra" / "Journal" / f"{helpers.datetime.now():%Y-%m-%d}.md").exists()


@pytest.mark.asyncio
async def test_custodes_ignores_explicit_dispatch_doc_and_uses_daily_note(app_env, monkeypatch, tmp_path):
    helpers = __import__("session_doc_helpers")
    vault = tmp_path / "Imperium-ENV"
    monkeypatch.setattr(helpers, "_VAULT_ROOT", vault)
    monkeypatch.setattr(helpers, "DAILY_NOTES_DIR", vault / "Terra" / "Journal")

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
        row = await (await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))).fetchone()

    assert reason == "daily_note_custodes"
    assert row[0].endswith(f"/Terra/Journal/{helpers.datetime.now():%Y-%m-%d}.md")
    assert "not-custodes-home" not in row[0]
