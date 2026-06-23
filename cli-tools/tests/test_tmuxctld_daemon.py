import json
import urllib.error
import urllib.request

from tmuxctl.daemon import create_app


class FakeControl:
    def resolve_instance(self, instance_id):
        return {
            "instance_id": instance_id,
            "pane_id": "%24",
            "pane_role": "palace:N",
            "found": True,
            "agent": "codex",
            "live_agent": True,
        }


def _get(server, path):
    with urllib.request.urlopen(f"{server.base_url}{path}", timeout=2) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_tmuxctld_health_and_resolve_instance_loopback_contract():
    server = create_app(host="127.0.0.1", port=0, control=FakeControl())
    server.start_in_thread()
    try:
        status, payload = _get(server, "/health")
        assert status == 200
        assert payload["ok"] is True
        assert payload["service"] == "tmuxctld"

        status, payload = _get(server, "/resolve-instance?instance_id=abc")
        assert status == 200
        assert payload["found"] is True
        assert payload["instance_id"] == "abc"
        assert payload["pane_id"] == "%24"
        assert payload["pane_role"] == "palace:N"
    finally:
        server.stop()


def test_tmuxctld_resolve_instance_missing_arg_is_400():
    server = create_app(host="127.0.0.1", port=0, control=FakeControl())
    server.start_in_thread()
    try:
        try:
            urllib.request.urlopen(f"{server.base_url}/resolve-instance", timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode())
            assert payload["ok"] is False
        else:  # pragma: no cover
            raise AssertionError("expected HTTP 400")
    finally:
        server.stop()
