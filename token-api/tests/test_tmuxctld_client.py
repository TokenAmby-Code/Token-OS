import asyncio
import json
import subprocess
import sys
import urllib.error


class _Resp:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_resolve_instance_pane_uses_tmuxctld_when_configured(app_env, monkeypatch):
    shared = sys.modules["shared"]
    monkeypatch.setenv("TMUXCTLD_URL", "http://127.0.0.1:7778")

    async def boom(*args, **kwargs):
        raise AssertionError("subprocess fallback should not run")

    def fake_urlopen(url, timeout):
        assert url == "http://127.0.0.1:7778/resolve-instance?instance_id=u"
        assert timeout <= 1
        return _Resp({"found": True, "pane_id": "%24", "pane_role": "palace:N"})

    monkeypatch.setattr(shared, "_run_subprocess_offloop", boom)
    monkeypatch.setattr(shared.urllib.request, "urlopen", fake_urlopen)

    assert asyncio.run(shared.resolve_instance_pane("u")) == ("%24", "palace:N")


def test_resolve_instance_pane_falls_back_when_tmuxctld_down(app_env, monkeypatch):
    shared = sys.modules["shared"]
    monkeypatch.setenv("TMUXCTLD_URL", "http://127.0.0.1:7778")

    def fake_urlopen(url, timeout):
        raise urllib.error.URLError("down")

    async def fake_offloop(args, **kwargs):
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout=b'{"found":true,"pane_id":"%25","pane_role":"mars:E"}',
            stderr=b"",
        )

    monkeypatch.setattr(shared.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(shared, "_run_subprocess_offloop", fake_offloop)

    assert asyncio.run(shared.resolve_instance_pane("u")) == ("%25", "mars:E")
