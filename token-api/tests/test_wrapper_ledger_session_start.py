from __future__ import annotations

import asyncio

import aiosqlite


def test_stamp_posts_canonical_instance_id_for_wrapper(app_env, monkeypatch):
    hooks = __import__("routes.hooks", fromlist=["_stamp_instance_id"])
    posted: list[tuple[str, dict]] = []
    stamped: list[tuple[str, ...]] = []

    def fake_post(path: str, body: dict, **_kwargs):
        posted.append((path, body))
        return {"success": True}

    async def fake_run_tmux(args, **_kwargs):
        stamped.append(tuple(args))
        return ""

    monkeypatch.setattr(hooks.shared, "_tmuxctld_post_json", fake_post)
    monkeypatch.setattr(hooks.shared, "tmuxctld_run_tmux", fake_run_tmux)
    monkeypatch.setattr(hooks, "_tmux_pane_label", lambda _pane: asyncio.sleep(0, "somnium:W"))

    async def run_case():
        async with aiosqlite.connect(app_env.db_path) as db:
            await db.execute(
                """INSERT INTO instances (id, device_id, rank, wrapper_launch_id, last_activity)
                   VALUES (?, ?, ?, ?, ?)""",
                ("old-engine-session", "Mac-Mini", "retired", "wrap-1", "2026-07-01T00:00:00"),
            )
            await db.execute(
                """INSERT INTO instances (id, device_id, rank, wrapper_launch_id, last_activity)
                   VALUES (?, ?, ?, ?, ?)""",
                ("canonical-instance", "Mac-Mini", "astartes", "wrap-1", "2026-07-01T00:01:00"),
            )
            await db.commit()

            await hooks._stamp_instance_id(
                "%42",
                "live-token-api-session-id",
                db=db,
                wrapper_id="wrap-1",
                engine="codex",
                working_dir="/tmp/work",
                persona="salamanders",
            )

    asyncio.run(run_case())

    assert posted == [
        (
            "/ledger/upsert",
            {
                "wrapper_id": "wrap-1",
                "instance_id": "canonical-instance",
                "pane_positional_id": "somnium:W",
                "engine": "codex",
                "working_dir": "/tmp/work",
                "persona": "salamanders",
                "state": "OPEN",
            },
        )
    ]
    assert ("set-option", "-p", "-t", "%42", "@INSTANCE_ID", "canonical-instance") in stamped
