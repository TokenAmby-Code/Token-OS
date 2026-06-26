import pytest


@pytest.mark.asyncio
async def test_enqueue_pane_write_message_id_replay_does_not_double_queue(app_env):
    main = app_env.main

    first = await main.enqueue_pane_write(
        instance_id="%custodes",
        tmux_pane="%custodes",
        source="brief",
        purpose="brief_send",
        payload="cold-start lock retry payload",
        message_id="msg-cold-start-lock-1:%custodes",
    )
    second = await main.enqueue_pane_write(
        instance_id="%custodes",
        tmux_pane="%custodes",
        source="brief",
        purpose="brief_send",
        payload="cold-start lock retry payload",
        message_id="msg-cold-start-lock-1:%custodes",
    )

    assert second["id"] == first["id"]
    assert second["idempotent_replay"] is True

    async with main.aiosqlite.connect(main.DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM pane_write_queue
            WHERE source='brief' AND purpose='brief_send'
              AND message_id='msg-cold-start-lock-1:%custodes'
            """
        )
        row = await cur.fetchone()
    assert row[0] == 1
