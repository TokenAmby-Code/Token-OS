"""Tests for cause-tagged SessionStart registration-failure instrumentation.

When the critical SessionStart registration write fails, the dispatcher tags the
cause (EMFILE fd-burst vs db-locked vs other), counts it, and surfaces it in the
503 so the client retry can re-attempt. This lets us tell the fd-burst path apart
from the restart window (conn-refused, which never reaches the server and is
tagged client-side in agent-wrapper-common.sh).
"""

from __future__ import annotations

import errno
import types

import pytest
from fastapi import HTTPException

import routes.hooks as hooks


def _req():
    return types.SimpleNamespace(client=None)


def test_classify_direct_emfile() -> None:
    exc = OSError(errno.EMFILE, "Too many open files")
    assert hooks._classify_session_start_failure(exc) == "emfile"


def test_classify_wrapped_emfile() -> None:
    try:
        try:
            raise OSError(errno.EMFILE, "Too many open files")
        except OSError as inner:
            raise RuntimeError("registration INSERT failed") from inner
    except RuntimeError as exc:
        assert hooks._classify_session_start_failure(exc) == "emfile"


def test_classify_db_locked_and_other() -> None:
    assert hooks._classify_session_start_failure(Exception("database is locked")) == "db-locked"
    assert hooks._classify_session_start_failure(ValueError("nope")) == "other"


def test_dispatch_session_start_failure_503s_and_tallies(monkeypatch) -> None:
    async def boom(payload):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(hooks, "handle_session_start", boom)

    # Don't depend on a DB for the event log.
    async def _noop_log(*a, **k):
        return None

    monkeypatch.setattr(hooks, "log_event", _noop_log)

    before = dict(hooks._SESSION_START_FAILURE_CAUSES)
    with pytest.raises(HTTPException) as ei:
        import asyncio

        asyncio.run(hooks.dispatch_hook("SessionStart", {"session_id": "x"}, _req()))

    assert ei.value.status_code == 503
    assert "emfile" in str(ei.value.detail)
    assert hooks._SESSION_START_FAILURE_CAUSES.get("emfile", 0) == before.get("emfile", 0) + 1


def test_dispatch_non_sessionstart_failure_does_not_raise_or_tally(monkeypatch) -> None:
    async def boom(payload):
        raise RuntimeError("kaboom")

    async def _noop_log(*a, **k):
        return None

    monkeypatch.setattr(hooks, "handle_stop", boom)
    monkeypatch.setattr(hooks, "log_event", _noop_log)

    before = dict(hooks._SESSION_START_FAILURE_CAUSES)
    import asyncio

    result = asyncio.run(hooks.dispatch_hook("Stop", {"session_id": "x"}, _req()))
    assert result["success"] is False
    # The SessionStart tally must not move for a non-SessionStart failure.
    assert dict(hooks._SESSION_START_FAILURE_CAUSES) == before
