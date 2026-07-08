"""Token-API SessionStart delegates the @INSTANCE_ID stamp to tmuxctld (single-writer).

Token-API resolves the canonical instance row id at SessionStart and hands it to
tmuxctld's semantic ``POST /instance/stamp`` endpoint — the sole writer of the pane
stamp. Token-API must NEVER author a raw ``set-option @INSTANCE_ID`` (through
``/tmux/run`` or otherwise), and must never post the ledger directly (the ledger bind
is tmuxctld-internal to the stamp).
"""

import asyncio
import sys


def _stamp_mutations(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if "set-option" in c and ("@INSTANCE_ID" in c or "@PANE_LABEL" in c)]


def _stamp_posts(posts: list[tuple[str, dict]]) -> list[dict]:
    return [body for path, body in posts if path == "/instance/stamp"]


def test_fresh_registration_delegates_stamp_and_never_raw_writes(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    tmux_calls: list[tuple[str, ...]] = []
    posts: list[tuple[str, dict]] = []

    async def fake_tmuxctld_run_tmux(args, **_kwargs):
        tmux_calls.append(tuple(args))
        return {"stdout": ""}

    def fake_sync_tmux(args, **_kwargs):
        tmux_calls.append(tuple(args))
        return {"stdout": ""}

    def fake_post(path, body, **_kwargs):
        posts.append((path, body))
        return {"ok": True, "result": {"found": True, "stamped": True}}

    monkeypatch.setattr(shared, "tmuxctld_run_tmux", fake_tmuxctld_run_tmux)
    monkeypatch.setattr(shared, "_tmuxctld_run_tmux", fake_sync_tmux)
    monkeypatch.setattr(shared, "_tmuxctld_post_json", fake_post)

    session_id = "delegates-stamp-fresh"

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": session_id,
                "cwd": "/tmp/x",
                "pid": 4242,
                "env": {
                    "TMUX_PANE": "%77",
                    "TOKEN_API_ENGINE": "claude",
                    "TOKEN_API_WRAPPER_ID": "wrap-no-ledger",
                },
            }
        )
        assert result["success"] is True

    asyncio.run(run())

    # Writer ownership: token-api NEVER authors a raw @INSTANCE_ID/@PANE_LABEL write,
    # nor posts the ledger directly.
    assert _stamp_mutations(tmux_calls) == []
    assert not [path for path, _ in posts if path == "/ledger" + "/upsert"]

    # Restore: token-api delegates the stamp to the tmuxctld semantic endpoint with the
    # canonical row id + effective pane, so runtime.pane_id lands non-null.
    stamps = _stamp_posts(posts)
    assert len(stamps) == 1, stamps
    body = stamps[0]
    assert body["instance_id"] == session_id
    assert body["pane"] == "%77"
    assert body["wrapper_id"] == "wrap-no-ledger"
    assert body["engine"] == "claude"

    import sqlite3

    conn = sqlite3.connect(app_env.db_path)
    try:
        row = conn.execute(
            "SELECT id, wrapper_launch_id, engine FROM instances WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (session_id, "wrap-no-ledger", "claude")


def test_reregistration_delegates_restamp_without_raw_writes(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    tmux_calls: list[tuple[str, ...]] = []
    posts: list[tuple[str, dict]] = []

    async def fake_tmuxctld_run_tmux(args, **_kwargs):
        tmux_calls.append(tuple(args))
        return {"stdout": ""}

    def fake_sync_tmux(args, **_kwargs):
        tmux_calls.append(tuple(args))
        return {"stdout": ""}

    def fake_post(path, body, **_kwargs):
        posts.append((path, body))
        return {"ok": True, "result": {"found": True, "stamped": True}}

    monkeypatch.setattr(shared, "tmuxctld_run_tmux", fake_tmuxctld_run_tmux)
    monkeypatch.setattr(shared, "_tmuxctld_run_tmux", fake_sync_tmux)
    monkeypatch.setattr(shared, "_tmuxctld_post_json", fake_post)

    session_id = "delegates-restamp"

    async def run():
        await hooks.handle_session_start(
            {"session_id": session_id, "cwd": "/tmp/x", "pid": 1, "env": {"TMUX_PANE": "%77"}}
        )
        tmux_calls.clear()
        posts.clear()
        await hooks.handle_session_start(
            {"session_id": session_id, "cwd": "/tmp/x", "pid": 2, "env": {"TMUX_PANE": "%88"}}
        )

    asyncio.run(run())

    # Re-registration re-stamps via delegation (never a raw token-api write), binding the
    # SAME canonical id onto the current pane.
    assert _stamp_mutations(tmux_calls) == []
    stamps = _stamp_posts(posts)
    assert len(stamps) == 1, stamps
    assert stamps[0]["instance_id"] == session_id
    assert stamps[0]["pane"] == "%88"


def test_resolve_instance_pane_fail_closed_on_not_found(app_env, monkeypatch):
    shared = sys.modules["shared"]

    def fake_tmuxctld_get_json(path, params, **_kwargs):
        assert path == "/tmux/resolve-instance"
        assert params == {"instance_id": "ghost"}
        return {"instance_id": "ghost", "pane_id": "", "pane_role": "", "found": False}

    monkeypatch.setattr(shared, "_tmuxctld_get_json", fake_tmuxctld_get_json)

    pane, role = asyncio.run(shared.resolve_instance_pane("ghost"))
    assert pane is None
    assert role is None


def test_resolve_instance_pane_returns_live_pane_when_found(app_env, monkeypatch):
    shared = sys.modules["shared"]

    def fake_tmuxctld_get_json(path, params, **_kwargs):
        assert path == "/tmux/resolve-instance"
        assert params == {"instance_id": "u"}
        return {"instance_id": "u", "pane_id": "%24", "pane_role": "palace:N", "found": True}

    monkeypatch.setattr(shared, "_tmuxctld_get_json", fake_tmuxctld_get_json)

    pane, role = asyncio.run(shared.resolve_instance_pane("u"))
    assert pane == "%24"
    assert role == "palace:N"


def test_resolve_instance_pane_empty_uuid_is_fail_closed(app_env):
    shared = sys.modules["shared"]
    assert asyncio.run(shared.resolve_instance_pane("")) == (None, None)
    assert asyncio.run(shared.resolve_instance_pane(None)) == (None, None)


def test_resolve_instance_pane_fails_closed_when_tmuxctld_absent(app_env, monkeypatch):
    shared = sys.modules["shared"]

    def daemon_absent(*_args, **_kwargs):
        return None

    monkeypatch.setattr(shared, "_tmuxctld_get_json", daemon_absent)
    assert asyncio.run(shared.resolve_instance_pane("u")) == (None, None)
