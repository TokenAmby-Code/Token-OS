"""Routes that expose Pavlok enforcement over HTTP.

Regression net for the gap where `97bed98` dropped the inline
`/api/pavlok/{status,toggle,zap}` endpoints from main.py, so the `pavlok`
CLI (cli-tools/bin/pavlok) and custodes_watchtower got HTTP 404 and 65
"zaps" landed nowhere. The stimulus function (`send_pavlok_stimulus`)
survived; only the HTTP surface was missing.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env) -> TestClient:
    return TestClient(app_env.main.app)


def test_status_returns_200_not_404(client: TestClient) -> None:
    """GET /api/pavlok/status must exist (was 404 after the route drop)."""
    resp = client.get("/api/pavlok/status")
    assert resp.status_code == 200
    body = resp.json()
    # Keys the `pavlok` CLI show_status() reads.
    for key in ("enabled", "token_set", "default_zap_value", "last_stimulus_at"):
        assert key in body, f"status missing {key!r}: {body}"


def test_zap_invokes_send_pavlok_stimulus(app_env, client: TestClient, monkeypatch) -> None:
    """POST /api/pavlok/zap must drive send_pavlok_stimulus with the CLI contract."""
    calls = []

    def fake_send(stimulus_type="zap", value=None, reason="manual"):
        calls.append({"stimulus_type": stimulus_type, "value": value, "reason": reason})
        return {"success": True, "type": stimulus_type, "value": value}

    monkeypatch.setattr(app_env.main, "send_pavlok_stimulus", fake_send)

    resp = client.post("/api/pavlok/zap", params={"type": "zap", "value": 75, "reason": "break"})
    assert resp.status_code == 200
    assert len(calls) == 1, "send_pavlok_stimulus was not invoked"
    assert calls[0]["stimulus_type"] == "zap"
    assert calls[0]["value"] == 75
    assert calls[0]["reason"] == "break"
    assert resp.json().get("success") is True


def test_zap_passes_beep_type(app_env, client: TestClient, monkeypatch) -> None:
    """The CLI sends beep/vibe through the same /zap route via ?type=."""
    calls = []
    monkeypatch.setattr(
        app_env.main,
        "send_pavlok_stimulus",
        lambda stimulus_type="zap", value=None, reason="manual": (
            calls.append(stimulus_type) or {"success": True}
        ),
    )
    resp = client.post("/api/pavlok/zap", params={"type": "beep"})
    assert resp.status_code == 200
    assert calls == ["beep"]


def test_zap_rejects_unknown_type(client: TestClient) -> None:
    """Unknown stimulus types are rejected at the route boundary with 400."""
    resp = client.post("/api/pavlok/zap", params={"type": "nuke"})
    assert resp.status_code == 400


def test_toggle_body_false_from_disabled_is_idempotent(client: TestClient) -> None:
    """POST /api/pavlok/toggle with {"enabled": false} keeps disabled state."""
    from shared import PAVLOK_CONFIG

    PAVLOK_CONFIG["enabled"] = False

    resp = client.post("/api/pavlok/toggle", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert PAVLOK_CONFIG["enabled"] is False


def test_toggle_body_true_is_idempotent(client: TestClient) -> None:
    """POST /api/pavlok/toggle with {"enabled": true} sets/enforces enabled."""
    from shared import PAVLOK_CONFIG

    PAVLOK_CONFIG["enabled"] = False

    resp = client.post("/api/pavlok/toggle", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert PAVLOK_CONFIG["enabled"] is True

    resp = client.post("/api/pavlok/toggle", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert PAVLOK_CONFIG["enabled"] is True


def test_toggle_query_enable_state_remains_supported(client: TestClient) -> None:
    """Legacy ?enabled= callers (CLI `pavlok on/off`) still set state."""
    from shared import PAVLOK_CONFIG

    resp = client.post("/api/pavlok/toggle", params={"enabled": "false"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert PAVLOK_CONFIG["enabled"] is False

    resp = client.post("/api/pavlok/toggle", params={"enabled": "true"})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert PAVLOK_CONFIG["enabled"] is True


def test_toggle_no_arg_inverts_state(client: TestClient) -> None:
    """No-arg toggle inverts the current enable state (CLI `pavlok toggle`)."""
    from shared import PAVLOK_CONFIG

    PAVLOK_CONFIG["enabled"] = True
    resp = client.post("/api/pavlok/toggle")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert PAVLOK_CONFIG["enabled"] is False
