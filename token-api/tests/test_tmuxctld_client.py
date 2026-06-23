"""token-api tmuxctld loopback-client tests.

These are LIVE-endpoint tests (no urlopen mocks): each starts the real stdlib
tmuxctld ThreadingHTTPServer in-process against a stub adapter, gates on the
daemon's ready event, points ``TMUXCTLD_URL`` at the live port, and exercises
``shared.resolve_instance_pane`` / ``shared.instance_id_for_pane`` end to end.

Coverage maps to the carried-forward CodeRabbit findings:
* the daemon-preferred fast path returns canonical-only results;
* the subprocess path is the fail-closed fallback when the daemon is absent;
* the loopback client bypasses any system/env HTTP proxy (empty ProxyHandler).
"""

import asyncio
import json
import pathlib
import subprocess
import sys
import threading
import urllib.request

# The daemon lives in the sibling cli-tools package; add its lib to the path so
# the in-process server can be imported here (stdlib-only — no venv needed).
CLI_LIB = pathlib.Path(__file__).resolve().parents[2] / "cli-tools" / "lib"
if str(CLI_LIB) not in sys.path:
    sys.path.insert(0, str(CLI_LIB))


class FoundInstanceAdapter:
    """tmux reachable; one live pane carries @INSTANCE_ID=live-uuid @PANE_ID=palace:1."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("list-panes", "-a"):
            return "%24\tlive-uuid\tpalace:1"
        return ""


class StampedPaneAdapter:
    """Pane palace:1 carries @INSTANCE_ID=stamped-uuid (reverse lookup)."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "show-options" and args[-1] == "@INSTANCE_ID":
            return "stamped-uuid"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


def _serve_daemon(adapter_factory: type):
    from tmuxctl import daemon

    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Gate on the real ready event the server thread sets once it is listening —
    # no sleep, no magic-number timeout (the 5s is only a deadlock backstop).
    assert server.ready.wait(timeout=5), "tmuxctld never signalled ready"
    return server


def _url_for(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _warm(server, path: str) -> None:
    """Drive the cold first request to completion before the timed assertion.

    The real daemon is a long-running, warm process; in-process the FIRST request
    pays a one-time per-process cost (lazy import + bytecode compile in the handler
    thread — seconds on a cold CI runner with no cached .pyc) that exceeds the
    client's tight 0.5s loopback timeout. We pay that cost here with a generous
    timeout and ASSERT success, so a genuine failure surfaces loudly instead of
    silently degrading the timed assertion to the subprocess fallback.
    """
    url = f"{_url_for(server)}{path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        status = resp.status
        payload = json.loads(resp.read().decode("utf-8"))
    assert status == 200 and payload.get("ok") is True, f"warm-up failed: {payload}"


def test_resolve_instance_pane_prefers_tmuxctld_live(app_env, monkeypatch) -> None:
    shared = sys.modules["shared"]
    server = _serve_daemon(FoundInstanceAdapter)
    try:
        # The daemon's resolve-instance resolves the agent via a registry fetch to
        # TOKEN_API_URL (default localhost:7777). Point it at a dead port so that
        # fetch fails INSTANTLY (ECONNREFUSED) rather than stalling on its 5s
        # timeout — otherwise the daemon reply outruns the client's 0.5s budget.
        monkeypatch.setenv("TOKEN_API_URL", "http://127.0.0.1:1")
        _warm(server, "/tmux/resolve-instance?instance_id=live-uuid")
        monkeypatch.setenv("TMUXCTLD_URL", _url_for(server))

        async def boom(*args, **kwargs) -> None:
            raise AssertionError("subprocess fallback must not run when the daemon answers")

        monkeypatch.setattr(shared, "_run_subprocess_offloop", boom)
        # Daemon returns the canonical-only role (never the raw physical %24).
        assert asyncio.run(shared.resolve_instance_pane("live-uuid")) == ("palace:1", "palace:1")
    finally:
        server.shutdown()


def test_resolve_instance_pane_falls_back_when_daemon_down(app_env, monkeypatch) -> None:
    shared = sys.modules["shared"]
    # Nothing is listening on port 1 -> client returns None -> subprocess fallback.
    monkeypatch.setenv("TMUXCTLD_URL", "http://127.0.0.1:1")

    async def fake_offloop(args: list, **kwargs):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout=b'{"found":true,"pane_id":"%25","pane_role":"mars:E"}',
            stderr=b"",
        )

    monkeypatch.setattr(shared, "_run_subprocess_offloop", fake_offloop)
    assert asyncio.run(shared.resolve_instance_pane("u")) == ("%25", "mars:E")


def test_instance_id_for_pane_prefers_tmuxctld_live(app_env, monkeypatch) -> None:
    shared = sys.modules["shared"]
    server = _serve_daemon(StampedPaneAdapter)
    try:
        monkeypatch.setenv("TMUXCTLD_URL", _url_for(server))

        async def boom(*args, **kwargs) -> None:
            raise AssertionError("subprocess fallback must not run when the daemon answers")

        monkeypatch.setattr(shared, "_run_subprocess_offloop", boom)
        assert asyncio.run(shared.instance_id_for_pane("palace:1")) == "stamped-uuid"
    finally:
        server.shutdown()


def test_tmuxctld_url_rejects_non_loopback(app_env, monkeypatch) -> None:
    shared = sys.modules["shared"]
    # Off-box host -> rejected (no SSRF / exfiltration via a stray TMUXCTLD_URL).
    monkeypatch.setenv("TMUXCTLD_URL", "http://10.0.0.5:7778")
    assert shared._tmuxctld_url() is None
    # Non-http scheme -> rejected.
    monkeypatch.setenv("TMUXCTLD_URL", "https://127.0.0.1:7778")
    assert shared._tmuxctld_url() is None
    # Loopback http -> honoured.
    monkeypatch.setenv("TMUXCTLD_URL", "http://127.0.0.1:7778")
    assert shared._tmuxctld_url() == "http://127.0.0.1:7778"


def test_loopback_client_bypasses_env_proxy(app_env, monkeypatch) -> None:
    shared = sys.modules["shared"]
    server = _serve_daemon(FoundInstanceAdapter)
    try:
        # Fast-fail the daemon's registry fetch (see sibling test) so the reply
        # beats the client's 0.5s budget; warm BEFORE installing the bogus proxy
        # (warm-up uses a bare opener that would otherwise honour it).
        monkeypatch.setenv("TOKEN_API_URL", "http://127.0.0.1:1")
        _warm(server, "/tmux/resolve-instance?instance_id=live-uuid")
        # A bogus proxy on a dead port would break the call IF the loopback request
        # were routed through it. The empty ProxyHandler opener must bypass it.
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")
        monkeypatch.setenv("TMUXCTLD_URL", _url_for(server))

        async def boom(*args, **kwargs) -> None:
            raise AssertionError("subprocess fallback must not run when the daemon answers")

        monkeypatch.setattr(shared, "_run_subprocess_offloop", boom)
        assert asyncio.run(shared.resolve_instance_pane("live-uuid")) == ("palace:1", "palace:1")
    finally:
        server.shutdown()
