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
from types import SimpleNamespace

# The daemon package lives in root tmuxctld; add its lib to the path so
# the in-process server can be imported here (stdlib-only — no venv needed).
CLI_LIB = pathlib.Path(__file__).resolve().parents[2] / "tmuxctld" / "lib"
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


# ---------------------------------------------------------------------------
# instance_rename: tmuxctld owns BOTH the @PANE_LABEL border nametag AND the
# native pane title. Resolution precedence is pane -> instance_id; an unresolved
# target FAILS CLOSED with zero tmux mutation (mirrors instance_set_option).
# ---------------------------------------------------------------------------


class RecordingAdapter:
    """Records every ``run`` argv; ``resolve_pane`` is stubbed at module scope."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        return ""


def test_instance_rename_by_pane_issues_both_writes(app_env, monkeypatch) -> None:
    from tmuxctl import service as svc

    adapter = RecordingAdapter()
    monkeypatch.setattr(
        svc, "resolve_pane", lambda a, target: SimpleNamespace(pane_id="%42", pane_role="mars:E")
    )
    control = svc.TmuxControlPlane(adapter)

    result = control.instance_rename("auth-refactor", pane="mars:E")

    assert result == {
        "found": True,
        "target": "%42",
        "pane_role": "mars:E",
        "name": "auth-refactor",
    }
    # BOTH the border nametag (@PANE_LABEL) and the native pane title (-T) are set,
    # and nothing else — select-pane -T is the title-only, camera-neutral form.
    assert adapter.calls == [
        ("set-option", "-p", "-t", "%42", "@PANE_LABEL", "auth-refactor"),
        ("select-pane", "-t", "%42", "-T", "auth-refactor"),
    ]


def test_instance_rename_by_instance_id_issues_both_writes(app_env, monkeypatch) -> None:
    from tmuxctl import service as svc

    adapter = RecordingAdapter()
    control = svc.TmuxControlPlane(adapter)
    monkeypatch.setattr(
        control,
        "resolve_instance",
        lambda iid: {"found": True, "pane_id": "council:1", "pane_role": "council:1"},
    )

    result = control.instance_rename("mars-worker", instance_id="uuid-1")

    assert result["found"] is True
    assert result["target"] == "council:1"
    assert result["pane_role"] == "council:1"
    assert adapter.calls == [
        ("set-option", "-p", "-t", "council:1", "@PANE_LABEL", "mars-worker"),
        ("select-pane", "-t", "council:1", "-T", "mars-worker"),
    ]


def test_instance_rename_fail_closed_on_unresolved_pane(app_env, monkeypatch) -> None:
    from tmuxctl import service as svc

    adapter = RecordingAdapter()

    def _boom(a, target):
        raise ValueError("pane target not found")

    monkeypatch.setattr(svc, "resolve_pane", _boom)
    control = svc.TmuxControlPlane(adapter)

    result = control.instance_rename("x", pane="ghost:9")

    assert result["found"] is False
    assert adapter.calls == [], "an unresolved pane must yield ZERO tmux mutation"


def test_instance_rename_fail_closed_on_unresolved_instance(app_env, monkeypatch) -> None:
    from tmuxctl import service as svc

    adapter = RecordingAdapter()
    control = svc.TmuxControlPlane(adapter)
    monkeypatch.setattr(
        control, "resolve_instance", lambda iid: {"found": False, "pane_id": "", "pane_role": ""}
    )

    result = control.instance_rename("x", instance_id="dead-uuid")

    assert result["found"] is False
    assert adapter.calls == [], "an unresolved instance must yield ZERO tmux mutation"


def test_shared_tmuxctld_rename_pane_e2e(app_env, monkeypatch) -> None:
    """End-to-end: ``shared.tmuxctld_rename_pane`` -> live daemon -> both tmux writes."""
    shared = sys.modules["shared"]
    from tmuxctl import service as svc

    recorded: list[tuple[str, ...]] = []

    class E2EAdapter:
        def list_sessions(self) -> list:
            return []

        def run(self, *args: str, allow_failure: bool = False) -> str:
            recorded.append(tuple(args))
            return ""

    monkeypatch.setattr(
        svc, "resolve_pane", lambda a, target: SimpleNamespace(pane_id="%7", pane_role="palace:1")
    )
    server = _serve_daemon(lambda: E2EAdapter())
    try:
        monkeypatch.setenv("TMUXCTLD_URL", _url_for(server))
        envelope = asyncio.run(shared.tmuxctld_rename_pane(pane="palace:1", name="deploy-fix"))

        assert envelope is not None and envelope.get("ok") is True
        assert envelope["result"] == {
            "found": True,
            "target": "%7",
            "pane_role": "palace:1",
            "name": "deploy-fix",
        }
        assert ("set-option", "-p", "-t", "%7", "@PANE_LABEL", "deploy-fix") in recorded
        assert ("select-pane", "-t", "%7", "-T", "deploy-fix") in recorded
    finally:
        server.shutdown()


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
