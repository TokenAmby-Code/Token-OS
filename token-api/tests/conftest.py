import importlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

_MODULES_TO_RELOAD = [
    "personas",
    "shared",
    "db_schema",
    "phone_service",
    "enforce",
    "enforcement_service",
    "routes.voice",
    "routes.tts",
    "routes.day_start",
    "routes.hooks",
    "stop_hook",
    "init_db",
    "temp_message",
    "timer_telemetry",
    "main",
]


@pytest.fixture(autouse=True)
def isolate_vault(tmp_path, monkeypatch):
    """Point the Obsidian vault at a per-test temp dir for EVERY test.

    Without this, vault-root resolution falls back to the live vault at
    /Volumes/Imperium/Imperium-ENV whenever IMPERIUM_ENV is unset and the NAS is
    mounted — which is how thousands of placeholder `needs-session-name-*.md` and
    `test-job-*.md` docs leaked into the live vault from test runs.  Vault-root
    resolution is now lazy (shared._vault_root / session_doc_helpers.vault_root),
    so setting the env here redirects all session-doc writes into the temp dir.
    The chokepoint guard in session_doc_helpers is the backstop if anything slips.
    """
    vault = tmp_path / "Imperium-ENV"
    # Set only IMPERIUM_ENV: vault_root() / shared._vault_root() check it first, so
    # this fully isolates the vault. Do NOT override IMPERIUM here — it also drives
    # runtime-path resolution (cli-tools imperium config) unrelated to the vault.
    monkeypatch.setenv("IMPERIUM_ENV", str(vault))
    # Isolate the civic (Pax-ENV) vault too: civic_vault_root() checks CIVIC_ENV
    # first, so civic session-doc writes land in the temp dir, never /Volumes/Civic.
    monkeypatch.setenv("CIVIC_ENV", str(tmp_path / "Pax-ENV"))
    return vault


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "agents.db"
    monkeypatch.setenv("TOKEN_API_DB", str(db_path))
    monkeypatch.setenv("IMPERIUM_ENV", str(tmp_path / "Imperium-ENV"))
    monkeypatch.setenv("CIVIC_ENV", str(tmp_path / "Pax-ENV"))
    # The live Mac may have a launchd tmuxctld on 127.0.0.1:7778. Unit tests must
    # not consult that real daemon through shared's production default-loopback
    # resolver; individual tmuxctld client tests opt back in with their own URL.
    monkeypatch.setenv("TMUXCTLD_URL", "disabled")
    # Isolate morning-session state from the real /tmp so the keepalive gate and
    # morning/end endpoint operate on a per-test directory.
    monkeypatch.setenv("CUSTODES_MORNING_DIR", str(tmp_path / "custodes_morning"))
    for name in _MODULES_TO_RELOAD:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    shared = sys.modules["shared"]
    init_db = sys.modules["init_db"]
    main = sys.modules["main"]

    init_db.init_database()

    async def _no_pane_rows():
        return []

    async def _no_observed_agents():
        return []

    # The tmuxctl pane oracle (shared.resolve_instance_pane / instance_id_for_pane)
    # and the tmux-target resolvers all funnel through shared._run_subprocess_offloop,
    # which spawns a real subprocess against the live tmux server. Under test there
    # is no live server, so each call would pay the full 3s spawn before failing
    # closed — which dragged SessionStart-heavy files past their timeout. Stub the
    # chokepoint (not the high-level functions) so the real oracle logic still runs
    # but resolves to "no live pane" instantly. Tests that need a specific resolution
    # override resolve_instance_pane / _run_subprocess_offloop themselves after
    # app_env builds; the oracle's own unit tests (test_instance_id_stamp.py) rely on
    # this exact seam.
    async def _no_tmux_offloop(args, *, timeout=None, stdout=None, stderr=None, env=None):
        arglist = list(args)
        # tmuxctl resolve-instance --format json: payload printed on both exit codes,
        # caller trusts the `found` flag → emit a miss.
        if "resolve-instance" in arglist:
            return subprocess.CompletedProcess(
                args=arglist, returncode=1, stdout=b'{"found": false}', stderr=b""
            )
        # tmux show-options / display-message / tmuxctl resolve-pane: empty stdout +
        # nonzero rc → callers fail closed to None.
        return subprocess.CompletedProcess(args=arglist, returncode=1, stdout=b"", stderr=b"")

    # Golden Throne fixtures insert Mac-Mini-local instances; pin the
    # reloaded module so Linux CI does not route them through satellite dispatch.
    monkeypatch.setattr(main, "LOCAL_DEVICE_NAME", "Mac-Mini")
    monkeypatch.setattr(main, "_tmux_pane_rows", _no_pane_rows)
    monkeypatch.setattr(main, "_detect_tmux_agent_panes", _no_observed_agents)
    monkeypatch.setattr(shared, "_run_subprocess_offloop", _no_tmux_offloop)

    return SimpleNamespace(db_path=db_path, shared=shared, init_db=init_db, main=main)
