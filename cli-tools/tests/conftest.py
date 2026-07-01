"""Shared test isolation for the cli-tools suite.

The tmuxctl focus guard and send gate write observability artifacts (focus
logs in /tmp, events in agents.db) as a side effect of normal operation.
Without isolation, every test run pollutes the LIVE logs and DB with
fake-adapter events — which is exactly what poisoned the tmux de-lag
investigation. Every test gets redirected paths, unconditionally.
"""

from __future__ import annotations

import os
import pathlib

import pytest


@pytest.fixture(autouse=True)
def _isolate_live_observability(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPERIUM_TMUX_FOCUS_LOG", str(tmp_path / "tmux-focus-guard.log"))
    monkeypatch.setenv(
        "IMPERIUM_MECHANICUS_FOCUS_LOG", str(tmp_path / "mechanicus-focus-guard.log")
    )
    monkeypatch.setenv("TOKEN_API_DB", str(tmp_path / "agents.db"))
    # token-restart's plist reconcilers (ensure_plist_resource_limits /
    # ensure_plist_socket_activation) edit the LaunchAgent IN PLACE. Point them at
    # a throwaway path so a subprocess token-restart can never mutate the live
    # ~/Library/LaunchAgents plist while pytest runs (absent file → reconcile no-op).
    monkeypatch.setenv("TOKEN_RESTART_PLIST", str(tmp_path / "ai.openclaw.tokenapi.plist"))
    # Subprocess tests can execute dispatch paths that emit WrapperStart /
    # SessionStart / WrapperEnd hook telemetry. Those hooks must never post to
    # the developer's live Token-API while pytest is running; a failed local
    # connect is acceptable because the launch wrappers treat hook delivery as
    # best-effort.
    monkeypatch.setenv("TOKEN_API_URL", "http://127.0.0.1:9")
    # Subprocess dispatch tests must not post pane sends to the developer's live
    # tmuxctld. Provide a no-op tmuxctld-ping early on PATH; tests that need to
    # assert the payload can set TMUXCTLD_PING_LOG or shadow it with a narrower
    # fake in their own PATH prefix. Dedicated tmuxctld-ping tests execute the
    # real script by absolute path, so this transport stub does not mask them.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_ping = fake_bin / "tmuxctld-ping"
    fake_ping.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ -n "${TMUXCTLD_PING_LOG:-}" ]]; then printf \'%s\\n\' "$*" >> "$TMUXCTLD_PING_LOG"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_ping.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    # Most dispatch CLI tests use tiny fake tmux shims that only implement the
    # command under direct assertion. Keep the production launch observer opt-in
    # inside tests; tests that exercise the observer override this to a small
    # timeout explicitly.
    monkeypatch.setenv("DISPATCH_LAUNCH_OBSERVE_TIMEOUT", "0")
    # The duplicate-refusal guard enumerates the LIVE tmux server (list-panes -a +
    # ps) via `tmuxctl live-agents`. Disable it by default so the suite never reads
    # the operator's real fleet; the dedicated dup-refusal test re-enables it with a
    # fake tmuxctl that returns a canned match.
    monkeypatch.setenv("DISPATCH_WORKTREE_DUP_CHECK", "0")
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
