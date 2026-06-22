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
    # Subprocess tests can execute dispatch paths that emit WrapperStart /
    # SessionStart / WrapperEnd hook telemetry. Those hooks must never post to
    # the developer's live Token-API while pytest is running; a failed local
    # connect is acceptable because the launch wrappers treat hook delivery as
    # best-effort.
    monkeypatch.setenv("TOKEN_API_URL", "http://127.0.0.1:9")
    # Most dispatch CLI tests use tiny fake tmux shims that only implement the
    # command under direct assertion. Keep the production launch observer opt-in
    # inside tests; tests that exercise the observer override this to a small
    # timeout explicitly.
    monkeypatch.setenv("DISPATCH_LAUNCH_OBSERVE_TIMEOUT", "0")
    # Isolate the Obsidian vault too: cli-tools tests that import token-api
    # session-doc helpers must not write placeholder docs into the live vault at
    # /Volumes/Imperium/Imperium-ENV. Vault-root resolution is lazy and checks
    # IMPERIUM_ENV first, so this alone redirects all writes into the temp dir.
    # Do NOT override IMPERIUM here — it also drives runtime-path resolution
    # (imperium config) which is unrelated to the vault.
    monkeypatch.setenv("IMPERIUM_ENV", str(tmp_path / "Imperium-ENV"))
    # A live dev-shell exports TOKEN_API_AGENT_WRAPPER_BYPASS=1 so interactive
    # agents skip the launch wrappers. Inherited into pytest, it flips dispatch
    # codepaths that the suite asserts against and produces local-only flakes
    # (absent on CI, where it is never set). Scrub it so the suite is hermetic
    # regardless of the shell it runs from.
    monkeypatch.delenv("TOKEN_API_AGENT_WRAPPER_BYPASS", raising=False)
