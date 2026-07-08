from __future__ import annotations

import asyncio
import sys


def test_session_start_delegates_stamp_and_never_raw_writes_ledger(app_env, monkeypatch):
    """SessionStart binds identity through tmuxctld's semantic stamp, not raw writes.

    Token-API hands the canonical instance id to ``POST /instance/stamp`` (tmuxctld
    owns BOTH the ``@INSTANCE_ID`` write and the wrapper-ledger ``instance_id`` bind).
    Token-API must never author a raw ``set-option @INSTANCE_ID`` nor post
    ``/ledger/upsert`` directly — the single-writer boundary.
    """
    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    posted: list[tuple[str, dict]] = []
    tmux_writes: list[tuple[str, ...]] = []

    def fake_post(path: str, body: dict, **_kwargs):
        posted.append((path, body))
        return {"ok": True, "result": {"found": True, "stamped": True}}

    async def fake_run_tmux(args, **_kwargs):
        tmux_writes.append(tuple(args))
        return {"stdout": ""}

    monkeypatch.setattr(shared, "_tmuxctld_post_json", fake_post)
    monkeypatch.setattr(shared, "tmuxctld_run_tmux", fake_run_tmux)

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "ledger-boundary-session",
                "cwd": "/tmp/work",
                "pid": 4242,
                "wrapper_launch_id": "wrap-1",
                "env": {"TMUX_PANE": "%42", "TOKEN_API_ENGINE": "codex"},
            }
        )
        assert result["success"] is True

    asyncio.run(run())

    # Single-writer boundary: no direct ledger post, no raw @INSTANCE_ID write.
    assert [path for path, _body in posted if path == "/ledger" + "/upsert"] == []
    assert [c for c in tmux_writes if "set-option" in c and "@INSTANCE_ID" in c] == []

    # Restore: the stamp is delegated to tmuxctld with the canonical id, effective pane,
    # and wrapper id — the ledger bind rides along inside the daemon.
    stamps = [body for path, body in posted if path == "/instance/stamp"]
    assert len(stamps) == 1, stamps
    assert stamps[0]["instance_id"] == "ledger-boundary-session"
    assert stamps[0]["pane"] == "%42"
    assert stamps[0]["wrapper_id"] == "wrap-1"
