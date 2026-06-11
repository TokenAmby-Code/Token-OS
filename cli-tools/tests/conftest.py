"""Shared test isolation for the cli-tools suite.

The tmuxctl focus guard and send gate write observability artifacts (focus
logs in /tmp, events in agents.db) as a side effect of normal operation.
Without isolation, every test run pollutes the LIVE logs and DB with
fake-adapter events — which is exactly what poisoned the tmux de-lag
investigation. Every test gets redirected paths, unconditionally.
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def _isolate_live_observability(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_LOG", str(tmp_path / "tmux-focus-guard.log"))
    monkeypatch.setenv(
        "IMPERIUM_MECHANICUS_FOCUS_LOG", str(tmp_path / "mechanicus-focus-guard.log")
    )
    monkeypatch.setenv("TOKEN_API_DB", str(tmp_path / "agents.db"))
