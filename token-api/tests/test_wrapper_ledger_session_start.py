from __future__ import annotations

import asyncio
import sys


def test_session_start_does_not_post_wrapper_ledger(app_env, monkeypatch):
    """Token-API must not hydrate tmuxctld ledger from SessionStart."""
    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    posted: list[tuple[str, dict]] = []
    tmux_writes: list[tuple[str, ...]] = []

    def fake_post(path: str, body: dict, **_kwargs):
        posted.append((path, body))
        return {"success": True}

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

    assert [path for path, _body in posted if path == "/ledger" + "/upsert"] == []
    assert [c for c in tmux_writes if "set-option" in c and "@INSTANCE_ID" in c] == []
