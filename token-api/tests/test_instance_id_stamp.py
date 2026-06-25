"""Phase 0/2 tests: @INSTANCE_ID stamping on registration + fail-closed resolution."""

import asyncio
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable


def _recorder() -> tuple[
    list[tuple[str, ...]],
    Callable[..., Awaitable[subprocess.CompletedProcess]],
]:
    """Return (calls, fake_offloop) where fake_offloop records argv and succeeds."""
    calls: list[tuple[str, ...]] = []

    async def fake_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=b"", stderr=b"")

    return calls, fake_offloop


def _stamp_calls(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        c
        for c in calls
        if len(c) >= 6 and c[0] == "tmux" and c[1] == "set-option" and c[5] == "@INSTANCE_ID"
    ]


def _pane_label_calls(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        c
        for c in calls
        if len(c) >= 6 and c[0] == "tmux" and c[1] == "set-option" and c[5] == "@PANE_LABEL"
    ]


def test_fresh_registration_stamps_instance_id(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    calls, fake_offloop = _recorder()
    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    session_id = str(uuid.uuid4())

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": session_id,
                "cwd": "/tmp/x",
                "pid": 4242,
                "env": {"TMUX_PANE": "%77", "TOKEN_API_ENGINE": "claude"},
            }
        )
        assert result["success"] is True

    asyncio.run(run())

    stamps = _stamp_calls(calls)
    assert stamps, f"no @INSTANCE_ID stamp recorded; calls={calls}"
    # The stamp targets the agent's pane and carries the session UUID.
    pane = stamps[-1][4]
    value = stamps[-1][6]
    assert pane == "%77"
    assert value == session_id

    labels = _pane_label_calls(calls)
    assert labels, f"no @PANE_LABEL stamp recorded; calls={calls}"
    assert labels[-1][4] == "%77"
    assert labels[-1][6] == "needs-name"


def test_reregistration_restamps_instance_id(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    calls, fake_offloop = _recorder()
    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    session_id = str(uuid.uuid4())

    async def run():
        # First registration creates the row...
        await hooks.handle_session_start(
            {"session_id": session_id, "cwd": "/tmp/x", "pid": 1, "env": {"TMUX_PANE": "%77"}}
        )
        calls.clear()
        # ...second SessionStart for the same UUID re-stamps (re-register branch).
        await hooks.handle_session_start(
            {"session_id": session_id, "cwd": "/tmp/x", "pid": 2, "env": {"TMUX_PANE": "%88"}}
        )

    asyncio.run(run())

    stamps = _stamp_calls(calls)
    assert stamps, "re-registration did not re-stamp @INSTANCE_ID"
    # The re-stamp must land on the NEW pane (%88), not the original (%77).
    assert stamps[-1][4] == "%88"
    assert stamps[-1][6] == session_id


def test_resolve_instance_pane_fail_closed_on_not_found(app_env, monkeypatch):
    shared = sys.modules["shared"]

    async def fake_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        # Mirror tmuxctl resolve-instance --format json on a miss (exit 1 + found:false).
        payload = b'{"instance_id": "ghost", "pane_id": "", "pane_role": "", "found": false}'
        return subprocess.CompletedProcess(
            args=list(args), returncode=1, stdout=payload, stderr=b""
        )

    monkeypatch.setattr(shared, "_run_subprocess_offloop", fake_offloop)

    pane, role = asyncio.run(shared.resolve_instance_pane("ghost"))
    assert pane is None
    assert role is None


def test_resolve_instance_pane_returns_live_pane_when_found(app_env, monkeypatch):
    shared = sys.modules["shared"]

    async def fake_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        payload = b'{"instance_id": "u", "pane_id": "%24", "pane_role": "palace:N", "found": true}'
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout=payload, stderr=b""
        )

    monkeypatch.setattr(shared, "_run_subprocess_offloop", fake_offloop)

    pane, role = asyncio.run(shared.resolve_instance_pane("u"))
    assert pane == "%24"
    assert role == "palace:N"


def test_resolve_instance_pane_empty_uuid_is_fail_closed(app_env):
    shared = sys.modules["shared"]
    assert asyncio.run(shared.resolve_instance_pane("")) == (None, None)
    assert asyncio.run(shared.resolve_instance_pane(None)) == (None, None)


def test_resolve_instance_pane_swallows_subprocess_error(app_env, monkeypatch):
    shared = sys.modules["shared"]

    async def boom(args, *, timeout=None, stdout=None, stderr=None, env=None):
        raise subprocess.TimeoutExpired(cmd="tmuxctl", timeout=3)

    monkeypatch.setattr(shared, "_run_subprocess_offloop", boom)
    assert asyncio.run(shared.resolve_instance_pane("u")) == (None, None)


def _unset_calls(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        c
        for c in calls
        if c[0] == "tmux" and c[1] == "set-option" and "-u" in c and c[-1] == "@INSTANCE_ID"
    ]


def test_unstamp_clears_old_pane_when_stamp_still_matches(app_env, monkeypatch):
    """An instance moving off a pane clears its own stamp on that pane."""
    hooks = sys.modules["routes.hooks"]
    calls: list[tuple[str, ...]] = []
    session_id = str(uuid.uuid4())

    async def fake_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        calls.append(tuple(args))
        # show-options reports the old pane still carries THIS instance's id.
        out = session_id.encode() if "show-options" in args else b""
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=out, stderr=b"")

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)
    asyncio.run(hooks._unstamp_instance_id("%77", session_id))

    unsets = _unset_calls(calls)
    assert unsets, "old pane stamp was not cleared"
    assert unsets[-1][5] == "%77"


def test_unstamp_never_clobbers_pane_reused_by_another_instance(app_env, monkeypatch):
    """If the old pane was re-stamped by a different instance, leave it alone."""
    hooks = sys.modules["routes.hooks"]
    calls: list[tuple[str, ...]] = []

    async def fake_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        calls.append(tuple(args))
        # show-options reports a DIFFERENT instance now owns the pane.
        out = b"some-other-instance-id" if "show-options" in args else b""
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=out, stderr=b"")

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)
    asyncio.run(hooks._unstamp_instance_id("%77", str(uuid.uuid4())))

    assert not _unset_calls(calls), "must not clear a pane owned by another instance"
